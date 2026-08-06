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
const CAPTURE_TAPS: usize = 64;
const ROUNDS_PER_FRAME: u32 = 5;
const LEGACY_LINKS_PER_FRAME: usize = N_NODES as usize * (N_NODES as usize - 1);
const PENDING_FRAME_LIMIT: usize = 8;
const FRAME_QUEUE_CAPACITY: usize = 4;
const STDERR_TAIL_LINES: usize = 8;
const RATE_WINDOW_NS: i64 = 5_000_000_000;
const IDLE_STOP_GRACE: Duration = Duration::from_secs(30);

fn directed_links() -> impl Iterator<Item = (u8, u8)> {
    (0..N_NODES).flat_map(|a| (0..N_NODES).filter(move |b| *b != a).map(move |b| (a, b)))
}

fn reciprocal_links() -> impl Iterator<Item = (u8, u8)> {
    (0..N_NODES).flat_map(|a| (a + 1..N_NODES).map(move |b| (a, b)))
}

#[derive(Clone)]
struct FeatureContract {
    links: Vec<(u8, u8)>,
    taps_left: usize,
    taps_right: usize,
    legacy_full: bool,
    variant: String,
    mode: String,
    smoothing_window: usize,
}

impl Default for FeatureContract {
    fn default() -> Self {
        Self {
            links: directed_links().collect(),
            taps_left: 0,
            taps_right: CAPTURE_TAPS - 1,
            legacy_full: true,
            variant: "raw".to_owned(),
            mode: "seat".to_owned(),
            smoothing_window: 5,
        }
    }
}

impl FeatureContract {
    fn load(checkpoint: &std::path::Path) -> Result<Self> {
        let manifest_path = checkpoint.with_extension("manifest.json");
        if !manifest_path.is_file() {
            let mut legacy = Self::default();
            if checkpoint
                .file_name()
                .is_some_and(|name| name.to_string_lossy().contains("calibrated"))
            {
                legacy.variant = "calibrated".to_owned();
            }
            return Ok(legacy);
        }
        let value: Value = serde_json::from_slice(&std::fs::read(&manifest_path)?)
            .with_context(|| format!("parse model manifest {}", manifest_path.display()))?;
        if value["schema_version"].as_u64() != Some(2) {
            bail!(
                "unsupported classifier manifest schema in {}",
                manifest_path.display()
            );
        }
        let links = value["link_order"]
            .as_array()
            .context("model manifest contains no link_order")?
            .iter()
            .map(|row| {
                let row = row.as_array().context("link_order rows must be arrays")?;
                let from = row
                    .first()
                    .and_then(Value::as_u64)
                    .context("link from missing")? as u8;
                let to = row
                    .get(1)
                    .and_then(Value::as_u64)
                    .context("link to missing")? as u8;
                if from >= N_NODES || to >= N_NODES || from == to {
                    bail!("invalid model link {from}>{to}");
                }
                Ok((from, to))
            })
            .collect::<Result<Vec<_>>>()?;
        let taps_left = value["taps_left"]
            .as_u64()
            .context("manifest taps_left missing")? as usize;
        let taps_right = value["taps_right"]
            .as_u64()
            .context("manifest taps_right missing")? as usize;
        let n_taps = value["n_taps"]
            .as_u64()
            .context("manifest n_taps missing")? as usize;
        if links.is_empty()
            || value["n_links"].as_u64() != Some(links.len() as u64)
            || n_taps != taps_left + taps_right + 1
        {
            bail!("classifier manifest feature dimensions are inconsistent");
        }
        Ok(Self {
            links,
            taps_left,
            taps_right,
            legacy_full: false,
            variant: value["variant"].as_str().unwrap_or("raw").to_owned(),
            mode: value["model_mode"].as_str().unwrap_or("seat").to_owned(),
            smoothing_window: value["smoothing_window"].as_u64().unwrap_or(5).clamp(1, 31) as usize,
        })
    }

    fn width(&self) -> usize {
        if self.legacy_full {
            CAPTURE_TAPS
        } else {
            self.taps_left + self.taps_right + 1
        }
    }
}

enum Variant {
    Raw,
    /// Frozen reference taps in canonical link order; live magnitude becomes
    /// |cir - reference| exactly like the calibrated dataset variant.
    Calibrated(BTreeMap<(u8, u8), Vec<[f64; 2]>>),
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
        expected_links: usize,
    ) -> Option<(u64, HashMap<(u8, u8), Vec<f32>>)> {
        if self.emitted_through.is_some_and(|done| frame <= done) {
            return None;
        }
        let links = self.pending.entry(frame).or_default();
        links.insert(key, magnitude);
        if links.len() == expected_links {
            let complete = self.pending.remove(&frame).unwrap();
            self.emitted_through = Some(frame);
            self.pending
                .retain(|&pending_frame, _| pending_frame > frame);
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
    features: FeatureContract,
    variant: Variant,
    started_ns: i64,
    predictions: u64,
    dropped_frames: u64,
    latency_ms: VecDeque<f64>,
    recent_ns: VecDeque<i64>,
    last: Option<Value>,
    error: Option<String>,
    stop_reason: Option<String>,
    stderr_tail: VecDeque<String>,
    child: Option<Child>,
    frame_tx: Option<SyncSender<String>>,
    assembler: FrameAssembler,
    smoother: PredictionSmoother,
}

impl Default for RunState {
    fn default() -> Self {
        Self {
            generation: 0,
            phase: Phase::Idle,
            model: String::new(),
            features: FeatureContract::default(),
            variant: Variant::Raw,
            started_ns: 0,
            predictions: 0,
            dropped_frames: 0,
            latency_ms: VecDeque::new(),
            recent_ns: VecDeque::new(),
            last: None,
            error: None,
            stop_reason: None,
            stderr_tail: VecDeque::new(),
            child: None,
            frame_tx: None,
            assembler: FrameAssembler::default(),
            smoother: PredictionSmoother::default(),
        }
    }
}

#[derive(Default)]
struct PredictionSmoother {
    window: usize,
    seat: VecDeque<Vec<f64>>,
    person: VecDeque<Vec<f64>>,
    person_labels: VecDeque<String>,
}

impl PredictionSmoother {
    fn reset(&mut self, window: usize) {
        self.window = window.max(1);
        self.seat.clear();
        self.person.clear();
        self.person_labels.clear();
    }

    fn update(&mut self, value: &mut Value) {
        let raw_seat = value["raw_seat"]
            .as_str()
            .or_else(|| value["seat"].as_str())
            .map(str::to_owned);
        let seat_probs = probability_vector(&value["raw_seat_probs"])
            .or_else(|| probability_vector(&value["probs"]));
        if let Some(probs) = seat_probs {
            push_probabilities(&mut self.seat, probs, self.window);
            let stable = average_probabilities(&self.seat);
            let index = argmax(&stable);
            let classes = ["FrontLeft", "FrontRight", "BackRight", "BackLeft", "Empty"];
            let name = classes
                .get(index)
                .copied()
                .or(raw_seat.as_deref())
                .unwrap_or("")
                .to_owned();
            value["raw_seat"] = raw_seat.map_or(Value::Null, Value::String);
            value["raw_seat_probs"] = json!(
                value["raw_seat_probs"]
                    .as_array()
                    .cloned()
                    .unwrap_or_else(|| value["probs"].as_array().cloned().unwrap_or_default())
            );
            value["stable_seat"] = json!(name);
            value["stable_seat_index"] = json!(index);
            value["stable_seat_probs"] = json!(stable);
            value["seat"] = json!(name);
            value["seat_index"] = json!(index);
            value["probs"] = value["stable_seat_probs"].clone();
            value["uncertain"] = json!(stable.get(index).copied().unwrap_or(0.0) < 0.6);
        }

        if let Some(probs) = probability_vector(&value["raw_person_probs"]) {
            push_probabilities(&mut self.person, probs, self.window);
            let stable = average_probabilities(&self.person);
            let index = argmax(&stable);
            value["stable_person_index"] = json!(index);
            value["stable_person_probs"] = json!(stable);
        }
        if let Some(raw) = value["raw_person"].as_str().map(str::to_owned) {
            self.person_labels.push_back(raw);
            while self.person_labels.len() > self.window {
                self.person_labels.pop_front();
            }
        }
        if value["stable_seat"] == "Empty" {
            value["stable_person"] = json!("n/a");
        } else if let Some(person) = majority_label(&self.person_labels) {
            value["stable_person"] = json!(person);
        }
        if value.get("stable_person").is_some() {
            value["person"] = value["stable_person"].clone();
        }
    }
}

fn probability_vector(value: &Value) -> Option<Vec<f64>> {
    value
        .as_array()?
        .iter()
        .map(Value::as_f64)
        .collect::<Option<Vec<_>>>()
        .filter(|values| !values.is_empty() && values.iter().all(|value| value.is_finite()))
}

fn push_probabilities(queue: &mut VecDeque<Vec<f64>>, values: Vec<f64>, window: usize) {
    if queue
        .back()
        .is_some_and(|prior| prior.len() != values.len())
    {
        queue.clear();
    }
    queue.push_back(values);
    while queue.len() > window {
        queue.pop_front();
    }
}

fn average_probabilities(queue: &VecDeque<Vec<f64>>) -> Vec<f64> {
    let mut average = vec![0.0; queue.front().map_or(0, Vec::len)];
    for values in queue {
        for (total, value) in average.iter_mut().zip(values) {
            *total += value;
        }
    }
    let count = queue.len().max(1) as f64;
    average.iter_mut().for_each(|value| *value /= count);
    average
}

fn argmax(values: &[f64]) -> usize {
    values
        .iter()
        .enumerate()
        .max_by(|(_, a), (_, b)| a.total_cmp(b))
        .map_or(0, |(index, _)| index)
}

fn majority_label(values: &VecDeque<String>) -> Option<&str> {
    values
        .iter()
        .max_by_key(|candidate| values.iter().filter(|value| *value == *candidate).count())
        .map(String::as_str)
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
                    let checkpoint = dir.join(&name);
                    let features = FeatureContract::load(&checkpoint).ok();
                    json!({
                        "name": name,
                        "modified_ms": modified_ms,
                        "bytes": bytes,
                        "mode": features.as_ref().map(|value| value.mode.as_str()),
                        "variant": features.as_ref().map(|value| value.variant.as_str()),
                        "links": features.as_ref().map(|value| value.links.len()),
                        "taps": features.as_ref().map(FeatureContract::width),
                    })
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
        let latency_average_ms = if state.latency_ms.is_empty() {
            0.0
        } else {
            state.latency_ms.iter().sum::<f64>() / state.latency_ms.len() as f64
        };
        let mut ordered_latency = state.latency_ms.iter().copied().collect::<Vec<_>>();
        ordered_latency.sort_by(f64::total_cmp);
        let latency_p95_ms = ordered_latency
            .get((ordered_latency.len().saturating_sub(1) * 95) / 100)
            .copied()
            .unwrap_or(0.0);
        json!({
            "status": state.phase.name(),
            "model": state.model,
            "features": {
                "mode": state.features.mode,
                "variant": state.features.variant,
                "links": state.features.links.len(),
                "taps_left": state.features.taps_left,
                "taps_right": state.features.taps_right,
                "taps": state.features.width(),
                "smoothing_window": state.features.smoothing_window,
            },
            "started_ns": if state.started_ns == 0 { Value::Null } else { json!(state.started_ns) },
            "predictions": state.predictions,
            "dropped_frames": state.dropped_frames,
            "rate_hz": rate_hz,
            "latency_average_ms": latency_average_ms,
            "latency_p95_ms": latency_p95_ms,
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
        let checkpoint = self
            .inner
            .config
            .seatclass_root
            .join("models")
            .join(model_name);
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
        let features = FeatureContract::load(&checkpoint)?;
        let variant = if features.variant == "calibrated" {
            let refs = frozen_refs.context(
                "the calibrated model subtracts frozen board references; freeze the board first",
            )?;
            for key in &features.links {
                let taps = refs.get(key).with_context(|| {
                    format!("frozen references are missing link {}>{}", key.0, key.1)
                })?;
                if taps.len() != CAPTURE_TAPS {
                    bail!(
                        "frozen reference for {}>{} has {} taps",
                        key.0,
                        key.1,
                        taps.len()
                    );
                }
            }
            Variant::Calibrated(refs)
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
                format!(
                    "spawn {} for live inference",
                    self.inner.config.python.display()
                )
            })?;
        let stdin = child.stdin.take().context("inference stdin unavailable")?;
        let stdout = child
            .stdout
            .take()
            .context("inference stdout unavailable")?;
        let stderr = child
            .stderr
            .take()
            .context("inference stderr unavailable")?;

        let generation = state.generation + 1;
        let smoothing_window = features.smoothing_window;
        let (frame_tx, frame_rx) = sync_channel::<String>(FRAME_QUEUE_CAPACITY);
        *state = RunState {
            generation,
            phase: Phase::Starting,
            model: model_name.to_owned(),
            features,
            variant,
            started_ns: now_ns(),
            child: Some(child),
            frame_tx: Some(frame_tx),
            ..RunState::default()
        };
        state.smoother.reset(smoothing_window);
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
            let smoothing_window = state.features.smoothing_window;
            state.smoother.reset(smoothing_window);
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
            let idle = demand[Topic::SeatInference as usize].load(Ordering::Relaxed) == 0;
            if idle && manager.wants_frames() && manager.inner.state.lock().generation == generation
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
        let feature_links = state.features.links.clone();
        let expected_links = feature_links.len();
        for sample in samples {
            if sample.from >= N_NODES
                || sample.to >= N_NODES
                || sample.from == sample.to
                || sample.iq.len() != CAPTURE_TAPS
            {
                continue;
            }
            let key = (sample.from, sample.to);
            if !feature_links.contains(&key) {
                continue;
            }
            // Magnitude from iq, matching build_seat_dataset.py exactly (the
            // stored magnitude field is not guaranteed to equal |iq|).
            let magnitude = feature_magnitude(sample, &state.variant, &state.features);
            let frame = u64::from(sample.round / ROUNDS_PER_FRAME);
            if let Some((frame_id, links)) =
                state
                    .assembler
                    .insert(frame, key, magnitude, expected_links)
            {
                let matrix: Vec<&Vec<f32>> =
                    feature_links.iter().map(|link| &links[link]).collect();
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

    fn spawn_stdout_reader(&self, generation: u64, stdout: impl std::io::Read + Send + 'static) {
        let manager = self.clone();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                let Ok(mut value) = serde_json::from_str::<Value>(&line) else {
                    continue;
                };
                if value["ready"] == true {
                    let mut state = manager.inner.state.lock();
                    if state.generation == generation && state.phase == Phase::Starting {
                        state.phase = Phase::Running;
                    }
                    continue;
                }
                if value.get("seat").is_none()
                    && value.get("raw_seat").is_none()
                    && value.get("raw_person").is_none()
                {
                    continue;
                }
                {
                    let mut state = manager.inner.state.lock();
                    if state.generation != generation {
                        return;
                    }
                    state.smoother.update(&mut value);
                    state.phase = Phase::Running;
                    state.predictions += 1;
                    let now = now_ns();
                    if let Some(sent_ms) = value["ts"].as_f64() {
                        let latency_ms = now as f64 / 1e6 - sent_ms;
                        if latency_ms.is_finite() && latency_ms >= 0.0 {
                            state.latency_ms.push_back(latency_ms);
                            while state.latency_ms.len() > 256 {
                                state.latency_ms.pop_front();
                            }
                        }
                    }
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

    fn spawn_stderr_reader(&self, generation: u64, stderr: impl std::io::Read + Send + 'static) {
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

fn feature_magnitude(
    sample: &AlignedCirSample,
    variant: &Variant,
    features: &FeatureContract,
) -> Vec<f32> {
    let reference = match variant {
        Variant::Raw => None,
        Variant::Calibrated(references) => references.get(&(sample.from, sample.to)),
    };
    let magnitude_at = |index: usize| {
        let [re, im] = sample.iq[index];
        let (mut re, mut im) = (f64::from(re), f64::from(im));
        if let Some(reference) = reference {
            re -= reference[index][0];
            im -= reference[index][1];
        }
        (re * re + im * im).sqrt() as f32
    };
    if features.legacy_full {
        return (0..sample.iq.len()).map(magnitude_at).collect();
    }
    let center = round_half_away_from_zero(sample.marker_aligned);
    let start = center - features.taps_left as isize;
    (0..features.width())
        .map(|offset| {
            let index = start + offset as isize;
            usize::try_from(index)
                .ok()
                .filter(|index| *index < sample.iq.len())
                .map_or(0.0, magnitude_at)
        })
        .collect()
}

fn round_half_away_from_zero(value: f64) -> isize {
    if value >= 0.0 {
        (value + 0.5).floor() as isize
    } else {
        (value - 0.5).ceil() as isize
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
    fn link_orders_match_python_contract() {
        let ordered = directed_links().collect::<Vec<_>>();
        assert_eq!(ordered.len(), LEGACY_LINKS_PER_FRAME);
        assert_eq!(ordered[0], (0, 1));
        assert_eq!(ordered[4], (1, 0));
        assert_eq!(ordered[19], (4, 3));
        let reciprocal = reciprocal_links().collect::<Vec<_>>();
        assert_eq!(reciprocal.len(), 10);
        assert_eq!(reciprocal[0], (0, 1));
        assert_eq!(reciprocal[9], (3, 4));
    }

    #[test]
    fn assembler_emits_only_complete_frames_and_skips_stale() {
        let mut assembler = FrameAssembler::default();
        let links = reciprocal_links().collect::<Vec<_>>();
        for (from, to) in links.iter().take(links.len() - 1) {
            assert!(
                assembler
                    .insert(3, (*from, *to), vec![1.0], links.len())
                    .is_none()
            );
        }
        let (frame, complete) = assembler
            .insert(3, links[links.len() - 1], vec![1.0], links.len())
            .expect("final selected link completes the frame");
        assert_eq!(frame, 3);
        assert_eq!(complete.len(), links.len());
        // Late sample for an already-emitted frame is ignored.
        assert!(
            assembler
                .insert(3, links[0], vec![1.0], links.len())
                .is_none()
        );
        assert!(
            assembler
                .insert(2, links[0], vec![1.0], links.len())
                .is_none()
        );
        assert!(assembler.pending.is_empty());
    }

    #[test]
    fn assembler_bounds_pending_frames() {
        let mut assembler = FrameAssembler::default();
        for frame in 0..(PENDING_FRAME_LIMIT as u64 + 5) {
            assembler.insert(frame, (0, 1), vec![1.0], 10);
        }
        assert!(assembler.pending.len() <= PENDING_FRAME_LIMIT);
        assert!(
            assembler
                .pending
                .contains_key(&(PENDING_FRAME_LIMIT as u64 + 4))
        );
    }

    #[test]
    fn los_window_matches_python_padding_contract() {
        let mut sample = frame_samples(0).remove(0);
        sample.marker_aligned = 1.5;
        sample.iq = (0..CAPTURE_TAPS).map(|index| [index as f32, 0.0]).collect();
        let features = FeatureContract {
            links: vec![(sample.from, sample.to)],
            taps_left: 3,
            taps_right: 2,
            legacy_full: false,
            variant: "raw".to_owned(),
            mode: "seat".to_owned(),
            smoothing_window: 5,
        };
        assert_eq!(round_half_away_from_zero(sample.marker_aligned), 2);
        assert_eq!(
            feature_magnitude(&sample, &Variant::Raw, &features),
            vec![0.0, 0.0, 1.0, 2.0, 3.0, 4.0]
        );
    }

    #[test]
    fn smoother_preserves_raw_and_emits_stable_empty() {
        let mut smoother = PredictionSmoother::default();
        smoother.reset(3);
        let mut first = json!({
            "raw_seat": "FrontLeft",
            "raw_seat_probs": [0.7, 0.1, 0.1, 0.05, 0.05],
            "raw_person": "Anand",
            "raw_person_probs": [0.9, 0.1],
        });
        smoother.update(&mut first);
        assert_eq!(first["stable_seat"], "FrontLeft");
        assert_eq!(first["stable_person"], "Anand");
        for _ in 0..2 {
            let mut empty = json!({
                "raw_seat": "Empty",
                "raw_seat_probs": [0.01, 0.01, 0.01, 0.01, 0.96],
                "raw_person": "n/a",
                "raw_person_probs": [0.5, 0.5],
            });
            smoother.update(&mut empty);
            if smoother.seat.len() == 3 {
                assert_eq!(empty["stable_seat"], "Empty");
                assert_eq!(empty["stable_person"], "n/a");
            }
        }
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
        directed_links()
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
                magnitude: vec![0.0; CAPTURE_TAPS],
                iq: vec![[1e-3, 0.0]; CAPTURE_TAPS],
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
        assert!(
            manager.start(&model, None).is_err(),
            "second start must be rejected"
        );
        assert!(
            wait_for(
                || manager.status()["status"] == "running",
                Duration::from_secs(30)
            ),
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
            wait_for(
                || manager.status()["status"] == "running",
                Duration::from_secs(30)
            ),
            "restart: {:?}",
            manager.status()
        );
        manager.stop("test");
        assert_eq!(manager.status()["status"], "idle");
    }
}
