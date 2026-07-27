use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};

use crate::metadata::{Metadata, SegmentRow, now_ns};

pub const SEGMENT_BYTES: u64 = 8 * 1024 * 1024;
pub const CIRCULAR_QUOTA_BYTES: u64 = 200_000_000;

pub struct ArchiveWriter {
    root: PathBuf,
    metadata: Arc<Metadata>,
    connection_id: Option<i64>,
    rotate_bytes: u64,
    quota_bytes: u64,
    sequence: u64,
    current: Option<OpenSegment>,
}

struct OpenSegment {
    path: PathBuf,
    file: File,
    bytes: u64,
    last_sync: Instant,
}

impl ArchiveWriter {
    pub fn new(
        root: impl AsRef<Path>,
        metadata: Arc<Metadata>,
        connection_id: Option<i64>,
    ) -> Result<Self> {
        Self::with_limits(
            root,
            metadata,
            connection_id,
            SEGMENT_BYTES,
            CIRCULAR_QUOTA_BYTES,
        )
    }

    pub fn with_limits(
        root: impl AsRef<Path>,
        metadata: Arc<Metadata>,
        connection_id: Option<i64>,
        rotate_bytes: u64,
        quota_bytes: u64,
    ) -> Result<Self> {
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(&root)?;
        let root = root.canonicalize()?;
        let mut writer = Self {
            sequence: next_sequence(&root)?,
            root,
            metadata,
            connection_id,
            rotate_bytes: rotate_bytes.max(1),
            quota_bytes,
            current: None,
        };
        writer.recover()?;
        writer.enforce_quota()?;
        Ok(writer)
    }

    pub fn write(&mut self, mut bytes: &[u8]) -> Result<()> {
        self.check_free_space()?;
        while !bytes.is_empty() {
            if self.current.is_none() {
                self.open_segment()?;
            }
            let segment = self.current.as_mut().unwrap();
            let room = (self.rotate_bytes - segment.bytes) as usize;
            let count = room.min(bytes.len());
            segment.file.write_all(&bytes[..count])?;
            segment.bytes += count as u64;
            if segment.last_sync.elapsed() >= Duration::from_secs(1) {
                segment.file.sync_data()?;
                segment.last_sync = Instant::now();
            }
            bytes = &bytes[count..];
            if segment.bytes == self.rotate_bytes {
                self.close_segment()?;
            }
        }
        Ok(())
    }

    pub fn finish(&mut self) -> Result<()> {
        if self.current.is_some() {
            self.close_segment()?;
        }
        Ok(())
    }

    fn open_segment(&mut self) -> Result<()> {
        let path = self
            .root
            .join(format!("segment-{:020}.open", self.sequence));
        self.sequence += 1;
        let file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&path)?;
        self.current = Some(OpenSegment {
            path,
            file,
            bytes: 0,
            last_sync: Instant::now(),
        });
        Ok(())
    }

    fn close_segment(&mut self) -> Result<()> {
        let segment = self.current.take().unwrap();
        segment.file.sync_data()?;
        drop(segment.file);
        let closed = segment.path.with_extension("husb");
        fs::rename(&segment.path, &closed)?;
        let row = segment_row(&closed)?;
        self.metadata.catalog_segment(self.connection_id, &row)?;
        self.enforce_quota()?;
        Ok(())
    }

    fn recover(&mut self) -> Result<()> {
        for entry in fs::read_dir(&self.root)? {
            let path = entry?.path();
            if path.extension().is_some_and(|value| value == "open") {
                OpenOptions::new().write(true).open(&path)?.sync_data()?;
                let closed = path.with_extension("husb");
                fs::rename(&path, &closed)?;
                self.metadata
                    .catalog_segment(self.connection_id, &segment_row(&closed)?)?;
            } else if path.extension().is_some_and(|value| value == "husb") {
                self.metadata
                    .catalog_segment(self.connection_id, &segment_row(&path)?)?;
            }
        }
        Ok(())
    }

    fn enforce_quota(&self) -> Result<()> {
        let mut rows = self
            .metadata
            .verified_segments()?
            .into_iter()
            .filter(|row| Path::new(&row.path).starts_with(&self.root))
            .collect::<Vec<_>>();
        let mut total: u64 = rows.iter().map(|row| row.bytes).sum();
        for row in rows.drain(..) {
            if total <= self.quota_bytes {
                break;
            }
            let path = PathBuf::from(&row.path);
            if path.starts_with(&self.root) && path.is_file() && hash_file(&path)? == row.sha256 {
                fs::remove_file(&path)?;
                self.metadata.mark_segment_deleted(&row.path)?;
                total = total.saturating_sub(row.bytes);
            }
        }
        Ok(())
    }

    fn check_free_space(&self) -> Result<()> {
        let total = fs2::total_space(&self.root).context("read archive filesystem size")?;
        let available =
            fs2::available_space(&self.root).context("read archive filesystem free space")?;
        if below_storage_floor(total, available, 0) {
            self.enforce_quota()?;
            let available = fs2::available_space(&self.root)
                .context("re-read archive filesystem free space")?;
            if below_storage_floor(total, available, 0) {
                bail!("archive filesystem has less than 20% free space");
            }
        }
        Ok(())
    }
}

pub(crate) fn require_storage_headroom(path: &Path, reserve_bytes: u64) -> Result<()> {
    let total = fs2::total_space(path).context("read filesystem size")?;
    let available = fs2::available_space(path).context("read filesystem free space")?;
    if below_storage_floor(total, available, reserve_bytes) {
        bail!("filesystem would have less than 20% free space");
    }
    Ok(())
}

fn below_storage_floor(total: u64, available: u64, reserve_bytes: u64) -> bool {
    total > 0 && available.saturating_sub(reserve_bytes).saturating_mul(5) < total
}

impl Drop for ArchiveWriter {
    fn drop(&mut self) {
        let _ = self.finish();
    }
}

pub fn verify_archive(root: impl AsRef<Path>, metadata: &Metadata) -> Result<Vec<SegmentRow>> {
    let root = root.as_ref().canonicalize()?;
    let paths = ordered_segments(&root)?
        .into_iter()
        .map(|path| path.canonicalize())
        .collect::<std::io::Result<Vec<_>>>()?;
    let rows = metadata
        .verified_segments()?
        .into_iter()
        .filter(|row| Path::new(&row.path).starts_with(&root))
        .collect::<Vec<_>>();
    if paths.len() != rows.len()
        || paths
            .iter()
            .any(|path| !rows.iter().any(|row| Path::new(&row.path) == path))
    {
        bail!("archive files and verified SHA256 catalog differ");
    }
    for row in &rows {
        let path = PathBuf::from(&row.path);
        let actual = hash_file(&path).with_context(|| format!("verify {}", path.display()))?;
        if actual != row.sha256 {
            bail!("SHA256 mismatch for {}", path.display());
        }
        if fs::metadata(&path)?.len() != row.bytes {
            bail!("length mismatch for {}", path.display());
        }
    }
    Ok(rows)
}

pub fn ordered_segments(root: impl AsRef<Path>) -> Result<Vec<PathBuf>> {
    let mut paths = fs::read_dir(root)?
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.extension().is_some_and(|ext| ext == "husb"))
        .collect::<Vec<_>>();
    paths.sort();
    Ok(paths)
}

pub fn hash_file(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0; 64 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn segment_row(path: &Path) -> Result<SegmentRow> {
    Ok(SegmentRow {
        path: path.canonicalize()?.to_string_lossy().into_owned(),
        bytes: fs::metadata(path)?.len(),
        sha256: hash_file(path)?,
        closed_ns: now_ns(),
    })
}

fn next_sequence(root: &Path) -> Result<u64> {
    Ok(fs::read_dir(root)?
        .filter_map(|entry| entry.ok())
        .filter_map(|entry| entry.file_name().to_str().map(str::to_owned))
        .filter_map(|name| {
            name.strip_prefix("segment-")?
                .split('.')
                .next()?
                .parse()
                .ok()
        })
        .max()
        .map_or(0, |value: u64| value + 1))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn storage_floor_includes_reserved_write_size() {
        assert!(!below_storage_floor(1_000, 300, 100));
        assert!(below_storage_floor(1_000, 299, 100));
        assert!(below_storage_floor(1_000, 100, 200));
    }

    #[test]
    fn rotation_hash_recovery_and_quota() {
        let dir = tempfile::tempdir().unwrap();
        let db = Metadata::open(dir.path().join("catalog.db")).unwrap();
        let raw = dir.path().join("raw");
        let mut archive = ArchiveWriter::with_limits(&raw, db.clone(), None, 4, 12).unwrap();
        archive.write(b"abcdefghijkl").unwrap();
        archive.finish().unwrap();
        let paths = ordered_segments(&raw).unwrap();
        assert_eq!(paths.len(), 3);
        let exact = paths
            .iter()
            .flat_map(|path| fs::read(path).unwrap())
            .collect::<Vec<_>>();
        assert_eq!(exact, b"abcdefghijkl");
        assert_eq!(verify_archive(&raw, &db).unwrap().len(), 3);

        drop(ArchiveWriter::with_limits(&raw, db.clone(), None, 4, 8).unwrap());
        assert_eq!(ordered_segments(&raw).unwrap().len(), 2);

        fs::write(raw.join("segment-99999999999999999999.open"), b"tail").unwrap();
        drop(ArchiveWriter::with_limits(&raw, db.clone(), None, 4, 12).unwrap());
        assert!(raw.join("segment-99999999999999999999.husb").is_file());
        verify_archive(&raw, &db).unwrap();

        let first = ordered_segments(&raw).unwrap().remove(0);
        fs::write(first, b"tampered").unwrap();
        assert!(verify_archive(&raw, &db).is_err());
    }
}
