use std::{
    collections::HashSet,
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
    routing::get,
};
use parking_lot::Mutex;
use serde_json::{Value, json};
use tokio::sync::broadcast;
use tower_http::{cors::CorsLayer, trace::TraceLayer};

use crate::{
    clips::ClipManager,
    metadata::Metadata,
    pipeline::Pipeline,
    telemetry::{Topic, envelope_topic},
};

include!(concat!(env!("OUT_DIR"), "/assets.rs"));

#[derive(Clone)]
pub struct AppState {
    pub pipeline: Arc<Mutex<Pipeline>>,
    pub metadata: Arc<Metadata>,
    pub clips: ClipManager,
    pub stream: broadcast::Sender<Vec<u8>>,
    pub processing_drops: Arc<AtomicU64>,
    pub archive_errors: Arc<AtomicU64>,
    pub archive_paused: Arc<std::sync::atomic::AtomicBool>,
    pub archive_last_error: Arc<Mutex<Option<String>>>,
    pub web_clients: Arc<AtomicU64>,
    pub topic_demand: Arc<[AtomicU64; 8]>,
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
            metadata,
            stream,
            processing_drops: Arc::new(AtomicU64::new(0)),
            archive_errors: Arc::new(AtomicU64::new(0)),
            archive_paused: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            archive_last_error: Arc::new(Mutex::new(None)),
            web_clients: Arc::new(AtomicU64::new(0)),
            topic_demand: Arc::new(std::array::from_fn(|_| AtomicU64::new(0))),
            data_root: Arc::new(data_root),
            started: std::time::Instant::now(),
        })
    }

    pub fn topic_mask(&self) -> u8 {
        self.topic_demand
            .iter()
            .enumerate()
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
        .route("/api/v1/clips", get(clips).post(post_clip))
        .route("/api/v1/clips/{id}", get(download_clip).delete(delete_clip))
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
        .route("/api/clips", get(clips).post(post_clip))
        .route("/api/clips/{id}", get(download_clip).delete(delete_clip))
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
    let pipeline = state.pipeline.lock();
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
    let free_percent = if total_bytes == 0 {
        None
    } else {
        Some(free_bytes as f64 * 100.0 / total_bytes as f64)
    };
    Json(json!({
        "status": if archive_paused { "degraded" } else { "ok" }, "uptime_seconds": state.started.elapsed().as_secs_f64(),
        "pipeline": pipeline.summary(),
        "processing_queue_drops": state.processing_drops.load(Ordering::Relaxed),
        "websocket_clients": state.web_clients.load(Ordering::Relaxed),
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

async fn clips(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    Ok(Json(Value::Array(state.metadata.clips()?)))
}

async fn post_clip(
    State(state): State<AppState>,
    Json(value): Json<Value>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let context = {
        let pipeline = state.pipeline.lock();
        json!({
            "software": {"name": "heimdall-service", "version": env!("CARGO_PKG_VERSION")},
            "pipeline": pipeline.summary(), "settings": pipeline.settings(),
            "calibration": state.metadata.calibration_snapshot()?
        })
    };
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
    ws.on_upgrade(move |socket| {
        websocket(
            socket,
            state.stream.subscribe(),
            state.web_clients,
            state.topic_demand,
        )
    })
}

async fn websocket(
    mut socket: WebSocket,
    mut receiver: broadcast::Receiver<Vec<u8>>,
    clients: Arc<AtomicU64>,
    demand: Arc<[AtomicU64; 8]>,
) {
    clients.fetch_add(1, Ordering::Relaxed);
    struct ClientGuard(Arc<AtomicU64>);
    impl Drop for ClientGuard {
        fn drop(&mut self) {
            self.0.fetch_sub(1, Ordering::Relaxed);
        }
    }
    let _guard = ClientGuard(clients);
    let mut topics = HashSet::new();
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
                        topics = next;
                    }
                }
                _ => {}
            },
            message = receiver.recv() => match message {
                Ok(bytes) => if envelope_topic(&bytes).is_some_and(|topic| topics.contains(&topic))
                    && socket.send(Message::Binary(bytes.into())).await.is_err() { break; },
                Err(broadcast::error::RecvError::Lagged(_)) => {
                    let _ = socket.send(Message::Close(None)).await;
                    break;
                },
                Err(broadcast::error::RecvError::Closed) => break,
            }
        }
    }
    for topic in topics {
        demand[topic as usize].fetch_sub(1, Ordering::Relaxed);
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
    }
}
