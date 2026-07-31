use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Instant,
};

use axum::{Json, Router, extract::State, response::Html, routing::get};
use heimdall_protocol::{DecodedRecord, DecoderState, Record, StreamParser, decode_record};
use parking_lot::Mutex;
use serde_json::{Value, json};

#[derive(Clone)]
pub struct AgentState {
    inner: Arc<Mutex<AgentHealth>>,
    pub forwarded: Arc<AtomicU64>,
    pub send_drops: Arc<AtomicU64>,
    started: Instant,
}

#[derive(Default)]
struct AgentHealth {
    parser: StreamParser,
    decoder: DecoderState,
    nodes: BTreeMap<u8, NodeHealth>,
    expected_nodes: u8,
    gateway_node: Option<u8>,
    last_cycle: Option<u32>,
    decode_errors: u64,
}

#[derive(Clone)]
struct NodeHealth {
    last_seen: Instant,
    signal_power: u32,
}

impl AgentState {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(AgentHealth::default())),
            forwarded: Arc::new(AtomicU64::new(0)),
            send_drops: Arc::new(AtomicU64::new(0)),
            started: Instant::now(),
        }
    }

    /// Returns only CRC-valid, sequence-current USB records for live forwarding.
    pub fn ingest(&self, bytes: &[u8]) -> Vec<Record> {
        let mut health = self.inner.lock();
        let records = health.parser.feed(bytes);
        for record in &records {
            match decode_record(record, &mut health.decoder) {
                Ok(DecodedRecord::Hello(hello)) => {
                    health.expected_nodes = hello.n_nodes;
                    health.gateway_node = Some(hello.node_id);
                }
                Ok(DecodedRecord::LocalObservation(observation)) => {
                    health.nodes.insert(
                        observation.subreport.observed_node_id,
                        NodeHealth {
                            last_seen: Instant::now(),
                            signal_power: observation.subreport.ip_power,
                        },
                    );
                }
                Ok(DecodedRecord::CycleSummary(summary)) => {
                    health.last_cycle = Some(summary.cycle_index)
                }
                Ok(_) => {}
                Err(_) => health.decode_errors += 1,
            }
        }
        records
    }

    fn snapshot(&self) -> Value {
        let health = self.inner.lock();
        let stats = health.parser.stats();
        let nodes: Vec<Value> = health
            .nodes
            .iter()
            .map(|(id, node)| {
                json!({
                    "node_id": id,
                    "age_ms": node.last_seen.elapsed().as_millis(),
                    "signal_power": node.signal_power,
                    "connected": node.last_seen.elapsed().as_secs_f32() < 2.5,
                })
            })
            .collect();
        Json(json!({
            "status": "ok",
            "uptime_seconds": self.started.elapsed().as_secs_f64(),
            "expected_nodes": health.expected_nodes,
            "gateway_node": health.gateway_node,
            "active_nodes": nodes.iter().filter(|node| node["connected"].as_bool() == Some(true)).count(),
            "last_cycle": health.last_cycle,
            "nodes": nodes,
            "usb": {"crc_failures": stats.crc_failures, "framing_errors": stats.framing_errors,
                "sequence_gaps": stats.sequence_gaps, "duplicates_or_old": stats.duplicates_or_old,
                "decode_errors": health.decode_errors},
            "udp": {"forwarded_records": self.forwarded.load(Ordering::Relaxed),
                "send_drops": self.send_drops.load(Ordering::Relaxed)}
        })).0
    }
}

pub fn router(state: AgentState) -> Router {
    Router::new()
        .route("/", get(dashboard))
        .route("/api/health", get(health))
        .route("/api/topology", get(health))
        .with_state(state)
}

async fn health(State(state): State<AgentState>) -> Json<Value> {
    Json(state.snapshot())
}

async fn dashboard() -> Html<&'static str> {
    Html(
        r#"<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Heimdall Agent</title><style>body{font:16px system-ui;max-width:48rem;margin:2rem auto;padding:0 1rem}pre{background:#111;color:#b7f7b7;padding:1rem;overflow:auto}</style><h1>Heimdall UNO Q Health</h1><pre id="status">Loading...</pre><script>const out=document.querySelector('#status');async function refresh(){try{out.textContent=JSON.stringify(await (await fetch('/api/health')).json(),null,2)}catch(e){out.textContent=e}}refresh();setInterval(refresh,1000)</script>"#,
    )
}
