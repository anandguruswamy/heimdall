use std::collections::{HashMap, VecDeque};

use crate::METRES_PER_DTU;

pub const U40_MASK: u64 = (1_u64 << 40) - 1;
const U40_HALF: u64 = 1_u64 << 39;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct QualityFlags(pub u32);

impl QualityFlags {
    pub const NONE: Self = Self(0);
    pub const WRAPPED_TIMESTAMP: Self = Self(1 << 0);
    pub const CFO_APPLIED: Self = Self(1 << 1);
    pub const ANALYTICAL_GAP: Self = Self(1 << 2);
    pub const INVALID_INTERVAL: Self = Self(1 << 3);
    pub const INTERPOLATED_GAP: Self = Self(1 << 4);
    pub const LOW_CORRELATION: Self = Self(1 << 5);
    pub const LOW_ACCUMULATION: Self = Self(1 << 6);
    pub const NEGATIVE_RANGE: Self = Self(1 << 7);

    pub fn contains(self, other: Self) -> bool {
        self.0 & other.0 != 0
    }
    pub fn insert(&mut self, other: Self) {
        self.0 |= other.0;
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Evidence {
    pub ids: Vec<u64>,
    pub quality: QualityFlags,
}

#[derive(Clone, Copy, Debug)]
pub struct SsTwrInput {
    pub poll_tx: u64,
    pub response_rx: u64,
    pub poll_rx: u64,
    pub response_tx: u64,
    /// Remote/local fractional clock ratio minus one.
    pub remote_clock_offset: Option<f64>,
}

#[derive(Clone, Copy, Debug)]
pub struct DsTwrInput {
    pub poll_tx: u64,
    pub poll_rx: u64,
    pub response_tx: u64,
    pub response_rx: u64,
    pub final_tx: u64,
    pub final_rx: u64,
}

#[derive(Clone, Debug)]
pub struct RangeEstimate {
    pub tof_dtu: f64,
    pub distance_m: f64,
    pub evidence: Evidence,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RangeError {
    InvalidTimestamp,
    NonPhysical,
    NonFinite,
}

fn interval(start: u64, end: u64, flags: &mut QualityFlags) -> Result<u64, RangeError> {
    if start > U40_MASK || end > U40_MASK {
        return Err(RangeError::InvalidTimestamp);
    }
    let d = end.wrapping_sub(start) & U40_MASK;
    if end < start {
        flags.insert(QualityFlags::WRAPPED_TIMESTAMP);
    }
    // Modular intervals at or above half-range are directionally ambiguous.
    if d >= U40_HALF {
        flags.insert(QualityFlags::INVALID_INTERVAL);
        return Err(RangeError::InvalidTimestamp);
    }
    Ok(d)
}

pub fn ss_twr(
    input: SsTwrInput,
    evidence_ids: impl Into<Vec<u64>>,
) -> Result<RangeEstimate, RangeError> {
    let mut quality = QualityFlags::NONE;
    let round = interval(input.poll_tx, input.response_rx, &mut quality)? as f64;
    let reply = interval(input.poll_rx, input.response_tx, &mut quality)? as f64;
    let clock_offset = input.remote_clock_offset.unwrap_or(0.0);
    if !clock_offset.is_finite() || clock_offset.abs() >= 1.0 {
        return Err(RangeError::NonFinite);
    }
    if input.remote_clock_offset.is_some() {
        quality.insert(QualityFlags::CFO_APPLIED);
    }
    // SS-TWR cannot remove changing clock skew or antenna delay by itself.
    quality.insert(QualityFlags::ANALYTICAL_GAP);
    // Match the validated DW3000 primitive: convert the responder's interval
    // into the initiator clock domain using the receiver-reported ratio.
    let tof = (round - reply * (1.0 - clock_offset)) * 0.5;
    finish(tof, evidence_ids.into(), quality)
}

pub fn asymmetric_ds_twr(
    input: DsTwrInput,
    evidence_ids: impl Into<Vec<u64>>,
) -> Result<RangeEstimate, RangeError> {
    let mut quality = QualityFlags::NONE;
    let ra = interval(input.poll_tx, input.response_rx, &mut quality)? as i128;
    let da = interval(input.poll_rx, input.response_tx, &mut quality)? as i128;
    let rb = interval(input.response_tx, input.final_rx, &mut quality)? as i128;
    let db = interval(input.response_rx, input.final_tx, &mut quality)? as i128;
    let numerator = ra * rb - da * db;
    let denominator = ra + rb + da + db;
    if denominator <= 0 {
        return Err(RangeError::NonPhysical);
    }
    // The timestamp solution does not account for antenna delay or NLOS bias.
    quality.insert(QualityFlags::ANALYTICAL_GAP);
    finish(
        numerator as f64 / denominator as f64,
        evidence_ids.into(),
        quality,
    )
}

fn finish(tof: f64, ids: Vec<u64>, mut quality: QualityFlags) -> Result<RangeEstimate, RangeError> {
    if !tof.is_finite() {
        return Err(RangeError::NonFinite);
    }
    if tof < 0.0 {
        quality.insert(QualityFlags::NEGATIVE_RANGE);
    }
    Ok(RangeEstimate {
        tof_dtu: tof,
        distance_m: tof * METRES_PER_DTU,
        evidence: Evidence { ids, quality },
    })
}

/// Event-time leaky integrator. Direction is part of the state key at the call site.
#[derive(Clone, Debug)]
pub struct CfoIntegrator {
    half_life_s: f64,
    value: Option<f64>,
    last_time_s: Option<f64>,
}

impl CfoIntegrator {
    pub fn new(half_life_s: f64) -> Self {
        assert!(half_life_s > 0.0);
        Self {
            half_life_s,
            value: None,
            last_time_s: None,
        }
    }
    pub fn update(&mut self, event_time_s: f64, sample: f64) -> f64 {
        let next = match (self.value, self.last_time_s) {
            (Some(old), Some(last)) if event_time_s > last => {
                let retain = 2.0_f64.powf(-(event_time_s - last) / self.half_life_s);
                old * retain + sample * (1.0 - retain)
            }
            (Some(old), _) => old,
            _ => sample,
        };
        if self.last_time_s.is_none_or(|t| event_time_s >= t) {
            self.value = Some(next);
            self.last_time_s = Some(event_time_s);
        }
        next
    }
    pub fn value(&self) -> Option<f64> {
        self.value
    }
}

/// Maintains independent CFO state for each directed source/destination link.
#[derive(Clone, Debug)]
pub struct DirectionalCfoIntegrator {
    half_life_s: f64,
    links: HashMap<(u32, u32), CfoIntegrator>,
}

impl DirectionalCfoIntegrator {
    pub fn new(half_life_s: f64) -> Self {
        assert!(half_life_s > 0.0);
        Self {
            half_life_s,
            links: HashMap::new(),
        }
    }

    pub fn update(&mut self, source: u32, destination: u32, event_time_s: f64, sample: f64) -> f64 {
        self.links
            .entry((source, destination))
            .or_insert_with(|| CfoIntegrator::new(self.half_life_s))
            .update(event_time_s, sample)
    }

    pub fn value(&self, source: u32, destination: u32) -> Option<f64> {
        self.links
            .get(&(source, destination))
            .and_then(CfoIntegrator::value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MessageKind {
    Poll,
    Response,
    Final,
}

#[derive(Clone, Debug)]
pub struct RangingMessage {
    pub evidence_id: u64,
    pub round_id: u64,
    pub source: u32,
    pub destination: u32,
    pub sequence: u32,
    pub kind: MessageKind,
    pub event_time_s: f64,
}

#[derive(Clone, Debug)]
pub enum MatchedExchange {
    Ss([RangingMessage; 2]),
    Ds([RangingMessage; 3]),
}

/// Keeps enough arrival history for adjacent SS and one-message-bridged DS matches.
#[derive(Default)]
pub struct ExchangeMatcher {
    history: VecDeque<RangingMessage>,
}

impl ExchangeMatcher {
    pub fn push(&mut self, message: RangingMessage) -> Vec<MatchedExchange> {
        self.history.push_back(message);
        while self.history.len() > 4 {
            self.history.pop_front();
        }
        let mut out = Vec::new();
        let n = self.history.len();
        if n >= 2 {
            let a = &self.history[n - 2];
            let b = &self.history[n - 1];
            if exchange_pair(a, b)
                && a.kind == MessageKind::Poll
                && b.kind == MessageKind::Response
                && b.sequence == a.sequence.wrapping_add(1)
            {
                out.push(MatchedExchange::Ss([a.clone(), b.clone()]));
            }
        }
        if n >= 3 {
            let end = &self.history[n - 1];
            if end.kind == MessageKind::Final {
                // Up to one unrelated arrival may bridge response and final.
                for gap in 0..=1 {
                    if n < 3 + gap {
                        continue;
                    }
                    let p = &self.history[n - 3 - gap];
                    let r = &self.history[n - 2 - gap];
                    if p.kind == MessageKind::Poll
                        && r.kind == MessageKind::Response
                        && exchange_pair(p, r)
                        && exchange_pair(r, end)
                        && r.sequence == p.sequence.wrapping_add(1)
                        && end.sequence == r.sequence.wrapping_add(1)
                        && end.event_time_s - p.event_time_s <= 0.100
                        && end.event_time_s >= p.event_time_s
                    {
                        out.push(MatchedExchange::Ds([p.clone(), r.clone(), end.clone()]));
                        break;
                    }
                }
            }
        }
        out
    }
}

fn exchange_pair(a: &RangingMessage, b: &RangingMessage) -> bool {
    a.round_id == b.round_id && a.source == b.destination && a.destination == b.source
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn u40_wrap_ss_and_known_distance() {
        let base = U40_MASK - 100;
        let r = ss_twr(
            SsTwrInput {
                poll_tx: base,
                response_rx: (base + 1200) & U40_MASK,
                poll_rx: 50,
                response_tx: 1050,
                remote_clock_offset: None,
            },
            vec![1, 2],
        )
        .unwrap();
        assert!((r.tof_dtu - 100.0).abs() < 1e-12);
        assert!(r.evidence.quality.contains(QualityFlags::WRAPPED_TIMESTAMP));
    }
    #[test]
    fn cfo_half_life_and_ss_skew() {
        let mut c = CfoIntegrator::new(2.0);
        assert_eq!(c.update(0.0, 0.0), 0.0);
        assert!((c.update(2.0, 1.0) - 0.5).abs() < 1e-12);
        let r = ss_twr(
            SsTwrInput {
                poll_tx: 0,
                response_rx: 1200,
                poll_rx: 0,
                response_tx: 1001,
                remote_clock_offset: Some(1.0 / 1001.0),
            },
            vec![],
        )
        .unwrap();
        assert!((r.tof_dtu - 100.0).abs() < 1e-9);
    }
    #[test]
    fn ds_synthetic_and_i128_products() {
        let r = asymmetric_ds_twr(
            DsTwrInput {
                poll_tx: 0,
                poll_rx: 100,
                response_tx: 500_000_000_100,
                response_rx: 500_000_000_200,
                final_tx: 600_000_000_200,
                final_rx: 600_000_000_300,
            },
            vec![1, 2, 3],
        )
        .unwrap();
        assert!((r.tof_dtu - 100.0).abs() < 1e-6);
        let delay_a = (1_u64 << 38) - 1000;
        let delay_b = (1_u64 << 38) - 2000;
        let r = asymmetric_ds_twr(
            DsTwrInput {
                poll_tx: 0,
                poll_rx: 100,
                response_tx: delay_a + 100,
                response_rx: delay_a + 200,
                final_tx: delay_a + delay_b + 200,
                final_rx: delay_a + delay_b + 300,
            },
            vec![],
        )
        .unwrap();
        assert!((r.tof_dtu - 100.0).abs() < 1e-3);
    }
    #[test]
    fn negative_ranges_are_retained_and_flagged() {
        let ss = ss_twr(
            SsTwrInput {
                poll_tx: 0,
                response_rx: 800,
                poll_rx: 100,
                response_tx: 1100,
                remote_clock_offset: None,
            },
            vec![],
        )
        .unwrap();
        assert!((ss.tof_dtu + 100.0).abs() < 1e-12);
        assert!(ss.evidence.quality.contains(QualityFlags::NEGATIVE_RANGE));

        let ds = asymmetric_ds_twr(
            DsTwrInput {
                poll_tx: 0,
                poll_rx: 100,
                response_tx: 1100,
                response_rx: 800,
                final_tx: 1800,
                final_rx: 1900,
            },
            vec![],
        )
        .unwrap();
        assert!((ds.tof_dtu + 100.0).abs() < 1e-12);
        assert!(ds.evidence.quality.contains(QualityFlags::NEGATIVE_RANGE));
    }
    #[test]
    fn matcher_adjacency_bridge_and_bound() {
        fn m(id: u64, k: MessageKind, seq: u32, t: f64) -> RangingMessage {
            RangingMessage {
                evidence_id: id,
                round_id: 7,
                source: if k == MessageKind::Response { 2 } else { 1 },
                destination: if k == MessageKind::Response { 1 } else { 2 },
                sequence: seq,
                kind: k,
                event_time_s: t,
            }
        }
        let mut x = ExchangeMatcher::default();
        x.push(m(1, MessageKind::Poll, 1, 0.0));
        assert!(matches!(
            x.push(m(2, MessageKind::Response, 2, 0.02))[0],
            MatchedExchange::Ss(_)
        ));
        let unrelated = RangingMessage {
            round_id: 99,
            ..m(9, MessageKind::Poll, 9, 0.03)
        };
        x.push(unrelated);
        assert!(
            x.push(m(3, MessageKind::Final, 3, 0.09))
                .iter()
                .any(|v| matches!(v, MatchedExchange::Ds(_)))
        );
        x.push(m(4, MessageKind::Poll, 4, 1.0));
        x.push(m(5, MessageKind::Response, 5, 1.01));
        assert!(
            !x.push(m(6, MessageKind::Final, 6, 1.101))
                .iter()
                .any(|v| matches!(v, MatchedExchange::Ds(_)))
        );
    }

    #[test]
    fn cfo_is_directional() {
        let mut c = DirectionalCfoIntegrator::new(1.0);
        c.update(1, 2, 0.0, 0.001);
        c.update(2, 1, 0.0, -0.002);
        assert_eq!(c.value(1, 2), Some(0.001));
        assert_eq!(c.value(2, 1), Some(-0.002));
    }
}
