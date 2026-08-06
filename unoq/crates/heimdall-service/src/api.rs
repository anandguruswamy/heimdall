use std::{
    collections::{BTreeMap, HashSet},
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use axum::{
    Json, Router,
    body::Body,
    extract::{
        Path, State, WebSocketUpgrade,
        ws::{Message, WebSocket},
    },
    http::{HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use parking_lot::Mutex;
use serde_json::{Value, json};
use tokio::sync::broadcast;
use tower_http::{cors::CorsLayer, trace::TraceLayer};

use anyhow::Context;

use crate::{
    clips::ClipManager,
    inference::InferenceManager,
    metadata::Metadata,
    pipeline::Pipeline,
    telemetry::{DSP_TOPIC_COUNT, TOPIC_COUNT, Topic, envelope_batch, envelope_key, envelope_topic},
    training::{SEAT_CLASSES, TrainingClip, TrainingConfig, TrainingManager},
};

include!(concat!(env!("OUT_DIR"), "/assets.rs"));

#[derive(Default)]
pub struct LiveMetrics {
    pub udp_datagrams: AtomicU64,
    pub udp_invalid: AtomicU64,
    pub records_completed: AtomicU64,
    pub records_expired: AtomicU64,
    pub microbatches: AtomicU64,
    pub batched_records: AtomicU64,
    pub processing_queue_depth: AtomicU64,
    pub processing_queue_high_water: AtomicU64,
    pub queue_wait_count: AtomicU64,
    pub queue_wait_ns: AtomicU64,
    pub queue_wait_max_ns: AtomicU64,
    pub processing_count: AtomicU64,
    pub processing_ns: AtomicU64,
    pub processing_max_ns: AtomicU64,
    pub websocket_coalesced: AtomicU64,
    pub websocket_lagged: AtomicU64,
    pub websocket_send_count: AtomicU64,
    pub websocket_updates: AtomicU64,
    pub websocket_send_ns: AtomicU64,
    pub websocket_send_max_ns: AtomicU64,
}

pub fn record_max(target: &AtomicU64, value: u64) {
    target.fetch_max(value, Ordering::Relaxed);
}

#[derive(Clone)]
pub struct AppState {
    pub pipeline: Arc<Mutex<Pipeline>>,
    pub metadata: Arc<Metadata>,
    pub clips: ClipManager,
    pub training: TrainingManager,
    pub inference: InferenceManager,
    pub stream: broadcast::Sender<Vec<u8>>,
    pub processing_drops: Arc<AtomicU64>,
    pub live_metrics: Arc<LiveMetrics>,
    pub archive_errors: Arc<AtomicU64>,
    pub archive_paused: Arc<std::sync::atomic::AtomicBool>,
    pub archive_last_error: Arc<Mutex<Option<String>>>,
    pub web_clients: Arc<AtomicU64>,
    pub topic_demand: Arc<[AtomicU64; TOPIC_COUNT]>,
    pub data_root: Arc<PathBuf>,
    pub started: std::time::Instant,
}

impl AppState {
    pub fn new(
        metadata: Arc<Metadata>,
        data_root: impl AsRef<std::path::Path>,
    ) -> anyhow::Result<Self> {
        let data_root = data_root.as_ref().to_path_buf();
        let (stream, _) = broadcast::channel(2_048);
        let mut pipeline = Pipeline::new();
        if let Ok(stored) = metadata.settings()
            && stored["epoch"].as_u64().unwrap_or(0) > 0
        {
            let _ = pipeline.update_settings(&stored["value"]);
        }
        if let Ok(stack) = metadata.calibration_stack() {
            pipeline.restore_calibration_stack(stack);
        }
        Ok(Self {
            pipeline: Arc::new(Mutex::new(pipeline)),
            clips: ClipManager::new(data_root.join("clips"), metadata.clone())?,
            training: TrainingManager::new(data_root.join("training"), TrainingConfig::from_env()),
            inference: InferenceManager::new(TrainingConfig::from_env(), stream.clone()),
            metadata,
            stream,
            processing_drops: Arc::new(AtomicU64::new(0)),
            live_metrics: Arc::new(LiveMetrics::default()),
            archive_errors: Arc::new(AtomicU64::new(0)),
            archive_paused: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            archive_last_error: Arc::new(Mutex::new(None)),
            web_clients: Arc::new(AtomicU64::new(0)),
            topic_demand: Arc::new(std::array::from_fn(|_| AtomicU64::new(0))),
            data_root: Arc::new(data_root),
            started: std::time::Instant::now(),
        })
    }

    /// Demand mask for the DSP pipeline. Only the first DSP_TOPIC_COUNT
    /// topics participate; seat-inference is fed from the capture path.
    pub fn topic_mask(&self) -> u8 {
        self.topic_demand
            .iter()
            .enumerate()
            .take(DSP_TOPIC_COUNT)
            .fold(0, |mask, (index, count)| {
                mask | if count.load(Ordering::Relaxed) > 0 {
                    1 << index
                } else {
                    0
                }
            })
    }
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/api/v1/health", get(health))
        .route("/api/v1/topology", get(topology))
        .route("/api/v1/history/distance", get(distance_history))
        .route("/api/v1/settings", get(settings).put(put_settings))
        .route("/api/v1/board", get(board))
        .route("/api/v1/board/freeze", post(freeze_board))
        .route("/api/v1/board/unfreeze", post(unfreeze_board))
        .route("/api/v1/clips", get(clips).post(post_clip))
        .route("/api/v1/clips/{id}", get(download_clip).delete(delete_clip))
        .route("/api/v1/clips/{id}/training", post(tag_clip))
        .route("/api/v1/training/run", post(run_training))
        .route("/api/v1/training/status", get(training_status))
        .route("/api/v1/inference/models", get(inference_models))
        .route("/api/v1/inference/start", post(inference_start))
        .route("/api/v1/inference/stop", post(inference_stop))
        .route("/api/v1/inference/status", get(inference_status))
        .route("/api/v1/calibration", get(calibration))
        .route(
            "/api/v1/calibration/snapshot",
            get(calibration).post(start_calibration),
        )
        .route(
            "/api/v1/calibration/apply",
            axum::routing::post(apply_calibration),
        )
        .route(
            "/api/v1/calibration/solve",
            axum::routing::post(solve_calibration),
        )
        .route(
            "/api/v1/calibration/rollback",
            axum::routing::post(rollback_calibration),
        )
        .route("/api/v1/stream", get(stream))
        .route("/api/health", get(health))
        .route("/api/topology", get(topology))
        .route("/api/history/distance", get(distance_history))
        .route("/api/settings", get(settings).put(put_settings))
        .route("/api/board", get(board))
        .route("/api/board/freeze", post(freeze_board))
        .route("/api/board/unfreeze", post(unfreeze_board))
        .route("/api/clips", get(clips).post(post_clip))
        .route("/api/clips/{id}", get(download_clip).delete(delete_clip))
        .route("/api/clips/{id}/training", post(tag_clip))
        .route("/api/training/run", post(run_training))
        .route("/api/training/status", get(training_status))
        .route("/api/inference/models", get(inference_models))
        .route("/api/inference/start", post(inference_start))
        .route("/api/inference/stop", post(inference_stop))
        .route("/api/inference/status", get(inference_status))
        .route("/api/calibration", get(calibration))
        .route(
            "/api/calibration/snapshot",
            get(calibration).post(start_calibration),
        )
        .route(
            "/api/calibration/apply",
            axum::routing::post(apply_calibration),
        )
        .route(
            "/api/calibration/solve",
            axum::routing::post(solve_calibration),
        )
        .route(
            "/api/calibration/rollback",
            axum::routing::post(rollback_calibration),
        )
        .route("/api/stream", get(stream))
        .route(
            "/",
            get(|| async { asset(Path("index.html".to_owned())).await }),
        )
        .route("/{*path}", get(asset))
        .with_state(state)
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
}

async fn health(State(state): State<AppState>) -> Json<Value> {
    let pipeline = state.pipeline.lock().summary();
    let total_bytes = fs2::total_space(state.data_root.as_ref()).unwrap_or(0);
    let free_bytes = fs2::available_space(state.data_root.as_ref()).unwrap_or(0);
    let archive_bytes = state.metadata.verified_segments().map_or(0, |segments| {
        segments
            .into_iter()
            .map(|segment| segment.bytes)
            .sum::<u64>()
    });
    let archive_paused = state.archive_paused.load(Ordering::Relaxed);
    let archive_last_error = state.archive_last_error.lock().clone();
    let metrics = &state.live_metrics;
    let average_ms = |total: &AtomicU64, count: &AtomicU64| {
        let count = count.load(Ordering::Relaxed);
        (count > 0).then(|| total.load(Ordering::Relaxed) as f64 / count as f64 / 1_000_000.0)
    };
    let free_percent = if total_bytes == 0 {
        None
    } else {
        Some(free_bytes as f64 * 100.0 / total_bytes as f64)
    };
    Json(json!({
        "status": if archive_paused { "degraded" } else { "ok" }, "uptime_seconds": state.started.elapsed().as_secs_f64(),
        "pipeline": pipeline,
        "processing_queue_drops": state.processing_drops.load(Ordering::Relaxed),
        "websocket_clients": state.web_clients.load(Ordering::Relaxed),
        "live": {
            "udp_datagrams": metrics.udp_datagrams.load(Ordering::Relaxed),
            "udp_invalid": metrics.udp_invalid.load(Ordering::Relaxed),
            "records_completed": metrics.records_completed.load(Ordering::Relaxed),
            "records_expired": metrics.records_expired.load(Ordering::Relaxed),
            "microbatches": metrics.microbatches.load(Ordering::Relaxed),
            "batched_records": metrics.batched_records.load(Ordering::Relaxed),
            "queue_depth": metrics.processing_queue_depth.load(Ordering::Relaxed),
            "queue_high_water": metrics.processing_queue_high_water.load(Ordering::Relaxed),
            "queue_wait_avg_ms": average_ms(&metrics.queue_wait_ns, &metrics.queue_wait_count),
            "queue_wait_max_ms": metrics.queue_wait_max_ns.load(Ordering::Relaxed) as f64 / 1_000_000.0,
            "processing_avg_ms": average_ms(&metrics.processing_ns, &metrics.processing_count),
            "processing_max_ms": metrics.processing_max_ns.load(Ordering::Relaxed) as f64 / 1_000_000.0,
            "websocket_coalesced": metrics.websocket_coalesced.load(Ordering::Relaxed),
            "websocket_lagged": metrics.websocket_lagged.load(Ordering::Relaxed),
            "websocket_send_avg_ms": average_ms(&metrics.websocket_send_ns, &metrics.websocket_send_count),
            "websocket_send_max_ms": metrics.websocket_send_max_ns.load(Ordering::Relaxed) as f64 / 1_000_000.0,
            "websocket_batches": metrics.websocket_send_count.load(Ordering::Relaxed),
            "websocket_updates": metrics.websocket_updates.load(Ordering::Relaxed)
        },
        "archive": {"closed_bytes": archive_bytes, "free_bytes": free_bytes, "total_bytes": total_bytes,
            "free_percent": free_percent, "paused": archive_paused,
            "errors": state.archive_errors.load(Ordering::Relaxed), "last_error": archive_last_error},
        "process": {"rss_bytes": process_rss_bytes()}
    }))
}

async fn topology(State(state): State<AppState>) -> Json<Value> {
    Json(state.pipeline.lock().topology())
}

async fn distance_history(State(state): State<AppState>) -> Json<Value> {
    Json(state.pipeline.lock().distance_history())
}

async fn settings(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    let stored = state.metadata.settings()?;
    let pipeline = state.pipeline.lock();
    Ok(Json(json!({
        "epoch": stored["epoch"], "processing_epoch": pipeline.summary().processing_epoch,
        "value": pipeline.settings()
    })))
}

async fn put_settings(
    State(state): State<AppState>,
    Json(value): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let effective = state.pipeline.lock().update_settings(&value)?;
    let mut stored = state
        .metadata
        .put_settings(&serde_json::to_value(effective)?)?;
    stored["processing_epoch"] = json!(state.pipeline.lock().summary().processing_epoch);
    Ok(Json(stored))
}

async fn freeze_board(State(state): State<AppState>) -> Json<Value> {
    Json(state.pipeline.lock().freeze_board_references())
}

async fn board(State(state): State<AppState>) -> Json<Value> {
    Json(state.pipeline.lock().board_freeze_status("current"))
}

async fn unfreeze_board(State(state): State<AppState>) -> Json<Value> {
    Json(state.pipeline.lock().unfreeze_board_references())
}

async fn clips(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    Ok(Json(Value::Array(state.metadata.clips()?)))
}

async fn post_clip(
    State(state): State<AppState>,
    Json(value): Json<Value>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let mut context = {
        let pipeline = state.pipeline.lock();
        json!({
            "software": {"name": "heimdall-service", "version": env!("CARGO_PKG_VERSION")},
            "pipeline": pipeline.summary(), "settings": pipeline.settings(),
            "board_freeze": pipeline.board_freeze_snapshot(),
            "calibration": state.metadata.calibration_snapshot()?
        })
    };
    if let Some(positions) = value.get("board_positions") {
        context["board_positions"] = positions.clone();
    }
    let clip = state
        .clips
        .start(&value, context, crate::metadata::now_ns())?;
    Ok((StatusCode::ACCEPTED, Json(clip)))
}

async fn download_clip(
    Path(id): Path<i64>,
    State(state): State<AppState>,
) -> Result<Response, ApiError> {
    let bytes = tokio::fs::read(state.clips.path(id)?)
        .await
        .map_err(anyhow::Error::from)?;
    let mut response = Response::new(Body::from(bytes));
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/zip"),
    );
    response.headers_mut().insert(
        header::CONTENT_DISPOSITION,
        HeaderValue::from_str(&format!("attachment; filename=heimdall-clip-{id:06}.zip"))
            .map_err(anyhow::Error::from)?,
    );
    Ok(response)
}

async fn delete_clip(
    Path(id): Path<i64>,
    State(state): State<AppState>,
) -> Result<StatusCode, ApiError> {
    if state.clips.delete(id)? {
        Ok(StatusCode::NO_CONTENT)
    } else {
        Ok(StatusCode::NOT_FOUND)
    }
}

/// Attach or update a training tag (seat class, person, exclusion) on a clip.
/// The tag is merged into the clip's stored metadata value, so it survives
/// service restarts and page reloads. Passing `seat: null` removes the tag.
async fn tag_clip(
    Path(id): Path<i64>,
    State(state): State<AppState>,
    Json(request): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let row = state
        .metadata
        .clip(id)?
        .context("capture clip does not exist")?;
    let mut value = row["value"].clone();
    if value["status"] == "capturing" {
        return Err(anyhow::anyhow!("wait for the capture to complete before tagging").into());
    }
    let seat = match &request["seat"] {
        Value::Null => None,
        Value::String(seat) if SEAT_CLASSES.contains(&seat.as_str()) => Some(seat.clone()),
        _ => {
            return Err(anyhow::anyhow!(
                "seat must be null or one of FrontLeft, FrontRight, BackRight, BackLeft"
            )
            .into());
        }
    };
    let person = request["person"].as_str().unwrap_or("").trim();
    if person.len() > 60 {
        return Err(anyhow::anyhow!("person name is limited to 60 characters").into());
    }
    match seat {
        Some(seat) => {
            value["training"] = serde_json::json!({
                "seat": seat,
                "person": person,
                "exclude": request["exclude"] == true,
                "updated_ns": crate::metadata::now_ns(),
            });
        }
        None => {
            if let Some(object) = value.as_object_mut() {
                object.remove("training");
            }
        }
    }
    state.metadata.update_clip(id, &value)?;
    Ok(Json(
        json!({"id": id, "created_ns": row["created_ns"], "value": value}),
    ))
}

/// Launch the capture-to-model pipeline over every tagged, non-excluded,
/// complete clip. The run executes on a background thread; progress streams
/// through `training_status`.
async fn run_training(
    State(state): State<AppState>,
    Json(request): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let variant = request["variant"].as_str().unwrap_or("raw");
    let epochs = request["epochs"].as_i64();
    let mut training_clips = Vec::new();
    for row in state.metadata.clips()? {
        let value = &row["value"];
        if value["status"] != "complete" {
            continue;
        }
        let tag = &value["training"];
        let Some(seat) = tag["seat"].as_str() else {
            continue;
        };
        if tag["exclude"] == true {
            continue;
        }
        // Clips without aligned CIR records cannot feed the dataset builder;
        // the dashboard flags them NO CIR and they are skipped here too.
        if value["manifest"]["aligned_cir_records"].as_i64().unwrap_or(0) == 0 {
            continue;
        }
        let Some(id) = row["id"].as_i64() else {
            continue;
        };
        let zip_path = state
            .clips
            .path(id)
            .with_context(|| format!("stored clip {id:06} is unavailable"))?;
        training_clips.push(TrainingClip {
            id,
            zip_path,
            seat: seat.to_owned(),
            person: tag["person"].as_str().unwrap_or("").to_owned(),
        });
    }
    Ok(Json(state.training.start(training_clips, variant, epochs)?))
}

async fn training_status(
    State(state): State<AppState>,
    axum::extract::Query(params): axum::extract::Query<
        std::collections::HashMap<String, String>,
    >,
) -> Json<Value> {
    let after = params
        .get("after")
        .and_then(|value| value.parse().ok())
        .unwrap_or(0);
    Json(state.training.status(after))
}

async fn inference_models(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    Ok(Json(state.inference.models()?))
}

async fn inference_start(
    State(state): State<AppState>,
    Json(request): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let model = request["model"].as_str().unwrap_or("");
    // Calibrated checkpoints subtract the frozen board references, exactly
    // like the calibrated dataset variant they were trained on.
    let frozen_refs = if model.contains("calibrated") {
        let taps = state.pipeline.lock().frozen_reference_taps();
        if taps.is_empty() { None } else { Some(taps) }
    } else {
        None
    };
    Ok(Json(state.inference.start(model, frozen_refs)?))
}

async fn inference_stop(State(state): State<AppState>) -> Json<Value> {
    Json(state.inference.stop("stopped from dashboard"))
}

async fn inference_status(State(state): State<AppState>) -> Json<Value> {
    Json(state.inference.status())
}

async fn calibration(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    Ok(Json(json!({
        "live": state.pipeline.lock().calibration_snapshot(),
        "applied": state.metadata.calibration_snapshot()?
    })))
}

async fn start_calibration(
    State(state): State<AppState>,
    Json(request): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    Ok(Json(state.pipeline.lock().start_calibration(&request)?))
}

async fn apply_calibration(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    let mut pipeline = state.pipeline.lock();
    let solution = pipeline.solve_calibration()?;
    if !solution.has_full_rank {
        return Err(ApiError(anyhow::anyhow!(
            "calibration cannot be applied until the solve has full rank"
        )));
    }
    pipeline.apply_calibration(&solution);
    let value = json!({"action": "apply", "solution": solution});
    let stored = state.metadata.add_calibration(&value)?;
    Ok(Json(stored))
}

async fn solve_calibration(
    State(state): State<AppState>,
    Json(request): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let mut pipeline = state.pipeline.lock();
    pipeline.set_calibration_references(&request)?;
    Ok(Json(serde_json::to_value(pipeline.solve_calibration()?)?))
}

async fn rollback_calibration(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    let rolled_back = state.pipeline.lock().rollback_calibration();
    if !rolled_back {
        return Err(ApiError(anyhow::anyhow!("no calibration to roll back")));
    }
    let stored = state
        .metadata
        .add_calibration(&json!({"action": "rollback"}))?;
    Ok(Json(stored))
}

async fn stream(ws: WebSocketUpgrade, State(state): State<AppState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| websocket(socket, state))
}

/// Called after any seat-inference demand decrement: if no dashboard is
/// subscribed anymore, give the inference run a grace period to find a new
/// subscriber before shutting the Python process down.
fn maybe_stop_idle_inference(state: &AppState) {
    if state.topic_demand[Topic::SeatInference as usize].load(Ordering::Relaxed) == 0 {
        state.inference.schedule_idle_stop(state.topic_demand.clone());
    }
}

async fn websocket(mut socket: WebSocket, state: AppState) {
    let mut receiver = state.stream.subscribe();
    let demand = state.topic_demand.clone();
    let metrics = state.live_metrics.clone();
    state.web_clients.fetch_add(1, Ordering::Relaxed);
    struct ClientGuard(Arc<AtomicU64>);
    impl Drop for ClientGuard {
        fn drop(&mut self) {
            self.0.fetch_sub(1, Ordering::Relaxed);
        }
    }
    let _guard = ClientGuard(state.web_clients.clone());
    let mut topics = HashSet::new();
    let mut pending = BTreeMap::<String, Vec<u8>>::new();
    let mut publish = tokio::time::interval(Duration::from_millis(16));
    publish.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            message = socket.recv() => match message {
                Some(Ok(Message::Close(_))) | None | Some(Err(_)) => break,
                Some(Ok(Message::Text(text))) => {
                    if let Ok(value) = serde_json::from_str::<Value>(text.as_str())
                        && value["op"] == "subscribe"
                    {
                        let next: HashSet<_> = value["topics"].as_array().into_iter().flatten()
                            .filter_map(Value::as_str).filter_map(Topic::from_subscription).collect();
                        for topic in topics.difference(&next) {
                            demand[*topic as usize].fetch_sub(1, Ordering::Relaxed);
                        }
                        for topic in next.difference(&topics) {
                            demand[*topic as usize].fetch_add(1, Ordering::Relaxed);
                        }
                        let dropped_inference = topics.contains(&Topic::SeatInference)
                            && !next.contains(&Topic::SeatInference);
                        topics = next;
                        if dropped_inference {
                            maybe_stop_idle_inference(&state);
                        }
                    }
                }
                _ => {}
            },
            message = receiver.recv() => match message {
                Ok(bytes) => if envelope_topic(&bytes).is_some_and(|topic| topics.contains(&topic)) {
                    let key = envelope_key(&bytes).unwrap_or_else(|| format!("unknown:{}", pending.len()));
                    if pending.insert(key, bytes).is_some() {
                        metrics.websocket_coalesced.fetch_add(1, Ordering::Relaxed);
                    }
                },
                // Live views can skip stale samples; closing here forces the
                // browser to reset its rolling histories on every brief lag.
                Err(broadcast::error::RecvError::Lagged(count)) => {
                    metrics.websocket_lagged.fetch_add(count, Ordering::Relaxed);
                },
                Err(broadcast::error::RecvError::Closed) => break,
            },
            _ = publish.tick(), if !pending.is_empty() => {
                let messages = std::mem::take(&mut pending);
                let updates = messages.len() as u64;
                let bytes = envelope_batch(messages.into_values());
                let started = std::time::Instant::now();
                let sent = matches!(
                    tokio::time::timeout(
                        Duration::from_millis(50),
                        socket.send(Message::Binary(bytes.into()))
                    ).await,
                    Ok(Ok(()))
                );
                let elapsed = started.elapsed().as_nanos() as u64;
                metrics.websocket_send_count.fetch_add(1, Ordering::Relaxed);
                metrics.websocket_updates.fetch_add(updates, Ordering::Relaxed);
                metrics.websocket_send_ns.fetch_add(elapsed, Ordering::Relaxed);
                record_max(&metrics.websocket_send_max_ns, elapsed);
                if !sent { break; }
            }
        }
    }
    let had_inference = topics.contains(&Topic::SeatInference);
    for topic in topics {
        demand[topic as usize].fetch_sub(1, Ordering::Relaxed);
    }
    if had_inference {
        maybe_stop_idle_inference(&state);
    }
}

fn process_rss_bytes() -> u64 {
    #[cfg(target_os = "linux")]
    {
        let pages = std::fs::read_to_string("/proc/self/statm")
            .ok()
            .and_then(|value| value.split_whitespace().nth(1)?.parse::<u64>().ok())
            .unwrap_or(0);
        pages.saturating_mul(4096)
    }
    #[cfg(not(target_os = "linux"))]
    {
        0
    }
}

async fn asset(Path(path): Path<String>) -> Response {
    let path = path.trim_start_matches('/');
    let selected = ASSETS
        .iter()
        .find(|(name, _)| *name == path)
        .or_else(|| ASSETS.iter().find(|(name, _)| *name == "index.html"));
    let Some((name, bytes)) = selected else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let mime = match name.rsplit('.').next().unwrap_or("") {
        "html" => "text/html; charset=utf-8",
        "js" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "svg" => "image/svg+xml",
        "json" => "application/json",
        "woff2" => "font/woff2",
        _ => "application/octet-stream",
    };
    (
        [
            (header::CONTENT_TYPE, mime),
            (
                header::CACHE_CONTROL,
                if *name == "index.html" {
                    "no-cache"
                } else {
                    "public, max-age=31536000, immutable"
                },
            ),
        ],
        Body::from(*bytes),
    )
        .into_response()
}

struct ApiError(anyhow::Error);
impl From<anyhow::Error> for ApiError {
    fn from(value: anyhow::Error) -> Self {
        Self(value)
    }
}
impl From<serde_json::Error> for ApiError {
    fn from(value: serde_json::Error) -> Self {
        Self(value.into())
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": self.0.to_string()})),
        )
            .into_response()
    }
}

pub async fn serve(listener: tokio::net::TcpListener, state: AppState) -> anyhow::Result<()> {
    axum::serve(listener, router(state))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
            tokio::time::sleep(Duration::from_millis(10)).await;
        })
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    #[tokio::test]
    async fn api_state_and_compatibility_routes() {
        let dir = tempfile::tempdir().unwrap();
        let state = AppState::new(
            Metadata::open(dir.path().join("api.db")).unwrap(),
            dir.path(),
        )
        .unwrap();
        let app = router(state);
        for path in [
            "/api/v1/health",
            "/api/health",
            "/api/v1/topology",
            "/api/settings",
            "/api/v1/board",
            "/api/board",
        ] {
            let response = app
                .clone()
                .oneshot(
                    axum::http::Request::builder()
                        .uri(path)
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::OK, "{path}");
            let body = response.into_body().collect().await.unwrap().to_bytes();
            serde_json::from_slice::<Value>(&body).unwrap();
        }

        let response = app
            .clone()
            .oneshot(
                axum::http::Request::builder()
                    .uri("/api/v1/board")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = response.into_body().collect().await.unwrap().to_bytes();
        let board = serde_json::from_slice::<Value>(&body).unwrap();
        assert_eq!(board["status"], "current");
        assert_eq!(board["board_frozen"], false);
        assert_eq!(board["reference_count"], 0);
        assert!(board["configuration_epoch"].is_u64());
        assert!(board["processing_epoch"].is_u64());

        let response = app
            .clone()
            .oneshot(
                axum::http::Request::builder()
                    .method(axum::http::Method::PUT)
                    .uri("/api/v1/settings")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(r#"{"cfo_half_life_s":3.0}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        let value = serde_json::from_slice::<Value>(&body).unwrap();
        assert_eq!(value["value"]["cfo_half_life_s"], 3.0);
        assert_eq!(value["processing_epoch"], 2);

        for path in [
            "/api/v1/board/freeze",
            "/api/board/unfreeze",
            "/api/board/freeze",
            "/api/v1/board/unfreeze",
        ] {
            let response = app
                .clone()
                .oneshot(
                    axum::http::Request::builder()
                        .method(axum::http::Method::POST)
                        .uri(path)
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::OK, "{path}");
            let body = response.into_body().collect().await.unwrap().to_bytes();
            assert!(serde_json::from_slice::<Value>(&body).unwrap()["board_frozen"].is_boolean());
        }
    }

    #[tokio::test]
    async fn clip_training_tags_persist_in_metadata() {
        let dir = tempfile::tempdir().unwrap();
        let metadata = Metadata::open(dir.path().join("api.db")).unwrap();
        let state = AppState::new(metadata.clone(), dir.path()).unwrap();
        let row = metadata
            .add_clip(&json!({"status": "complete", "name": "seat test", "duration_s": 10}))
            .unwrap();
        let id = row["id"].as_i64().unwrap();
        let app = router(state);

        let tag_request = |body: &str| {
            axum::http::Request::builder()
                .method(axum::http::Method::POST)
                .uri(format!("/api/clips/{id}/training"))
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(body.to_owned()))
                .unwrap()
        };

        let response = app
            .clone()
            .oneshot(tag_request(r#"{"seat":"BackLeft","person":"Simarjit","exclude":false}"#))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let stored = metadata.clip(id).unwrap().unwrap();
        assert_eq!(stored["value"]["training"]["seat"], "BackLeft");
        assert_eq!(stored["value"]["training"]["person"], "Simarjit");
        assert_eq!(stored["value"]["name"], "seat test");

        let response = app
            .clone()
            .oneshot(tag_request(r#"{"seat":"MiddleSeat"}"#))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);

        let response = app
            .clone()
            .oneshot(tag_request(r#"{"seat":null}"#))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let cleared = metadata.clip(id).unwrap().unwrap();
        assert!(cleared["value"]["training"].is_null());

        let response = app
            .clone()
            .oneshot(
                axum::http::Request::builder()
                    .method(axum::http::Method::POST)
                    .uri("/api/training/run")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(r#"{"variant":"raw"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        let error = serde_json::from_slice::<Value>(&body).unwrap()["error"]
            .as_str()
            .unwrap()
            .to_owned();
        assert!(error.contains("at least one tagged clip"), "{error}");

        let status = app
            .clone()
            .oneshot(
                axum::http::Request::builder()
                    .uri("/api/training/status?after=0")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(status.status(), StatusCode::OK);
        let body = status.into_body().collect().await.unwrap().to_bytes();
        let payload = serde_json::from_slice::<Value>(&body).unwrap();
        assert_eq!(payload["status"], "idle");
        assert_eq!(payload["log_next"], 0);
    }
}
