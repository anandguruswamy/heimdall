//! Live seat-classifier inference: assembles complete 5-round CIR frames from
//! the aligned capture stream, feeds one persistent Python process
//! (live_infer_seats.py), and broadcasts each prediction on the
//! seat-inference WebSocket topic.

use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    io::{BufRead, BufReader, Write},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{Receiver, SyncSender, TrySendError, sync_channel},
    },
    thread,
    time::Duration,
};

use anyhow::{Context, Result, bail};
use parking_lot::Mutex;
use serde_json::{Value, json};
use tokio::sync::broadcast;

use crate::{
    metadata::now_ns,
    pipeline::AlignedCirSample,
    telemetry::{TOPIC_COUNT, Topic, envelope},
    training::TrainingConfig,
};

const N_NODES: u8 = 5;
const N_TAPS: usize = 64;
const ROUNDS_PER_FRAME: u32 = 5;
/// All directed links between distinct nodes: 5 * 4 = 20 rows per frame.
const LINKS_PER_FRAME: usize = N_NODES as usize * (N_NODES as usize - 1);
const PENDING_FRAME_LIMIT: usize = 8;
const FRAME_QUEUE_CAPACITY: usize = 4;
const STDERR_TAIL_LINES: usize = 8;
const RATE_WINDOW_NS: i64 = 5_000_000_000;
const IDLE_STOP_GRACE: Duration = Duration::from_secs(30);

/// Row index in the canonical LINK_ORDER of build_seat_dataset.py:
/// [(a, b) for a in range(5) for b in range(5) if a != b].
fn link_index(from: u8, to: u8) -> usize {
    from as usize * (N_NODES as usize - 1) + to as usize - usize::from(to > from)
}

fn canonical_links() -> impl Iterator<Item = (u8, u8)> {
    (0..N_NODES).flat_map(|a| (0..N_NODES).filter(move |b| *b != a).map(move |b| (a, b)))
}

enum Variant {
    Raw,
    /// Frozen reference taps in canonical link order; live magnitude becomes
    /// |cir - reference| exactly like the calibrated dataset variant.
    Calibrated(Vec<Vec<[f64; 2]>>),
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Idle,
    Starting,
    Running,
    Error,
}

impl Phase {
    fn name(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Starting => "starting",
            Self::Running => "running",
            Self::Error => "error",
        }
    }
}

#[derive(Default)]
struct FrameAssembler {
    pending: BTreeMap<u64, HashMap<(u8, u8), Vec<f32>>>,
    emitted_through: Option<u64>,
}

impl FrameAssembler {
    /// Returns the complete 20-link frame once its final link arrives.
    /// Incomplete frames older than an emitted one are dropped (skipped), and
    /// the pending set is bounded so link outages cannot grow it.
    fn insert(
        &mut self,
        frame: u64,
        key: (u8, u8),
        magnitude: Vec<f32>,
    ) -> Option<(u64, HashMap<(u8, u8), Vec<f32>>)> {
        if self.emitted_through.is_some_and(|done| frame <= done) {
            return None;
        }
        let links = self.pending.entry(frame).or_default();
        links.insert(key, magnitude);
        if links.len() == LINKS_PER_FRAME {
            let complete = self.pending.remove(&frame).unwrap();
            self.emitted_through = Some(frame);
            self.pending.retain(|&pending_frame, _| pending_frame > frame);
            return Some((frame, complete));
        }
        while self.pending.len() > PENDING_FRAME_LIMIT {
            let oldest = *self.pending.keys().next().unwrap();
            self.pending.remove(&oldest);
        }
        None
    }
}

struct RunState {
    generation: u64,
    phase: Phase,
    model: String,
    variant: Variant,
    started_ns: i64,
    predictions: u64,
    dropped_frames: u64,
    recent_ns: VecDeque<i64>,
    last: Option<Value>,
    error: Option<String>,
    stop_reason: Option<String>,
    stderr_tail: VecDeque<String>,
    child: Option<Child>,
    frame_tx: Option<SyncSender<String>>,
    assembler: FrameAssembler,
}

impl Default for RunState {
    fn default() -> Self {
        Self {
            generation: 0,
            phase: Phase::Idle,
            model: String::new(),
            variant: Variant::Raw,
            started_ns: 0,
            predictions: 0,
            dropped_frames: 0,
            recent_ns: VecDeque::new(),
            last: None,
            error: None,
            stop_reason: None,
            stderr_tail: VecDeque::new(),
            child: None,
            frame_tx: None,
            assembler: FrameAssembler::default(),
        }
    }
}

#[derive(Clone)]
pub struct InferenceManager {
    inner: Arc<Inner>,
}

struct Inner {
    config: TrainingConfig,
    stream: broadcast::Sender<Vec<u8>>,
    active: AtomicBool,
    sequence: AtomicU64,
    state: Mutex<RunState>,
}

impl InferenceManager {
    pub fn new(config: TrainingConfig, stream: broadcast::Sender<Vec<u8>>) -> Self {
        Self {
            inner: Arc::new(Inner {
                config,
                stream,
                active: AtomicBool::new(false),
                sequence: AtomicU64::new(0),
                state: Mutex::new(RunState::default()),
            }),
        }
    }

    /// Cheap check for the processing thread: does inference want aligned CIR
    /// samples right now?
    pub fn wants_frames(&self) -> bool {
        self.inner.active.load(Ordering::Relaxed)
    }

    /// List selectable checkpoints, most recently modified first.
    pub fn models(&self) -> Result<Value> {
        let dir = self.inner.config.seatclass_root.join("models");
        let mut models = Vec::new();
        if dir.is_dir() {
            for entry in std::fs::read_dir(&dir)?.flatten() {
                let name = entry.file_name().to_string_lossy().into_owned();
                if !name.ends_with(".pt") {
                    continue;
                }
                let metadata = entry.metadata()?;
                let modified_ms = metadata
                    .modified()
                    .ok()
                    .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
                    .map_or(0, |duration| duration.as_millis() as u64);
                models.push((modified_ms, name, metadata.len()));
            }
        }
        models.sort_by(|a, b| b.0.cmp(&a.0));
        Ok(Value::Array(
            models
                .into_iter()
                .map(|(modified_ms, name, bytes)| {
                    json!({"name": name, "modified_ms": modified_ms, "bytes": bytes})
                })
                .collect(),
        ))
    }

    pub fn status(&self) -> Value {
        let state = self.inner.state.lock();
        let now = now_ns();
        let in_window = state
            .recent_ns
            .iter()
            .filter(|&&at| at > now - RATE_WINDOW_NS)
            .count();
        let rate_hz = if in_window >= 2 {
            let oldest = state
                .recent_ns
                .iter()
                .find(|&&at| at > now - RATE_WINDOW_NS)
                .copied()
                .unwrap_or(now);
            let span_s = (state.recent_ns.back().copied().unwrap_or(now) - oldest) as f64 / 1e9;
            if span_s > 0.0 {
                (in_window - 1) as f64 / span_s
            } else {
                0.0
            }
        } else {
            0.0
        };
        json!({
            "status": state.phase.name(),
            "model": state.model,
            "started_ns": if state.started_ns == 0 { Value::Null } else { json!(state.started_ns) },
            "predictions": state.predictions,
            "dropped_frames": state.dropped_frames,
            "rate_hz": rate_hz,
            "last": state.last,
            "error": state.error,
            "stop_reason": state.stop_reason,
            "config": {
                "python": self.inner.config.python,
                "seatclass_root": self.inner.config.seatclass_root,
            },
        })
    }

    /// Launch the persistent inference process for `model_name`. For
    /// calibrated checkpoints `frozen_refs` must hold the complete frozen
    /// board reference map.
    pub fn start(
        &self,
        model_name: &str,
        frozen_refs: Option<BTreeMap<(u8, u8), Vec<[f64; 2]>>>,
    ) -> Result<Value> {
        let model_name = model_name.trim();
        if model_name.is_empty()
            || !model_name.ends_with(".pt")
            || model_name.contains(['/', '\\'])
            || model_name.contains("..")
        {
            bail!("model must be a .pt file name from the models directory");
        }
        let checkpoint = self.inner.config.seatclass_root.join("models").join(model_name);
        if !checkpoint.is_file() {
            bail!("model checkpoint not found: {}", checkpoint.display());
        }
        let scripts = self.inner.config.seatclass_root.join("scripts");
        if !scripts.join("live_infer_seats.py").is_file() {
            bail!(
                "live_infer_seats.py not found under {} (set HEIMDALL_SEATCLASS_ROOT)",
                scripts.display()
            );
        }
        if !self.inner.config.python.is_file() {
            bail!(
                "python interpreter not found at {} (set HEIMDALL_PYTHON)",
                self.inner.config.python.display()
            );
        }
        let variant = if model_name.contains("calibrated") {
            let refs = frozen_refs.context(
                "the calibrated model subtracts frozen board references; freeze the board first",
            )?;
            let ordered = canonical_links()
                .map(|key| {
                    let taps = refs.get(&key).with_context(|| {
                        format!("frozen references are missing link {}>{}", key.0, key.1)
                    })?;
                    if taps.len() != N_TAPS {
                        bail!("frozen reference for {}>{} has {} taps", key.0, key.1, taps.len());
                    }
                    Ok(taps.clone())
                })
                .collect::<Result<Vec<_>>>()?;
            Variant::Calibrated(ordered)
        } else {
            Variant::Raw
        };

        let mut state = self.inner.state.lock();
        if matches!(state.phase, Phase::Starting | Phase::Running) {
            bail!("live inference is already running");
        }
        let mut child = Command::new(&self.inner.config.python)
            .arg("-u")
            .arg("live_infer_seats.py")
            .arg("--checkpoint")
            .arg(&checkpoint)
            .current_dir(&scripts)
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .with_context(|| {
                format!("spawn {} for live inference", self.inner.config.python.display())
            })?;
        let stdin = child.stdin.take().context("inference stdin unavailable")?;
        let stdout = child.stdout.take().context("inference stdout unavailable")?;
        let stderr = child.stderr.take().context("inference stderr unavailable")?;

        let generation = state.generation + 1;
        let (frame_tx, frame_rx) = sync_channel::<String>(FRAME_QUEUE_CAPACITY);
        *state = RunState {
            generation,
            phase: Phase::Starting,
            model: model_name.to_owned(),
            variant,
            started_ns: now_ns(),
            child: Some(child),
            frame_tx: Some(frame_tx),
            ..RunState::default()
        };
        drop(state);
        self.inner.active.store(true, Ordering::Release);

        spawn_frame_writer(frame_rx, stdin);
        self.spawn_stdout_reader(generation, stdout);
        self.spawn_stderr_reader(generation, stderr);
        Ok(json!({"status": "starting", "model": model_name, "run": generation}))
    }

    /// Stop the current run: closing the frame channel drops the child's
    /// stdin, the script exits on EOF, and a background thread reaps it
    /// (killing after a two-second grace period if needed).
    pub fn stop(&self, reason: &str) -> Value {
        self.inner.active.store(false, Ordering::Release);
        let child = {
            let mut state = self.inner.state.lock();
            state.phase = Phase::Idle;
            state.stop_reason = Some(reason.to_owned());
            state.frame_tx = None;
            state.assembler = FrameAssembler::default();
            state.child.take()
        };
        if let Some(mut child) = child {
            thread::spawn(move || {
                for _ in 0..20 {
                    if matches!(child.try_wait(), Ok(Some(_))) {
                        return;
                    }
                    thread::sleep(Duration::from_millis(100));
                }
                let _ = child.kill();
                let _ = child.wait();
            });
        }
        self.status()
    }

    /// Called when the last seat-inference WebSocket subscriber goes away:
    /// stop the run if nobody re-subscribes within the grace period.
    pub fn schedule_idle_stop(&self, demand: Arc<[AtomicU64; TOPIC_COUNT]>) {
        if !self.wants_frames() {
            return;
        }
        let manager = self.clone();
        let generation = self.inner.state.lock().generation;
        tokio::spawn(async move {
            tokio::time::sleep(IDLE_STOP_GRACE).await;
            let idle =
                demand[Topic::SeatInference as usize].load(Ordering::Relaxed) == 0;
            if idle
                && manager.wants_frames()
                && manager.inner.state.lock().generation == generation
            {
                manager.stop("no dashboard subscribed");
            }
        });
    }

    /// Fold aligned CIR samples into frames; complete frames are queued to
    /// the Python process. Runs on the processing thread, so the fast path
    /// (inference off) is a single relaxed atomic load.
    pub fn ingest_cir(&self, samples: &[AlignedCirSample]) {
        if samples.is_empty() || !self.wants_frames() {
            return;
        }
        let mut state = self.inner.state.lock();
        if !matches!(state.phase, Phase::Starting | Phase::Running) {
            return;
        }
        for sample in samples {
            if sample.from >= N_NODES
                || sample.to >= N_NODES
                || sample.from == sample.to
                || sample.iq.len() != N_TAPS
            {
                continue;
            }
            // Magnitude from iq, matching build_seat_dataset.py exactly (the
            // stored magnitude field is not guaranteed to equal |iq|).
            let magnitude: Vec<f32> = match &state.variant {
                Variant::Raw => sample
                    .iq
                    .iter()
                    .map(|[re, im]| {
                        let (re, im) = (*re as f64, *im as f64);
                        (re * re + im * im).sqrt() as f32
                    })
                    .collect(),
                Variant::Calibrated(references) => {
                    let reference = &references[link_index(sample.from, sample.to)];
                    sample
                        .iq
                        .iter()
                        .zip(reference)
                        .map(|([re, im], [ref_re, ref_im])| {
                            let d_re = *re as f64 - ref_re;
                            let d_im = *im as f64 - ref_im;
                            (d_re * d_re + d_im * d_im).sqrt() as f32
                        })
                        .collect()
                }
            };
            let frame = u64::from(sample.round / ROUNDS_PER_FRAME);
            let key = (sample.from, sample.to);
            if let Some((frame_id, links)) = state.assembler.insert(frame, key, magnitude) {
                let matrix: Vec<&Vec<f32>> =
                    canonical_links().map(|link| &links[&link]).collect();
                let line = json!({
                    "frame_id": frame_id,
                    "ts": now_ns() / 1_000_000,
                    "magnitude": matrix,
                })
                .to_string();
                if let Some(frame_tx) = &state.frame_tx {
                    match frame_tx.try_send(line) {
                        Ok(()) => {}
                        // Latest-wins live stream: dropping under backpressure
                        // beats stalling the DSP thread.
                        Err(TrySendError::Full(_)) => state.dropped_frames += 1,
                        Err(TrySendError::Disconnected(_)) => {}
                    }
                }
            }
        }
    }

    fn spawn_stdout_reader(
        &self,
        generation: u64,
        stdout: impl std::io::Read + Send + 'static,
    ) {
        let manager = self.clone();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                let Ok(value) = serde_json::from_str::<Value>(&line) else {
                    continue;
                };
                if value["ready"] == true {
                    let mut state = manager.inner.state.lock();
                    if state.generation == generation && state.phase == Phase::Starting {
                        state.phase = Phase::Running;
                    }
                    continue;
                }
                if value.get("seat").is_none() {
                    continue;
                }
                {
                    let mut state = manager.inner.state.lock();
                    if state.generation != generation {
                        return;
                    }
                    state.phase = Phase::Running;
                    state.predictions += 1;
                    let now = now_ns();
                    state.recent_ns.push_back(now);
                    while state
                        .recent_ns
                        .front()
                        .is_some_and(|&at| at < now - RATE_WINDOW_NS)
                    {
                        state.recent_ns.pop_front();
                    }
                    state.last = Some(value.clone());
                }
                let sequence = manager.inner.sequence.fetch_add(1, Ordering::Relaxed);
                let bytes = envelope(
                    Topic::SeatInference,
                    sequence,
                    0,
                    0,
                    0,
                    value.to_string().as_bytes(),
                );
                let _ = manager.inner.stream.send(bytes);
            }
            manager.handle_exit(generation);
        });
    }

    fn spawn_stderr_reader(
        &self,
        generation: u64,
        stderr: impl std::io::Read + Send + 'static,
    ) {
        let manager = self.clone();
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines() {
                let Ok(line) = line else { break };
                if line.trim().is_empty() {
                    continue;
                }
                tracing::warn!(target: "seat_inference", "{line}");
                let mut state = manager.inner.state.lock();
                if state.generation != generation {
                    return;
                }
                state.stderr_tail.push_back(line);
                while state.stderr_tail.len() > STDERR_TAIL_LINES {
                    state.stderr_tail.pop_front();
                }
            }
        });
    }

    /// stdout EOF: either a clean stop already marked Idle, or the process
    /// died and the run flips to an error state visible to the dashboard.
    /// The generation check must precede any state change so a stale reader
    /// from a stopped run can never disable a newer one.
    fn handle_exit(&self, generation: u64) {
        let child = {
            let mut state = self.inner.state.lock();
            if state.generation != generation || state.phase == Phase::Idle {
                return;
            }
            self.inner.active.store(false, Ordering::Release);
            state.phase = Phase::Error;
            let tail = state
                .stderr_tail
                .iter()
                .cloned()
                .collect::<Vec<_>>()
                .join(" | ");
            state.error = Some(if tail.is_empty() {
                "inference process exited unexpectedly".to_owned()
            } else {
                format!("inference process exited: {tail}")
            });
            state.frame_tx = None;
            state.child.take()
        };
        if let Some(mut child) = child {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn spawn_frame_writer(frames: Receiver<String>, mut stdin: ChildStdin) {
    thread::spawn(move || {
        while let Ok(line) = frames.recv() {
            if stdin.write_all(line.as_bytes()).is_err() || stdin.write_all(b"\n").is_err() {
                return;
            }
        }
        // Channel closed: dropping stdin sends EOF and the script exits.
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn link_index_matches_python_link_order() {
        let ordered = canonical_links().collect::<Vec<_>>();
        assert_eq!(ordered.len(), LINKS_PER_FRAME);
        assert_eq!(ordered[0], (0, 1));
        assert_eq!(ordered[4], (1, 0));
        assert_eq!(ordered[19], (4, 3));
        for (index, (from, to)) in ordered.into_iter().enumerate() {
            assert_eq!(link_index(from, to), index, "{from}>{to}");
        }
    }

    #[test]
    fn assembler_emits_only_complete_frames_and_skips_stale() {
        let mut assembler = FrameAssembler::default();
        let links = canonical_links().collect::<Vec<_>>();
        for (from, to) in links.iter().take(LINKS_PER_FRAME - 1) {
            assert!(assembler.insert(3, (*from, *to), vec![1.0]).is_none());
        }
        let (frame, complete) = assembler
            .insert(3, links[LINKS_PER_FRAME - 1], vec![1.0])
            .expect("20th link completes the frame");
        assert_eq!(frame, 3);
        assert_eq!(complete.len(), LINKS_PER_FRAME);
        // Late sample for an already-emitted frame is ignored.
        assert!(assembler.insert(3, links[0], vec![1.0]).is_none());
        assert!(assembler.insert(2, links[0], vec![1.0]).is_none());
        assert!(assembler.pending.is_empty());
    }

    #[test]
    fn assembler_bounds_pending_frames() {
        let mut assembler = FrameAssembler::default();
        for frame in 0..(PENDING_FRAME_LIMIT as u64 + 5) {
            assembler.insert(frame, (0, 1), vec![1.0]);
        }
        assert!(assembler.pending.len() <= PENDING_FRAME_LIMIT);
        assert!(assembler.pending.contains_key(&(PENDING_FRAME_LIMIT as u64 + 4)));
    }

    #[test]
    fn start_validates_model_names() {
        let (stream, _) = broadcast::channel(8);
        let manager = InferenceManager::new(TrainingConfig::from_env(), stream);
        for bad in ["", "model", "../x.pt", "a/b.pt", "a\\b.pt"] {
            assert!(manager.start(bad, None).is_err(), "{bad:?}");
        }
        assert_eq!(manager.status()["status"], "idle");
        assert!(!manager.wants_frames());
    }

    fn frame_samples(round: u32) -> Vec<AlignedCirSample> {
        canonical_links()
            .map(|(from, to)| AlignedCirSample {
                from,
                to,
                round,
                event_s: 0.0,
                evidence: 0,
                delay_samples: 0.0,
                common_phase_rad: 0.0,
                correlation: 1.0,
                quality: 0,
                marker_raw: 0.0,
                marker_aligned: 0.0,
                fit_algorithm: "test".to_owned(),
                match_score: None,
                magnitude: vec![0.0; N_TAPS],
                iq: vec![[1e-3, 0.0]; N_TAPS],
            })
            .collect()
    }

    fn wait_for(mut check: impl FnMut() -> bool, timeout: Duration) -> bool {
        let deadline = std::time::Instant::now() + timeout;
        while std::time::Instant::now() < deadline {
            if check() {
                return true;
            }
            thread::sleep(Duration::from_millis(100));
        }
        false
    }

    /// Full loop against the real interpreter and checkpoint: run explicitly
    /// with `cargo test -- --ignored live_inference` on the configured host.
    #[test]
    #[ignore = "requires the configured python interpreter with torch and a raw checkpoint"]
    fn live_inference_end_to_end_start_predict_stop() {
        let (stream, mut receiver) = broadcast::channel(64);
        let manager = InferenceManager::new(TrainingConfig::from_env(), stream);
        let models = manager.models().unwrap();
        let model = models
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|row| row["name"].as_str())
            .find(|name| name.contains("data_raw") && !name.contains("shuffled"))
            .expect("a raw checkpoint under models/")
            .to_owned();
        manager.start(&model, None).unwrap();
        assert!(manager.start(&model, None).is_err(), "second start must be rejected");
        assert!(
            wait_for(|| manager.status()["status"] == "running", Duration::from_secs(30)),
            "model load: {:?}",
            manager.status()
        );
        manager.ingest_cir(&frame_samples(0));
        assert!(
            wait_for(
                || manager.status()["predictions"].as_u64().unwrap_or(0) >= 1,
                Duration::from_secs(10)
            ),
            "prediction: {:?}",
            manager.status()
        );
        let status = manager.status();
        let probs = status["last"]["probs"].as_array().unwrap();
        assert_eq!(probs.len(), 4);
        assert_eq!(status["last"]["frame_id"], 0);
        let mut broadcast_seen = false;
        while let Ok(bytes) = receiver.try_recv() {
            if crate::telemetry::envelope_topic(&bytes) == Some(Topic::SeatInference) {
                broadcast_seen = true;
            }
        }
        assert!(broadcast_seen, "prediction envelope must reach the stream");
        manager.stop("test");
        assert_eq!(manager.status()["status"], "idle");
        assert!(!manager.wants_frames());
        // The process exits on stdin EOF; a fresh start must work afterwards
        // (this also proves the previous child is not lingering as the only
        // holder of the checkpoint or pipes).
        assert!(wait_for(
            || manager.start(&model, None).is_ok(),
            Duration::from_secs(5)
        ));
        assert!(
            wait_for(|| manager.status()["status"] == "running", Duration::from_secs(30)),
            "restart: {:?}",
            manager.status()
        );
        manager.stop("test");
        assert_eq!(manager.status()["status"], "idle");
    }
}
