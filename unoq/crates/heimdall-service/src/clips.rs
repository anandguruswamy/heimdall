use std::{
    collections::VecDeque,
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
};

const CLIP_WINDOW_NS: i64 = 30_000_000_000;
const MAX_ACTIVE_CLIPS: usize = 2;

#[derive(Clone)]
pub struct ClipManager {
    root: Arc<PathBuf>,
    metadata: Arc<Metadata>,
    state: Arc<Mutex<ClipState>>,
}

struct ClipState {
    parser: StreamParser,
    history: VecDeque<TimedRecord>,
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
    name: String,
    note: String,
    context: Value,
    records: Vec<TimedRecord>,
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
                history: VecDeque::new(),
                active: Vec::new(),
            })),
        })
    }

    pub fn reset_stream(&self) {
        let active = {
            let mut state = self.state.lock();
            state.parser = StreamParser::default();
            state.history.clear();
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
        let mut state = self.state.lock();
        if state.active.len() >= MAX_ACTIVE_CLIPS {
            bail!("at most {MAX_ACTIVE_CLIPS} capture clips may be active");
        }
        let initial = json!({
            "status": "capturing", "name": name, "note": note,
            "trigger_ns": trigger_ns, "deadline_ns": trigger_ns + CLIP_WINDOW_NS,
            "pre_seconds": 30, "post_seconds": 30
        });
        let row = self.metadata.add_clip(&initial)?;
        let id = row["id"].as_i64().context("clip row has no id")?;
        let records = state
            .history
            .iter()
            .filter(|record| record.received_ns >= trigger_ns - CLIP_WINDOW_NS)
            .cloned()
            .collect();
        state.active.push(ActiveClip {
            id,
            trigger_ns,
            deadline_ns: trigger_ns + CLIP_WINDOW_NS,
            name: name.to_owned(),
            note: note.to_owned(),
            context,
            records,
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
                state.history.push_back(record.clone());
                for clip in &mut state.active {
                    if record.received_ns <= clip.deadline_ns {
                        clip.records.push(record.clone());
                    }
                }
            }
            while state
                .history
                .front()
                .is_some_and(|record| received_ns - record.received_ns > CLIP_WINDOW_NS)
            {
                state.history.pop_front();
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
        "first_record_ns": first.received_ns,
        "last_record_ns": last.received_ns,
        "first_sequence": first.sequence,
        "last_sequence": last.sequence,
        "records": clip.records.len(),
        "raw_bytes": raw.len(),
        "raw_sha256": raw_sha256,
        "complete_usb_record_boundaries": true,
        "compression": "stored"
    });
    let temporary = root.join(format!("clip-{:06}.open", clip.id));
    let final_path = root.join(format!("clip-{:06}.zip", clip.id));
    let mut zip = ZipWriter::new(File::create(&temporary)?);
    let options = SimpleFileOptions::default().compression_method(CompressionMethod::Stored);
    zip.start_file("capture.husb", options)?;
    zip.write_all(&raw)?;
    zip.start_file("manifest.json", options)?;
    zip.write_all(&serde_json::to_vec_pretty(&manifest)?)?;
    zip.start_file("metadata.json", options)?;
    zip.write_all(&serde_json::to_vec_pretty(&clip.context)?)?;
    zip.start_file("sha256.txt", options)?;
    writeln!(zip, "{}  capture.husb", raw_sha256)?;
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
        manager.ingest(CLIP_WINDOW_NS, &after);
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
        assert_eq!(raw, [before, after].concat());
        assert!(manager.delete(id).unwrap());
        assert!(metadata.clip(id).unwrap().is_none());
    }
}
