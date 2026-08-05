use std::{
    fs::{self, File},
    io::Write,
    path::{Path, PathBuf},
    sync::Arc,
    thread,
};

use anyhow::{Context, Result, bail};
use heimdall_protocol::StreamParser;
use parking_lot::Mutex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use zip::{CompressionMethod, ZipWriter, write::SimpleFileOptions};

use crate::{
    archive::{hash_file, require_storage_headroom},
    metadata::Metadata,
    pipeline::AlignedCirSample,
};

const DEFAULT_CLIP_DURATION_S: i64 = 10;
const MIN_CLIP_DURATION_S: i64 = 1;
const MAX_CLIP_DURATION_S: i64 = 60;
const MAX_ACTIVE_CLIPS: usize = 2;

#[derive(Clone)]
pub struct ClipManager {
    root: Arc<PathBuf>,
    metadata: Arc<Metadata>,
    state: Arc<Mutex<ClipState>>,
}

struct ClipState {
    parser: StreamParser,
    active: Vec<ActiveClip>,
}

#[derive(Clone)]
struct TimedRecord {
    received_ns: i64,
    sequence: u32,
    raw: Vec<u8>,
}

struct ActiveClip {
    id: i64,
    trigger_ns: i64,
    deadline_ns: i64,
    duration_s: i64,
    name: String,
    note: String,
    context: Value,
    records: Vec<TimedRecord>,
    cir_samples: Vec<AlignedCirSample>,
}

impl ClipManager {
    pub fn new(root: impl AsRef<Path>, metadata: Arc<Metadata>) -> Result<Self> {
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(&root)?;
        Ok(Self {
            root: Arc::new(root.canonicalize()?),
            metadata,
            state: Arc::new(Mutex::new(ClipState {
                parser: StreamParser::default(),
                active: Vec::new(),
            })),
        })
    }

    pub fn reset_stream(&self) {
        let active = {
            let mut state = self.state.lock();
            state.parser = StreamParser::default();
            std::mem::take(&mut state.active)
        };
        for clip in active {
            let _ = self.metadata.update_clip(
                clip.id,
                &json!({
                    "status": "failed", "name": clip.name, "note": clip.note,
                    "trigger_ns": clip.trigger_ns,
                    "error": "gateway connection changed during capture"
                }),
            );
        }
    }

    pub fn start(&self, request: &Value, context: Value, trigger_ns: i64) -> Result<Value> {
        let name = request["name"].as_str().unwrap_or("").trim();
        let note = request["note"].as_str().unwrap_or("").trim();
        if name.len() > 120 || note.len() > 2_000 {
            bail!("clip name or note is too long");
        }
        let duration_s = request["duration_s"]
            .as_i64()
            .unwrap_or(DEFAULT_CLIP_DURATION_S)
            .clamp(MIN_CLIP_DURATION_S, MAX_CLIP_DURATION_S);
        let deadline_ns = trigger_ns + duration_s * 1_000_000_000;
        let mut state = self.state.lock();
        if state.active.len() >= MAX_ACTIVE_CLIPS {
            bail!("at most {MAX_ACTIVE_CLIPS} capture clips may be active");
        }
        let initial = json!({
            "status": "capturing", "name": name, "note": note,
            "trigger_ns": trigger_ns, "deadline_ns": deadline_ns,
            "duration_s": duration_s
        });
        let row = self.metadata.add_clip(&initial)?;
        let id = row["id"].as_i64().context("clip row has no id")?;
        state.active.push(ActiveClip {
            id,
            trigger_ns,
            deadline_ns,
            duration_s,
            name: name.to_owned(),
            note: note.to_owned(),
            context,
            records: Vec::new(),
            cir_samples: Vec::new(),
        });
        Ok(row)
    }

    pub fn ingest(&self, received_ns: i64, bytes: &[u8]) {
        let completed = {
            let mut state = self.state.lock();
            let records = state
                .parser
                .feed(bytes)
                .into_iter()
                .map(|record| TimedRecord {
                    received_ns,
                    sequence: record.sequence,
                    raw: record.raw,
                })
                .collect::<Vec<_>>();
            for record in records {
                for clip in &mut state.active {
                    if record.received_ns <= clip.deadline_ns {
                        clip.records.push(record.clone());
                    }
                }
            }
            let mut completed = Vec::new();
            let mut index = 0;
            while index < state.active.len() {
                if received_ns >= state.active[index].deadline_ns {
                    completed.push(state.active.remove(index));
                } else {
                    index += 1;
                }
            }
            completed
        };
        for clip in completed {
            self.finalize_async(clip);
        }
    }

    pub fn has_active_clips(&self) -> bool {
        !self.state.lock().active.is_empty()
    }

    pub fn ingest_cir(&self, received_ns: i64, samples: Vec<AlignedCirSample>) {
        if samples.is_empty() {
            return;
        }
        let mut state = self.state.lock();
        if state.active.is_empty() {
            return;
        }
        for sample in samples {
            for clip in &mut state.active {
                if received_ns <= clip.deadline_ns {
                    clip.cir_samples.push(sample.clone());
                }
            }
        }
    }

    pub fn path(&self, id: i64) -> Result<PathBuf> {
        let row = self
            .metadata
            .clip(id)?
            .context("capture clip does not exist")?;
        if row["value"]["status"] != "complete" {
            bail!("capture clip is not complete");
        }
        let path = PathBuf::from(
            row["value"]["path"]
                .as_str()
                .context("capture clip has no path")?,
        )
        .canonicalize()?;
        if !path.starts_with(self.root.as_ref()) || !path.is_file() {
            bail!("capture clip path is invalid");
        }
        Ok(path)
    }

    pub fn delete(&self, id: i64) -> Result<bool> {
        let row = self
            .metadata
            .clip(id)?
            .context("capture clip does not exist")?;
        if row["value"]["status"] == "capturing" {
            bail!("an active capture clip cannot be deleted");
        }
        if let Some(path) = row["value"]["path"].as_str() {
            let path = PathBuf::from(path);
            if let Ok(canonical) = path.canonicalize()
                && canonical.starts_with(self.root.as_ref())
            {
                fs::remove_file(canonical)?;
            }
        }
        self.metadata.delete_clip(id)
    }

    fn finalize_async(&self, clip: ActiveClip) {
        let root = self.root.clone();
        let metadata = self.metadata.clone();
        thread::spawn(move || {
            if let Err(error) = finalize(&root, &metadata, &clip) {
                let _ = metadata.update_clip(
                    clip.id,
                    &json!({
                        "status": "failed", "name": clip.name, "note": clip.note,
                        "trigger_ns": clip.trigger_ns, "error": error.to_string()
                    }),
                );
            }
        });
    }
}

fn finalize(root: &Path, metadata: &Metadata, clip: &ActiveClip) -> Result<()> {
    if clip.records.is_empty() {
        bail!("capture clip contains no complete USB records");
    }
    let raw = clip
        .records
        .iter()
        .flat_map(|record| record.raw.iter().copied())
        .collect::<Vec<_>>();
    require_storage_headroom(root, raw.len() as u64 + 64 * 1024)?;
    let raw_sha256 = format!("{:x}", Sha256::digest(&raw));
    let first = clip.records.first().unwrap();
    let last = clip.records.last().unwrap();
    let manifest = json!({
        "format": "heimdall-capture-clip-v1",
        "clip_id": clip.id,
        "name": clip.name,
        "note": clip.note,
        "trigger_ns": clip.trigger_ns,
        "duration_s": clip.duration_s,
        "first_record_ns": first.received_ns,
        "last_record_ns": last.received_ns,
        "first_sequence": first.sequence,
        "last_sequence": last.sequence,
        "records": clip.records.len(),
        "raw_bytes": raw.len(),
        "raw_sha256": raw_sha256,
        "aligned_cir_records": clip.cir_samples.len(),
        "complete_usb_record_boundaries": true,
        "compression": "stored"
    });
    let mut aligned_cir_bytes = Vec::new();
    for sample in &clip.cir_samples {
        serde_json::to_writer(&mut aligned_cir_bytes, sample)?;
        aligned_cir_bytes.push(b'\n');
    }
    let aligned_cir_sha256 = format!("{:x}", Sha256::digest(&aligned_cir_bytes));
    let temporary = root.join(format!("clip-{:06}.open", clip.id));
    let final_path = root.join(format!("clip-{:06}.zip", clip.id));
    let mut zip = ZipWriter::new(File::create(&temporary)?);
    let options = SimpleFileOptions::default().compression_method(CompressionMethod::Stored);
    zip.start_file("capture.husb", options)?;
    zip.write_all(&raw)?;
    if !aligned_cir_bytes.is_empty() {
        zip.start_file("aligned-cirs.ndjson", options)?;
        zip.write_all(&aligned_cir_bytes)?;
    }
    zip.start_file("manifest.json", options)?;
    zip.write_all(&serde_json::to_vec_pretty(&manifest)?)?;
    zip.start_file("metadata.json", options)?;
    zip.write_all(&serde_json::to_vec_pretty(&clip.context)?)?;
    zip.start_file("sha256.txt", options)?;
    writeln!(zip, "{}  capture.husb", raw_sha256)?;
    if !aligned_cir_bytes.is_empty() {
        writeln!(zip, "{}  aligned-cirs.ndjson", aligned_cir_sha256)?;
    }
    let file = zip.finish()?;
    file.sync_all()?;
    fs::rename(&temporary, &final_path)?;
    let final_path = final_path.canonicalize()?;
    let zip_sha256 = hash_file(&final_path)?;
    metadata.update_clip(
        clip.id,
        &json!({
            "status": "complete", "name": clip.name, "note": clip.note,
            "trigger_ns": clip.trigger_ns, "completed_ns": last.received_ns,
            "path": final_path, "bytes": fs::metadata(&final_path)?.len(),
            "sha256": zip_sha256, "manifest": manifest
        }),
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use heimdall_protocol::{RecordKind, encode_record};
    use std::{io::Read, time::Duration};

    #[test]
    fn clip_uses_complete_records_and_is_downloadable() {
        let dir = tempfile::tempdir().unwrap();
        let metadata = Metadata::open(dir.path().join("clips.db")).unwrap();
        let manager = ClipManager::new(dir.path().join("clips"), metadata.clone()).unwrap();
        let before = encode_record(RecordKind::Heartbeat, 0, 1, &[0; 12]).unwrap();
        let after = encode_record(RecordKind::Error, 0, 2, &[0; 8]).unwrap();
        manager.ingest(-1_000_000_000, &before);
        let started = manager
            .start(&json!({"name": "test"}), json!({"epoch": 1}), 0)
            .unwrap();
        let id = started["id"].as_i64().unwrap();
        assert_eq!(started["value"]["pre_seconds"].as_i64(), None);
        assert_eq!(started["value"]["duration_s"].as_i64(), Some(10));
        manager.ingest(DEFAULT_CLIP_DURATION_S * 500_000_000, &after);
        manager.ingest(DEFAULT_CLIP_DURATION_S * 1_000_000_000 + 1_000_000_000, &before);
        for _ in 0..100 {
            if metadata.clip(id).unwrap().unwrap()["value"]["status"] == "complete" {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        let path = manager.path(id).unwrap();
        let mut zip = zip::ZipArchive::new(File::open(path).unwrap()).unwrap();
        let mut raw = Vec::new();
        zip.by_name("capture.husb")
            .unwrap()
            .read_to_end(&mut raw)
            .unwrap();
        // The ring buffer was removed: only records received at or after the
        // trigger are captured, so `before` (received before the trigger) is
        // excluded even though it was ingested first.
        assert_eq!(raw, [after].concat());
        assert!(manager.delete(id).unwrap());
        assert!(metadata.clip(id).unwrap().is_none());
    }

    #[test]
    fn clip_stores_aligned_cir_samples() {
        let dir = tempfile::tempdir().unwrap();
        let metadata = Metadata::open(dir.path().join("clips.db")).unwrap();
        let manager = ClipManager::new(dir.path().join("clips"), metadata.clone()).unwrap();
        let started = manager
            .start(&json!({"name": "test"}), json!({"epoch": 1}), 0)
            .unwrap();
        let id = started["id"].as_i64().unwrap();
        assert!(manager.has_active_clips());
        let sample = AlignedCirSample {
            from: 0,
            to: 1,
            round: 7,
            event_s: 1.5,
            evidence: 42,
            delay_samples: 2.25,
            common_phase_rad: 0.5,
            correlation: 0.9,
            quality: 3,
            marker_raw: 12.0,
            marker_aligned: 9.75,
            fit_algorithm: "robust_grid".to_owned(),
            magnitude: vec![1.0, 0.5],
            iq: vec![[1.0, 0.0], [0.5, -0.25]],
        };
        manager.ingest_cir(DEFAULT_CLIP_DURATION_S * 500_000_000, vec![sample.clone()]);
        manager.ingest_cir(
            DEFAULT_CLIP_DURATION_S * 1_000_000_000 + 1_000_000_000,
            vec![sample],
        );
        let record = encode_record(RecordKind::Heartbeat, 0, 1, &[0; 12]).unwrap();
        manager.ingest(DEFAULT_CLIP_DURATION_S * 500_000_000, &record);
        manager.ingest(DEFAULT_CLIP_DURATION_S * 1_000_000_000 + 1_000_000_000, &record);
        for _ in 0..100 {
            if metadata.clip(id).unwrap().unwrap()["value"]["status"] == "complete" {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(
            metadata.clip(id).unwrap().unwrap()["value"]["manifest"]["aligned_cir_records"]
                .as_i64(),
            Some(1)
        );
        let path = manager.path(id).unwrap();
        let mut zip = zip::ZipArchive::new(File::open(path).unwrap()).unwrap();
        let mut ndjson = String::new();
        zip.by_name("aligned-cirs.ndjson")
            .unwrap()
            .read_to_string(&mut ndjson)
            .unwrap();
        let first: Value = ndjson.lines().next().map(|line| serde_json::from_str(line).unwrap()).unwrap();
        assert_eq!(first["from"].as_u64(), Some(0));
        assert_eq!(first["to"].as_u64(), Some(1));
        assert_eq!(first["round"].as_u64(), Some(7));
        assert_eq!(first["magnitude"][1].as_f64(), Some(0.5));
        assert_eq!(first["iq"][1][0].as_f64(), Some(0.5));
        assert!(manager.delete(id).unwrap());
    }

    #[test]
    fn clip_honors_requested_duration_with_clamping() {
        let dir = tempfile::tempdir().unwrap();
        let start = |request: Value| -> Value {
            let metadata = Metadata::open(dir.path().join("clips.db")).unwrap();
            let manager = ClipManager::new(dir.path().join("clips"), metadata.clone()).unwrap();
            manager
                .start(&request, json!({"epoch": 1}), 0)
                .unwrap()
        };
        assert_eq!(start(json!({"name": "t", "duration_s": 25}))["value"]["duration_s"].as_i64(), Some(25));
        assert_eq!(start(json!({"name": "t", "duration_s": 3600}))["value"]["duration_s"].as_i64(), Some(60));
        assert_eq!(start(json!({"name": "t"}))["value"]["duration_s"].as_i64(), Some(10));
    }
}
