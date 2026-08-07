use std::{
    ffi::OsString,
    fs::{self, File},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{self, Receiver, Sender},
    },
    thread,
    time::Duration,
};

use anyhow::{Context, Result, bail};
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};

use crate::metadata::now_ns;

#[cfg(windows)]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn MoveFileExW(existing: *const u16, new: *const u16, flags: u32) -> i32;
}

pub const CAMERA_WIDTH: u32 = 1280;
pub const CAMERA_HEIGHT: u32 = 720;
pub const CAMERA_FPS: u32 = 30;
const STOP_TIMEOUT: Duration = Duration::from_secs(5);
const PREVIEW_FPS: u32 = 2;

#[derive(Clone, Debug)]
pub struct CameraConfig {
    pub device: String,
    pub ffmpeg: PathBuf,
}

#[derive(Clone)]
pub struct CameraManager {
    inner: Arc<CameraInner>,
}

struct CameraInner {
    root: PathBuf,
    config: Option<CameraConfig>,
    state: Mutex<CameraState>,
    preview_available: AtomicBool,
    next_id: AtomicU64,
}

#[derive(Default)]
struct CameraState {
    active: Option<ActiveSession>,
    error: Option<String>,
}

struct ActiveSession {
    manifest: CameraSession,
    commands: Sender<WorkerCommand>,
}

enum WorkerCommand {
    Stop(Sender<Result<()>>),
}

#[derive(Clone, Debug, Serialize)]
pub struct CameraStatus {
    pub enabled: bool,
    pub device: Option<String>,
    pub width: u32,
    pub height: u32,
    pub fps: u32,
    pub state: &'static str,
    pub session: Option<CameraSession>,
    pub preview_available: bool,
    pub error: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CameraSession {
    pub id: String,
    pub person: String,
    pub consent: bool,
    pub status: String,
    pub started_ns: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stopped_ns: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub video_path: String,
    pub preview_path: String,
    pub ffmpeg_log_path: String,
    pub events: Vec<CameraEvent>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CameraEvent {
    pub kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub seat: Option<SeatClass>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    pub timestamp_ns: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub calibration_path: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
pub enum SeatClass {
    FrontLeft,
    FrontRight,
    BackRight,
    BackLeft,
    Empty,
}

impl std::fmt::Display for SeatClass {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{self:?}")
    }
}

#[derive(Debug, Deserialize)]
pub struct StartCameraSession {
    pub person: String,
    pub consent: bool,
}

#[derive(Debug, Deserialize)]
pub struct AddCameraEvent {
    pub kind: String,
    pub seat: Option<SeatClass>,
    pub note: Option<String>,
}

impl CameraManager {
    pub fn disabled(root: impl AsRef<Path>) -> Result<Self> {
        Self::new(root, None)
    }

    pub fn enabled(root: impl AsRef<Path>, config: CameraConfig) -> Result<Self> {
        if config.device.trim().is_empty() {
            bail!("camera device must be nonblank");
        }
        Self::new(root, Some(config))
    }

    fn new(root: impl AsRef<Path>, config: Option<CameraConfig>) -> Result<Self> {
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(&root)?;
        recover_interrupted_sessions(&root)?;
        reap_stale_ffmpeg(&root);
        Ok(Self {
            inner: Arc::new(CameraInner {
                root,
                config,
                state: Mutex::new(CameraState::default()),
                preview_available: AtomicBool::new(false),
                next_id: AtomicU64::new(0),
            }),
        })
    }

    pub fn status(&self) -> CameraStatus {
        let state = self.inner.state.lock();
        let current = state.active.as_ref().map(|active| active.manifest.clone());
        let preview_available =
            current.is_some() && self.inner.preview_available.load(Ordering::Relaxed);
        CameraStatus {
            enabled: self.inner.config.is_some(),
            device: self
                .inner
                .config
                .as_ref()
                .map(|config| config.device.clone()),
            width: CAMERA_WIDTH,
            height: CAMERA_HEIGHT,
            fps: CAMERA_FPS,
            state: if self.inner.config.is_none() {
                "disabled"
            } else if state.active.is_some() {
                "recording"
            } else if state.error.is_some() {
                "error"
            } else {
                "idle"
            },
            session: current,
            preview_available,
            error: state.error.clone(),
        }
    }

    pub fn start(&self, request: StartCameraSession) -> Result<CameraSession> {
        let config = self.inner.config.as_ref().context("camera is disabled")?;
        let person = request.person.trim();
        if person.is_empty() || person.chars().count() > 60 {
            bail!("person must be nonblank and at most 60 characters");
        }
        if !request.consent {
            bail!("camera recording requires consent=true");
        }

        let mut state = self.inner.state.lock();
        if state.active.is_some() {
            bail!("a camera session is already recording");
        }
        self.inner.preview_available.store(false, Ordering::Relaxed);
        let started_ns = now_ns();
        let sequence = self.inner.next_id.fetch_add(1, Ordering::Relaxed);
        let id = format!("{started_ns}-{:04}-{:04}", std::process::id(), sequence);
        let session_dir = self.inner.root.join(&id);
        fs::create_dir(&session_dir)?;
        let mut manifest = CameraSession {
            id: id.clone(),
            person: person.to_owned(),
            consent: true,
            status: "recording".to_owned(),
            started_ns,
            stopped_ns: None,
            error: None,
            video_path: "video.mp4".to_owned(),
            preview_path: "preview.jpg".to_owned(),
            ffmpeg_log_path: "ffmpeg.log".to_owned(),
            events: Vec::new(),
        };
        persist_manifest(&session_dir, &manifest)?;

        let log = match File::create(session_dir.join("ffmpeg.log")) {
            Ok(log) => log,
            Err(error) => {
                manifest.status = "failed".to_owned();
                manifest.stopped_ns = Some(now_ns());
                manifest.error = Some(format!("failed to create FFmpeg log: {error}"));
                persist_manifest(&session_dir, &manifest)?;
                state.error = manifest.error.clone();
                return Err(error).context("create FFmpeg camera log");
            }
        };
        let mut command = ffmpeg_command(config, &session_dir);
        let child = match command.stderr(Stdio::from(log)).spawn() {
            Ok(child) => child,
            Err(error) => {
                manifest.status = "failed".to_owned();
                manifest.stopped_ns = Some(now_ns());
                manifest.error = Some(format!("failed to start FFmpeg: {error}"));
                persist_manifest(&session_dir, &manifest)?;
                state.error = manifest.error.clone();
                return Err(error).context("start FFmpeg camera process");
            }
        };
        let (commands, receiver) = mpsc::channel();
        state.error = None;
        state.active = Some(ActiveSession {
            manifest: manifest.clone(),
            commands,
        });
        drop(state);

        let manager = self.clone();
        thread::spawn(move || camera_worker(manager, id, child, receiver));
        Ok(manifest)
    }

    pub fn sessions(&self) -> Result<Vec<CameraSession>> {
        let mut sessions: Vec<CameraSession> = Vec::new();
        for entry in fs::read_dir(&self.inner.root)? {
            let entry = entry?;
            if !entry.file_type()?.is_dir() {
                continue;
            }
            let path = entry.path().join("session.json");
            if path.is_file() {
                sessions.push(serde_json::from_slice(&fs::read(&path)?)?);
            }
        }
        sessions.sort_by_key(|session| std::cmp::Reverse(session.started_ns));
        Ok(sessions)
    }

    pub fn add_event(&self, id: &str, request: AddCameraEvent) -> Result<CameraSession> {
        validate_id(id)?;
        let kind = request.kind.trim();
        if kind.is_empty() || kind.chars().count() > 60 {
            bail!("event kind must be nonblank and at most 60 characters");
        }
        let note = request.note.map(|note| note.trim().to_owned());
        if note.as_ref().is_some_and(|note| note.chars().count() > 500) {
            bail!("event note is limited to 500 characters");
        }
        if kind == "calibration_sample" && request.seat.is_none() {
            bail!("calibration_sample requires a seat");
        }

        let mut state = self.inner.state.lock();
        let active = state
            .active
            .as_mut()
            .context("camera session is not active")?;
        if active.manifest.id != id {
            bail!("camera session is not active");
        }
        let calibration_path = if kind == "calibration_sample" {
            let seat = request.seat.context("calibration_sample requires a seat")?;
            let source = self.session_path(id, "preview.jpg");
            if !source.is_file() {
                bail!("camera preview is not available");
            }
            let name = format!("calibration-{seat}.jpg");
            fs::copy(source, self.session_path(id, &name))?;
            Some(name)
        } else {
            None
        };
        active.manifest.events.push(CameraEvent {
            kind: kind.to_owned(),
            seat: request.seat,
            note,
            timestamp_ns: now_ns(),
            calibration_path,
        });
        persist_manifest(&self.inner.root.join(id), &active.manifest)?;
        Ok(active.manifest.clone())
    }

    pub fn stop(&self, id: &str) -> Result<CameraSession> {
        validate_id(id)?;
        let (commands, manifest) = {
            let state = self.inner.state.lock();
            let active = state
                .active
                .as_ref()
                .context("camera session is not active")?;
            if active.manifest.id != id {
                bail!("camera session is not active");
            }
            (active.commands.clone(), active.manifest.clone())
        };
        let (done_tx, done_rx) = mpsc::channel();
        commands.send(WorkerCommand::Stop(done_tx))?;
        done_rx
            .recv_timeout(STOP_TIMEOUT + Duration::from_secs(2))
            .context("camera worker did not stop")??;
        self.read_session(&manifest.id)
    }

    pub fn stop_active(&self) {
        let id = self
            .inner
            .state
            .lock()
            .active
            .as_ref()
            .map(|active| active.manifest.id.clone());
        if let Some(id) = id
            && let Err(error) = self.stop(&id)
        {
            tracing::error!(%error, session_id=%id, "failed to stop camera session");
        }
    }

    pub fn preview_path(&self) -> Result<PathBuf> {
        let state = self.inner.state.lock();
        let active = state
            .active
            .as_ref()
            .context("camera session is not active")?;
        let path = self.session_path(&active.manifest.id, "preview.jpg");
        path.is_file()
            .then_some(path)
            .context("camera preview is not available")
    }

    pub fn video_path(&self, id: &str) -> Result<PathBuf> {
        let session = self.read_session(id)?;
        if session.status == "recording" {
            bail!("camera session is still recording");
        }
        checked_file(&self.inner.root, id, "video.mp4")
    }

    pub fn delete(&self, id: &str) -> Result<bool> {
        validate_id(id)?;
        if self
            .inner
            .state
            .lock()
            .active
            .as_ref()
            .is_some_and(|active| active.manifest.id == id)
        {
            bail!("an active camera session cannot be deleted");
        }
        let path = self.inner.root.join(id);
        if !path.is_dir() {
            return Ok(false);
        }
        self.read_session(id)?;
        fs::remove_dir_all(path)?;
        Ok(true)
    }

    pub fn clip_context(&self, trigger_ns: i64) -> Option<serde_json::Value> {
        let state = self.inner.state.lock();
        let session = &state.active.as_ref()?.manifest;
        Some(serde_json::json!({
            "session_id": session.id,
            "clip_trigger_ns": trigger_ns,
            "clip_offset_ns": trigger_ns.saturating_sub(session.started_ns),
            "session_started_ns": session.started_ns
        }))
    }

    fn read_session(&self, id: &str) -> Result<CameraSession> {
        validate_id(id)?;
        let path = self.inner.root.join(id).join("session.json");
        if !path.is_file() {
            bail!("camera session does not exist");
        }
        Ok(serde_json::from_slice(&fs::read(path)?)?)
    }

    fn session_path(&self, id: &str, name: &str) -> PathBuf {
        self.inner.root.join(id).join(name)
    }
}

fn ffmpeg_command(config: &CameraConfig, session_dir: &Path) -> Command {
    let mut command = Command::new(&config.ffmpeg);
    command
        .args(ffmpeg_args(&config.device, session_dir))
        .stdin(Stdio::piped())
        .stdout(Stdio::null());
    command
}

fn ffmpeg_args(device: &str, session_dir: &Path) -> Vec<OsString> {
    [
        "-hide_banner".into(),
        "-nostats".into(),
        "-f".into(),
        "dshow".into(),
        "-video_size".into(),
        format!("{CAMERA_WIDTH}x{CAMERA_HEIGHT}").into(),
        "-framerate".into(),
        CAMERA_FPS.to_string().into(),
        "-vcodec".into(),
        "mjpeg".into(),
        "-i".into(),
        format!("video={device}").into(),
        "-filter_complex".into(),
        format!("[0:v]split=2[record][preview];[preview]fps={PREVIEW_FPS}[previewout]").into(),
        "-map".into(),
        "[record]".into(),
        "-c:v".into(),
        "libx264".into(),
        "-preset".into(),
        "veryfast".into(),
        "-pix_fmt".into(),
        "yuv420p".into(),
        "-movflags".into(),
        "+frag_keyframe+empty_moov".into(),
        session_dir.join("video.mp4").into_os_string(),
        "-map".into(),
        "[previewout]".into(),
        "-q:v".into(),
        "3".into(),
        "-f".into(),
        "image2".into(),
        "-update".into(),
        "1".into(),
        "-atomic_writing".into(),
        "1".into(),
        session_dir.join("preview.jpg").into_os_string(),
    ]
    .into_iter()
    .collect()
}

fn camera_worker(
    manager: CameraManager,
    id: String,
    mut child: Child,
    commands: Receiver<WorkerCommand>,
) {
    loop {
        manager.inner.preview_available.store(
            manager.session_path(&id, "preview.jpg").is_file(),
            Ordering::Relaxed,
        );
        match child.try_wait() {
            Ok(Some(status)) => {
                let detail = ffmpeg_log_tail(&manager.inner.root.join(&id));
                let message = if detail.is_empty() {
                    format!("FFmpeg exited unexpectedly with {status}")
                } else {
                    format!("FFmpeg exited unexpectedly with {status}: {detail}")
                };
                manager.finish_worker(&id, Some(message));
                return;
            }
            Err(error) => {
                manager.finish_worker(&id, Some(format!("failed to query FFmpeg: {error}")));
                return;
            }
            Ok(None) => {}
        }
        match commands.recv_timeout(Duration::from_millis(100)) {
            Ok(WorkerCommand::Stop(done)) => {
                let result = stop_child(&mut child);
                manager.finish_worker(&id, result.as_ref().err().map(ToString::to_string));
                let _ = done.send(result);
                return;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                let result = stop_child(&mut child);
                manager.finish_worker(&id, result.as_ref().err().map(ToString::to_string));
                return;
            }
        }
    }
}

impl CameraManager {
    fn finish_worker(&self, id: &str, error: Option<String>) {
        let mut state = self.inner.state.lock();
        let Some(active) = state
            .active
            .as_mut()
            .filter(|active| active.manifest.id == id)
        else {
            return;
        };
        active.manifest.stopped_ns = Some(now_ns());
        active.manifest.status = if error.is_some() {
            "failed"
        } else {
            "complete"
        }
        .to_owned();
        active.manifest.error = error.clone();
        if let Err(persist_error) = persist_manifest(&self.inner.root.join(id), &active.manifest) {
            tracing::error!(error=%persist_error, session_id=%id, "failed to persist camera manifest");
            state.error = Some(persist_error.to_string());
        } else {
            state.error = error;
        }
        state.active = None;
        self.inner.preview_available.store(false, Ordering::Relaxed);
    }
}

fn stop_child(child: &mut Child) -> Result<()> {
    if let Some(stdin) = child.stdin.as_mut() {
        if let Err(error) = stdin.write_all(b"q\n").and_then(|()| stdin.flush()) {
            tracing::warn!(%error, "failed to send graceful stop to FFmpeg");
        }
    }
    child.stdin.take();
    let deadline = std::time::Instant::now() + STOP_TIMEOUT;
    while std::time::Instant::now() < deadline {
        if child.try_wait()?.is_some() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(100));
    }
    child.kill()?;
    child.wait()?;
    Ok(())
}

fn ffmpeg_log_tail(session_dir: &Path) -> String {
    let path = session_dir.join("ffmpeg.log");
    let Ok(mut file) = File::open(&path) else {
        return String::new();
    };
    let Ok(metadata) = file.metadata() else {
        return String::new();
    };
    const MAX_TAIL_BYTES: u64 = 4 * 1024;
    let start = metadata.len().saturating_sub(MAX_TAIL_BYTES);
    if file.seek(SeekFrom::Start(start)).is_err() {
        return String::new();
    }
    let mut bytes = Vec::new();
    if file.read_to_end(&mut bytes).is_err() {
        return String::new();
    }
    let tail = String::from_utf8_lossy(&bytes).trim().to_owned();
    if tail.is_empty() {
        return String::new();
    }
    tail.lines()
        .rev()
        .take(6)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect()
}

fn reap_stale_ffmpeg(root: &Path) {
    // A previous server generation may have been force-killed with an FFmpeg
    // child still holding the camera device. Reap any ffmpeg.exe whose command
    // line references this camera-sessions root so the device is released and
    // a new session can open it. Windows-only: the camera manager is only
    // enabled on the Windows processing server.
    #[cfg(windows)]
    {
        let root_text = root.display().to_string();
        let script = format!(
            "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{root_text}') }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
        );
        let _ = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", &script])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

fn recover_interrupted_sessions(root: &Path) -> Result<()> {
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let path = entry.path().join("session.json");
        if !path.is_file() {
            continue;
        }
        let mut session: CameraSession = serde_json::from_slice(&fs::read(&path)?)?;
        if session.status == "recording" {
            session.status = "failed".to_owned();
            session.stopped_ns = Some(now_ns());
            session.error = Some("service restarted during camera recording".to_owned());
            persist_manifest(&entry.path(), &session)?;
        }
    }
    Ok(())
}

fn persist_manifest(session_dir: &Path, manifest: &CameraSession) -> Result<()> {
    let destination = session_dir.join("session.json");
    let temporary = session_dir.join("session.json.tmp");
    let mut file = File::create(&temporary)?;
    serde_json::to_writer_pretty(&mut file, manifest)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    atomic_replace(&temporary, &destination)
}

#[cfg(windows)]
fn atomic_replace(source: &Path, destination: &Path) -> Result<()> {
    use std::os::windows::ffi::OsStrExt;
    const MOVEFILE_REPLACE_EXISTING: u32 = 0x1;
    const MOVEFILE_WRITE_THROUGH: u32 = 0x8;
    let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let destination: Vec<u16> = destination
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    let result = unsafe {
        MoveFileExW(
            source.as_ptr(),
            destination.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if result == 0 {
        return Err(std::io::Error::last_os_error()).context("atomically replace camera manifest");
    }
    Ok(())
}

#[cfg(not(windows))]
fn atomic_replace(source: &Path, destination: &Path) -> Result<()> {
    fs::rename(source, destination)?;
    Ok(())
}

fn validate_id(id: &str) -> Result<()> {
    if id.is_empty()
        || id.len() > 80
        || !id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    {
        bail!("invalid camera session id");
    }
    Ok(())
}

fn checked_file(root: &Path, id: &str, name: &str) -> Result<PathBuf> {
    validate_id(id)?;
    let path = root.join(id).join(name).canonicalize()?;
    let root = root.canonicalize()?;
    if !path.starts_with(root) || !path.is_file() {
        bail!("invalid camera session path");
    }
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn session(id: &str, status: &str) -> CameraSession {
        CameraSession {
            id: id.to_owned(),
            person: "Test Person".to_owned(),
            consent: true,
            status: status.to_owned(),
            started_ns: 123,
            stopped_ns: None,
            error: None,
            video_path: "video.mp4".to_owned(),
            preview_path: "preview.jpg".to_owned(),
            ffmpeg_log_path: "ffmpeg.log".to_owned(),
            events: Vec::new(),
        }
    }

    #[test]
    fn builds_single_input_split_ffmpeg_command() {
        let args = ffmpeg_args("USB Camera Name", Path::new("camera-session"));
        let args = args
            .iter()
            .map(|arg| arg.to_string_lossy())
            .collect::<Vec<_>>();
        assert_eq!(args.iter().filter(|arg| arg.as_ref() == "-i").count(), 1);
        assert!(args.contains(&"video=USB Camera Name".into()));
        let codec = args
            .iter()
            .position(|arg| arg.as_ref() == "-vcodec")
            .unwrap();
        assert_eq!(args[codec + 1], "mjpeg");
        assert!(args.iter().any(|arg| arg.contains("split=2")));
        assert!(args.iter().any(|arg| arg.ends_with("video.mp4")));
        assert!(args.iter().any(|arg| arg.ends_with("preview.jpg")));
    }

    #[test]
    fn disabled_manager_reports_contract_status() {
        let dir = tempfile::tempdir().unwrap();
        let manager = CameraManager::disabled(dir.path().join("camera-sessions")).unwrap();
        let status = manager.status();
        assert!(!status.enabled);
        assert_eq!(status.state, "disabled");
        assert_eq!((status.width, status.height, status.fps), (1280, 720, 30));
        assert!(
            manager
                .start(StartCameraSession {
                    person: "Person".to_owned(),
                    consent: true,
                })
                .is_err()
        );
    }

    #[test]
    fn initialization_recovers_interrupted_manifest() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("camera-sessions");
        let session_dir = root.join("123-1-0");
        fs::create_dir_all(&session_dir).unwrap();
        persist_manifest(&session_dir, &session("123-1-0", "recording")).unwrap();

        let manager = CameraManager::disabled(&root).unwrap();
        let recovered = manager.sessions().unwrap().pop().unwrap();
        assert_eq!(recovered.status, "failed");
        assert!(recovered.stopped_ns.is_some());
        assert!(recovered.error.unwrap().contains("restarted"));
    }

    #[test]
    fn calibration_event_copies_preview_and_persists_host_timestamp() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("camera-sessions");
        let manager = CameraManager::disabled(&root).unwrap();
        let id = "123-1-0";
        let session_dir = root.join(id);
        fs::create_dir(&session_dir).unwrap();
        fs::write(session_dir.join("preview.jpg"), b"jpeg bytes").unwrap();
        let manifest = session(id, "recording");
        persist_manifest(&session_dir, &manifest).unwrap();
        let (commands, _receiver) = mpsc::channel();
        manager.inner.state.lock().active = Some(ActiveSession { manifest, commands });

        let updated = manager
            .add_event(
                id,
                AddCameraEvent {
                    kind: "calibration_sample".to_owned(),
                    seat: Some(SeatClass::FrontLeft),
                    note: Some("known seat".to_owned()),
                },
            )
            .unwrap();

        assert_eq!(updated.events.len(), 1);
        assert!(updated.events[0].timestamp_ns > 0);
        assert_eq!(
            updated.events[0].calibration_path.as_deref(),
            Some("calibration-FrontLeft.jpg")
        );
        assert_eq!(
            fs::read(session_dir.join("calibration-FrontLeft.jpg")).unwrap(),
            b"jpeg bytes"
        );
        assert_eq!(manager.sessions().unwrap()[0].events.len(), 1);

        let context = manager.clip_context(1_000).unwrap();
        assert_eq!(context["session_id"], id);
        assert_eq!(context["clip_trigger_ns"], 1_000);
        assert_eq!(context["clip_offset_ns"], 877);
        assert_eq!(context["session_started_ns"], 123);
    }

    #[test]
    fn rejects_unsafe_session_ids() {
        for id in ["", "../other", "with/slash", "with\\slash", "with space"] {
            assert!(validate_id(id).is_err(), "{id}");
        }
        assert!(validate_id("1723456789-1234-0000").is_ok());
    }

    #[test]
    fn log_tail_surfaces_ffmpeg_failure_reason() {
        let dir = tempfile::tempdir().unwrap();
        let session_dir = dir.path();
        let mut file = File::create(session_dir.join("ffmpeg.log")).unwrap();
        writeln!(file, "[dshow @ ...] Could not run graph (sometimes caused by a device already in use by other application)").unwrap();
        writeln!(file, "Error opening input file video=Brio 101.").unwrap();
        writeln!(file, "Error opening input files: I/O error").unwrap();
        let tail = ffmpeg_log_tail(session_dir);
        assert!(tail.contains("device already in use"), "{tail}");
        assert!(tail.contains("I/O error"), "{tail}");
    }

    #[test]
    fn log_tail_is_empty_when_no_log_exists() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(ffmpeg_log_tail(dir.path()), "");
    }
}
