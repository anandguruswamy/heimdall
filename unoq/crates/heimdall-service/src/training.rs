//! Seat-classifier training pipeline: exports tagged capture clips into the
//! layout `build_seat_dataset.py` expects, then runs the dataset build and the
//! CNN training script as external Python processes with live log streaming.

use std::{
    ffi::OsStr,
    fs::{self, File},
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Arc,
    thread,
};

use anyhow::{Context, Result, bail};
use parking_lot::Mutex;
use serde_json::{Value, json};

use crate::metadata::now_ns;

/// Class names must match `LABELS` in build_seat_dataset.py and `CLASS_NAMES`
/// in train_seat_classifier.py exactly (FrontLeft=0, FrontRight=1,
/// BackRight=2, BackLeft=3).
pub const SEAT_CLASSES: [&str; 4] = ["FrontLeft", "FrontRight", "BackRight", "BackLeft"];

const DEFAULT_PYTHON: &str = r"C:\Users\qc_de\AppData\Local\Programs\Python\Python311\python.exe";
const DEFAULT_SEATCLASS_ROOT: &str =
    r"C:\Users\qc_de\OneDrive\Documents\GitHub\heimdall\host-tools\seat-classification";
const MAX_LOG_LINES: usize = 10_000;
const MIN_EPOCHS: i64 = 1;
const MAX_EPOCHS: i64 = 500;

#[derive(Clone)]
pub struct TrainingConfig {
    /// Interpreter that has torch installed; `python` on PATH is deliberately
    /// not used because other interpreters on the host lack torch.
    pub python: PathBuf,
    /// Seat-classification root containing scripts/ and models/
    /// (host-tools/seat-classification in the heimdall repository).
    pub seatclass_root: PathBuf,
}

impl TrainingConfig {
    pub fn from_env() -> Self {
        Self {
            python: std::env::var_os("HEIMDALL_PYTHON")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(DEFAULT_PYTHON)),
            seatclass_root: std::env::var_os("HEIMDALL_SEATCLASS_ROOT")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(DEFAULT_SEATCLASS_ROOT)),
        }
    }
}

#[derive(Clone)]
pub struct TrainingClip {
    pub id: i64,
    pub zip_path: PathBuf,
    pub seat: String,
    pub person: String,
}

#[derive(Clone)]
pub struct TrainingManager {
    config: TrainingConfig,
    work_root: Arc<PathBuf>,
    state: Arc<Mutex<RunState>>,
}

#[derive(Default)]
struct RunState {
    running: bool,
    run_id: u64,
    variant: String,
    clip_ids: Vec<i64>,
    started_ns: i64,
    finished_ns: Option<i64>,
    log: Vec<String>,
    truncated: bool,
    error: Option<String>,
    result: Option<Value>,
}

impl TrainingManager {
    pub fn new(work_root: impl AsRef<Path>, config: TrainingConfig) -> Self {
        Self {
            config,
            work_root: Arc::new(work_root.as_ref().to_path_buf()),
            state: Arc::new(Mutex::new(RunState::default())),
        }
    }

    /// Status snapshot plus the log lines from index `after` onward, so the
    /// dashboard can poll incrementally with `?after=<log_next>`.
    pub fn status(&self, after: usize) -> Value {
        let state = self.state.lock();
        let status = if state.running {
            "running"
        } else if state.error.is_some() {
            "failed"
        } else if state.result.is_some() {
            "complete"
        } else {
            "idle"
        };
        let from = after.min(state.log.len());
        json!({
            "status": status,
            "run_id": state.run_id,
            "variant": state.variant,
            "clip_ids": state.clip_ids,
            "started_ns": if state.run_id == 0 { Value::Null } else { json!(state.started_ns) },
            "finished_ns": state.finished_ns,
            "error": state.error,
            "result": state.result,
            "log": state.log[from..],
            "log_next": state.log.len(),
            "log_truncated": state.truncated,
            "config": {
                "python": self.config.python,
                "seatclass_root": self.config.seatclass_root,
            },
        })
    }

    pub fn start(
        &self,
        clips: Vec<TrainingClip>,
        variant: &str,
        epochs: Option<i64>,
    ) -> Result<Value> {
        if !matches!(variant, "raw" | "calibrated") {
            bail!("variant must be raw or calibrated");
        }
        let epochs = epochs.map(|value| value.clamp(MIN_EPOCHS, MAX_EPOCHS));
        for class in SEAT_CLASSES {
            if !clips.iter().any(|clip| clip.seat == class) {
                bail!("training requires at least one tagged clip for {class}");
            }
        }
        let run_id = {
            let mut state = self.state.lock();
            if state.running {
                bail!("a training run is already in progress");
            }
            let run_id = state.run_id + 1;
            *state = RunState {
                running: true,
                run_id,
                variant: variant.to_owned(),
                clip_ids: clips.iter().map(|clip| clip.id).collect(),
                started_ns: now_ns(),
                ..RunState::default()
            };
            run_id
        };
        let manager = self.clone();
        let variant_for_run = variant.to_owned();
        let clip_count = clips.len();
        thread::spawn(move || {
            let outcome = manager.execute(run_id, &clips, &variant_for_run, epochs);
            let mut state = manager.state.lock();
            if state.run_id != run_id {
                return;
            }
            state.running = false;
            state.finished_ns = Some(now_ns());
            match outcome {
                Ok(result) => state.result = Some(result),
                Err(error) => {
                    let message = format!("{error:#}");
                    state.log.push(format!("ERROR: {message}"));
                    state.error = Some(message);
                }
            }
        });
        Ok(json!({"run_id": run_id, "clips": clip_count, "variant": variant}))
    }

    fn execute(
        &self,
        run_id: u64,
        clips: &[TrainingClip],
        variant: &str,
        epochs: Option<i64>,
    ) -> Result<Value> {
        let scripts = self.config.seatclass_root.join("scripts");
        if !scripts.is_dir() {
            bail!(
                "seat-classification scripts directory not found at {} (set HEIMDALL_SEATCLASS_ROOT)",
                scripts.display()
            );
        }
        if !self.config.python.is_file() {
            bail!(
                "python interpreter not found at {} (set HEIMDALL_PYTHON)",
                self.config.python.display()
            );
        }
        // Only the current run's workspace is kept on disk.
        let _ = fs::remove_dir_all(self.work_root.as_ref());
        let work = self.work_root.join(format!("run-{run_id:06}"));
        let source = work.join("clips");
        let dataset = work.join("dataset");
        fs::create_dir_all(&source)
            .with_context(|| format!("create training workspace {}", source.display()))?;
        self.log(run_id, format!("exporting {} tagged clips", clips.len()));
        for clip in clips {
            let folder = export_clip(&source, clip)?;
            self.log(run_id, format!("  clip {:06} -> {folder}", clip.id));
        }
        self.run_python(
            run_id,
            &scripts,
            &[
                OsStr::new("build_seat_dataset.py"),
                OsStr::new("--dataset-dir"),
                source.as_os_str(),
                OsStr::new("--out-root"),
                dataset.as_os_str(),
            ],
            "dataset build",
        )?;
        let data_dir = format!("data_{variant}");
        let epochs_value = epochs.map(|value| value.to_string());
        let mut args = vec![
            OsStr::new("train_seat_classifier.py"),
            OsStr::new("--dataset-root"),
            dataset.as_os_str(),
            OsStr::new("--data-dir"),
            OsStr::new(&data_dir),
        ];
        if let Some(epochs_value) = &epochs_value {
            args.push(OsStr::new("--epochs"));
            args.push(OsStr::new(epochs_value));
        }
        self.run_python(run_id, &scripts, &args, "training")?;
        let state = self.state.lock();
        Ok(parse_result(&state.log))
    }

    fn run_python(&self, run_id: u64, cwd: &Path, args: &[&OsStr], stage: &str) -> Result<()> {
        let shown = args
            .iter()
            .map(|arg| arg.to_string_lossy())
            .collect::<Vec<_>>()
            .join(" ");
        self.log(run_id, format!("$ python -u {shown}"));
        let mut child = Command::new(&self.config.python)
            .arg("-u")
            .args(args)
            .current_dir(cwd)
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .with_context(|| {
                format!("spawn {} for the {stage}", self.config.python.display())
            })?;
        let stdout = child.stdout.take().context("child stdout unavailable")?;
        let stderr = child.stderr.take().context("child stderr unavailable")?;
        let stdout_reader = self.spawn_line_reader(run_id, stdout, false);
        let stderr_reader = self.spawn_line_reader(run_id, stderr, true);
        let status = child
            .wait()
            .with_context(|| format!("wait for the {stage} process"))?;
        let _ = stdout_reader.join();
        let stderr_tail = stderr_reader.join().unwrap_or_default();
        if !status.success() {
            let detail = if stderr_tail.is_empty() {
                String::new()
            } else {
                format!(": {}", stderr_tail.join(" | "))
            };
            bail!("{stage} failed ({status}){detail}");
        }
        Ok(())
    }

    fn spawn_line_reader(
        &self,
        run_id: u64,
        stream: impl std::io::Read + Send + 'static,
        keep_tail: bool,
    ) -> thread::JoinHandle<Vec<String>> {
        let manager = self.clone();
        thread::spawn(move || {
            let mut tail = Vec::new();
            let mut reader = BufReader::new(stream);
            let mut buffer = Vec::new();
            loop {
                buffer.clear();
                match reader.read_until(b'\n', &mut buffer) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        let line = String::from_utf8_lossy(&buffer)
                            .trim_end_matches(['\r', '\n'])
                            .to_owned();
                        if keep_tail && !line.trim().is_empty() {
                            tail.push(line.clone());
                            if tail.len() > 8 {
                                tail.remove(0);
                            }
                        }
                        manager.log(run_id, line);
                    }
                }
            }
            tail
        })
    }

    fn log(&self, run_id: u64, line: String) {
        let mut state = self.state.lock();
        if state.run_id != run_id || state.truncated {
            return;
        }
        if state.log.len() >= MAX_LOG_LINES {
            state.truncated = true;
            state
                .log
                .push("[log truncated; further output suppressed]".to_owned());
            return;
        }
        state.log.push(line);
    }
}

/// Extract aligned-cirs.ndjson + metadata.json from a stored clip zip into
/// `clip-<id>-<Seat><Person>/`, the folder-name label encoding that
/// build_seat_dataset.py parses.
fn export_clip(source_root: &Path, clip: &TrainingClip) -> Result<String> {
    let person: String = clip
        .person
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect();
    let folder = format!("clip-{:06}-{}{}", clip.id, clip.seat, person);
    let dir = source_root.join(&folder);
    fs::create_dir_all(&dir)?;
    let file = File::open(&clip.zip_path)
        .with_context(|| format!("open stored clip {:06}", clip.id))?;
    let mut zip = zip::ZipArchive::new(file)
        .with_context(|| format!("read stored clip {:06}", clip.id))?;
    for name in ["aligned-cirs.ndjson", "metadata.json"] {
        let mut entry = zip.by_name(name).with_context(|| {
            format!(
                "clip {:06} contains no {name}; only clips captured while the live \
                 CIR pipeline was aligned can be trained on",
                clip.id
            )
        })?;
        let mut output = File::create(dir.join(name))?;
        std::io::copy(&mut entry, &mut output)?;
    }
    Ok(folder)
}

/// Best-effort extraction of the headline numbers from the training output;
/// the full log is always available to the dashboard regardless.
fn parse_result(log: &[String]) -> Value {
    let mut test_accuracy = None;
    let mut model_path = None;
    for line in log {
        let trimmed = line.trim();
        if trimmed.starts_with("test loss") {
            if let Some(index) = trimmed.find("accuracy") {
                let token = trimmed[index + "accuracy".len()..]
                    .split_whitespace()
                    .next()
                    .unwrap_or("");
                if let Ok(value) = token.parse::<f64>() {
                    test_accuracy = Some(value);
                }
            }
        } else if let Some(rest) = trimmed.strip_prefix("saved model to ") {
            model_path = Some(rest.trim().to_owned());
        }
    }
    json!({
        "test_accuracy": test_accuracy,
        "model_path": model_path,
        "confusion": parse_confusion(log),
        "class_names": SEAT_CLASSES,
    })
}

fn parse_confusion(log: &[String]) -> Option<Vec<Vec<i64>>> {
    let start = log
        .iter()
        .rposition(|line| line.trim_start().starts_with("confusion matrix"))?;
    let mut rows = Vec::new();
    for line in log.iter().skip(start + 2).take(SEAT_CLASSES.len()) {
        let mut tokens = line.split_whitespace();
        let name = tokens.next()?;
        if !SEAT_CLASSES.contains(&name) {
            return None;
        }
        let row = tokens
            .map(|token| token.parse::<i64>().ok())
            .collect::<Option<Vec<_>>>()?;
        if row.len() != SEAT_CLASSES.len() {
            return None;
        }
        rows.push(row);
    }
    (rows.len() == SEAT_CLASSES.len()).then_some(rows)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use zip::{CompressionMethod, ZipWriter, write::SimpleFileOptions};

    fn stored_clip_zip(path: &Path, with_cirs: bool) {
        let mut zip = ZipWriter::new(File::create(path).unwrap());
        let options = SimpleFileOptions::default().compression_method(CompressionMethod::Stored);
        if with_cirs {
            zip.start_file("aligned-cirs.ndjson", options).unwrap();
            zip.write_all(b"{\"round\":0}\n").unwrap();
        }
        zip.start_file("metadata.json", options).unwrap();
        zip.write_all(b"{\"board_freeze\":{\"references\":{}}}")
            .unwrap();
        zip.finish().unwrap();
    }

    #[test]
    fn export_clip_writes_label_encoded_folder() {
        let dir = tempfile::tempdir().unwrap();
        let zip_path = dir.path().join("clip-000013.zip");
        stored_clip_zip(&zip_path, true);
        let clip = TrainingClip {
            id: 13,
            zip_path,
            seat: "FrontLeft".to_owned(),
            person: "Simarjit Singh!".to_owned(),
        };
        let folder = export_clip(dir.path(), &clip).unwrap();
        assert_eq!(folder, "clip-000013-FrontLeftSimarjitSingh");
        assert!(dir.path().join(&folder).join("aligned-cirs.ndjson").is_file());
        assert!(dir.path().join(&folder).join("metadata.json").is_file());
    }

    #[test]
    fn export_clip_requires_aligned_cirs() {
        let dir = tempfile::tempdir().unwrap();
        let zip_path = dir.path().join("clip-000014.zip");
        stored_clip_zip(&zip_path, false);
        let clip = TrainingClip {
            id: 14,
            zip_path,
            seat: "BackLeft".to_owned(),
            person: String::new(),
        };
        let error = export_clip(dir.path(), &clip).unwrap_err().to_string();
        assert!(error.contains("aligned-cirs.ndjson"), "{error}");
    }

    #[test]
    fn start_requires_every_seat_class() {
        let manager = TrainingManager::new("training-test-unused", TrainingConfig::from_env());
        let clips = ["FrontLeft", "FrontRight", "BackRight"]
            .into_iter()
            .map(|seat| TrainingClip {
                id: 1,
                zip_path: PathBuf::new(),
                seat: seat.to_owned(),
                person: String::new(),
            })
            .collect::<Vec<_>>();
        let error = manager.start(clips, "raw", None).unwrap_err().to_string();
        assert!(error.contains("BackLeft"), "{error}");
        assert_eq!(manager.status(0)["status"], "idle");
    }

    #[test]
    fn parses_train_script_output() {
        let log: Vec<String> = [
            "epoch 30/30  train loss 0.0100 acc 0.9800  |  val loss 0.0200 acc 0.9700  (2.1s) *",
            "",
            "=== test evaluation (best-val model) ===",
            "test loss 0.0500  accuracy 0.9750 (390/400)",
            "",
            "confusion matrix (rows = true, cols = predicted):",
            "              FrontLeft  FrontRigh  BackRight   BackLeft",
            "   FrontLeft        99          1          0          0",
            "  FrontRight         2         97          1          0",
            "   BackRight         0          0        100          0",
            "    BackLeft         0          2          4         94",
            "",
            "saved model to C:\\models\\seat_cnn_data_raw.pt",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect();
        let result = parse_result(&log);
        assert_eq!(result["test_accuracy"], 0.975);
        assert_eq!(result["model_path"], "C:\\models\\seat_cnn_data_raw.pt");
        assert_eq!(result["confusion"][0][0], 99);
        assert_eq!(result["confusion"][3][2], 4);
        assert_eq!(result["class_names"][3], "BackLeft");
    }
}
