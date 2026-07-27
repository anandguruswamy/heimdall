use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    time::Instant,
};

use anyhow::{Result, bail};
use heimdall_dsp::{
    CirReference, DirectionalCfoIntegrator, DsTwrInput, FftWindow, PairCalibrationObservation,
    QualityFlags, ReferenceMode, SsTwrInput, TimeMovingAverage, asymmetric_ds_twr,
    calibrate_offsets, common_phase, fast_fft, fast_fft_complex, fractional_align_non_circular,
    hampel, interpolate_short_gaps, normalized_correlation_delay, resample_cir_16x, scale_cir,
    ss_twr,
};
use heimdall_protocol::{
    CanonicalObservation, CanonicalProcessor, DecodedRecord, HelloRecord, ParserStats,
    ProtocolError, StreamParser,
};
use num_complex::Complex64;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::telemetry::{Topic, envelope};

const HISTORY_SECONDS: f64 = 30.0;
const DISTANCE_HISTORY_SECONDS: f64 = 300.0;
const TIMING_HISTORY_SECONDS: f64 = 0.120;
const MAX_CIR_FRAMES: usize = 1024;
const MAX_TIMING_OBSERVATIONS: usize = 32;
const MAX_DISTANCE_SAMPLES: usize = 1_024;
const MAX_DISTANCE_HISTORY_SAMPLES: usize = 2_048;
const SLOW_FFT_TAPS: usize = 64;
pub const QUALITY_BRIDGED_SPAN: u32 = 1 << 30;

pub struct Pipeline {
    parser: StreamParser,
    canonical: CanonicalProcessor,
    cfo: DirectionalCfoIntegrator,
    links: BTreeMap<(u8, u8), LinkState>,
    pairs: BTreeMap<(u8, u8), PairState>,
    clock: RoundClock,
    config: Option<ConfigInfo>,
    settings: DspSettings,
    calibration: Option<CalibrationCollection>,
    host_offsets: BTreeMap<u8, f64>,
    offset_history: Vec<BTreeMap<u8, f64>>,
    configuration_epoch: u64,
    processing_epoch: u64,
    stream_sequence: u64,
    records: u64,
    observations: u64,
    prehello_skipped: u64,
    rejected: u64,
}

struct LinkState {
    reference: CirReference,
    cir: Vec<CirFrame>,
    observations: u64,
    first_event_s: Option<f64>,
    latest_event_s: Option<f64>,
    last_slow_fft_s: Option<f64>,
    latest_cfo_ppm: Option<f64>,
    cfo_history: Vec<(f64, f64, f64)>,
    latest_fast_fft: Option<Value>,
    latest_slow_fft: Option<Value>,
}

#[derive(Clone)]
struct CirFrame {
    event_tick: i64,
    event_s: f64,
    round: u32,
    usb_sequence: u32,
    aligned: Vec<Complex64>,
    magnitude_16x: Vec<f32>,
    marker_raw: f64,
    marker_aligned: f64,
    correlation: f64,
    quality: u32,
}

#[derive(Default)]
struct PairState {
    timing: Vec<TimingObservation>,
    processed_ss: BTreeSet<(u64, u64)>,
    processed_ds: BTreeSet<(u64, u64, u64)>,
    distances: VecDeque<DistanceSample>,
    distance_evidence: BTreeSet<Vec<u64>>,
    distance_history: VecDeque<DistanceSample>,
    distance_history_last_event: BTreeMap<(&'static str, u8, u8), f64>,
}

#[derive(Clone)]
struct TimingObservation {
    event_tick: i64,
    event_s: f64,
    round: u32,
    from: u8,
    to: u8,
    tx: u64,
    rx: u64,
    cfo_ratio: f64,
    evidence_id: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct DistanceSample {
    pub event_s: f64,
    pub round: u32,
    pub kind: &'static str,
    pub from: u8,
    pub to: u8,
    pub raw_m: f64,
    #[serde(skip)]
    pub raw_mm: i64,
    pub calibrated_m: f64,
    #[serde(skip)]
    pub calibrated_mm: i64,
    #[serde(skip)]
    pub hampel_m: f64,
    pub moving_average_m: f64,
    #[serde(skip)]
    pub moving_average_mm: i64,
    pub outlier: bool,
    pub bridged: bool,
    #[serde(skip)]
    pub bridge_duration_s: Option<f64>,
    pub quality: u32,
    #[serde(skip)]
    pub evidence: Vec<u64>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ConfigInfo {
    pub n_nodes: u8,
    pub m_slots: u8,
    pub node_id: u8,
    pub master_node_id: u8,
    pub config_hash: u16,
    pub cir_taps: u8,
    pub cir_left_taps: u8,
    pub slot_duration_us: u32,
    pub superslot_duration_us: u64,
    pub cycle_us: u32,
    pub device_id: u64,
    pub firmware_id: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct DspSettings {
    pub cfo_half_life_s: f64,
    pub distance_smoothing_s: f64,
    pub hampel_radius: usize,
    pub hampel_threshold_sigma: f64,
    pub fft_window: String,
    pub reference_mode: String,
    pub reference_minimum_energy: f64,
    pub reference_half_life_s: f64,
    pub cir_max_lag: usize,
    pub slow_fft_cadence_s: f64,
    pub slow_fft_max_gap: usize,
    pub slow_fft_history_s: f64,
    pub fast_time_sample_rate_hz: f64,
}

impl Default for DspSettings {
    fn default() -> Self {
        Self {
            cfo_half_life_s: 2.0,
            distance_smoothing_s: 1.0,
            hampel_radius: 5,
            hampel_threshold_sigma: 3.0,
            fft_window: "hann".to_owned(),
            reference_mode: "qualified".to_owned(),
            reference_minimum_energy: 1e-9,
            reference_half_life_s: 4.0,
            cir_max_lag: 8,
            slow_fft_cadence_s: 1.0,
            slow_fft_max_gap: 2,
            slow_fft_history_s: 2.0,
            fast_time_sample_rate_hz: 998_400_000.0,
        }
    }
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct PipelineSummary {
    pub records: u64,
    pub observations: u64,
    pub prehello_skipped: u64,
    pub rejected: u64,
    pub links_with_samples: usize,
    pub expected_links: usize,
    pub configuration_epoch: u64,
    pub processing_epoch: u64,
    pub config: Option<ConfigInfo>,
    pub parser: ParserHealth,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ParserHealth {
    pub buffered_bytes: usize,
    pub crc_failures: u64,
    pub framing_errors: u64,
    pub unsupported_versions: u64,
    pub unknown_types: u64,
    pub sequence_gaps: u64,
    pub duplicates_or_old: u64,
}

#[derive(Debug, Clone)]
struct CalibrationCollection {
    started: Instant,
    duration_s: f64,
    samples: BTreeMap<(u8, u8), Vec<f64>>,
    references_m: BTreeMap<(u8, u8), f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CalibrationSolution {
    pub elapsed_s: f64,
    pub complete: bool,
    pub pairs: Vec<CalibrationPair>,
    pub board_offsets: Vec<(u32, f64)>,
    pub residuals: Vec<f64>,
    pub rank: usize,
    pub columns: usize,
    pub condition_number: f64,
    pub has_full_rank: bool,
    pub residual_rmse_m: f64,
    pub poor_fit: bool,
    pub regularization: f64,
    pub recommended_next_pair: Option<(u32, u32)>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CalibrationPair {
    pub a: u8,
    pub b: u8,
    pub samples: usize,
    pub measured_mean_m: f64,
    pub reference_m: Option<f64>,
    pub measured_bias_m: Option<f64>,
    pub variance_m2: f64,
}

#[derive(Default)]
struct RoundClock {
    anchor_k: Option<u32>,
    anchor_tick: i64,
    superslot_s: f64,
}

impl RoundClock {
    fn reset(&mut self, superslot_s: f64) {
        self.anchor_k = None;
        self.anchor_tick = 0;
        self.superslot_s = superslot_s;
    }

    fn event(&mut self, k: u32) -> (i64, f64) {
        let tick = if let Some(anchor_k) = self.anchor_k {
            self.anchor_tick + k.wrapping_sub(anchor_k) as i32 as i64
        } else {
            self.anchor_k = Some(k);
            0
        };
        if tick > self.anchor_tick || self.anchor_k.is_none() {
            self.anchor_k = Some(k);
            self.anchor_tick = tick;
        }
        (tick, tick as f64 * self.superslot_s)
    }
}

impl Default for Pipeline {
    fn default() -> Self {
        Self::new()
    }
}

impl Pipeline {
    pub fn new() -> Self {
        let settings = DspSettings::default();
        Self {
            parser: StreamParser::default(),
            canonical: CanonicalProcessor::new(),
            cfo: DirectionalCfoIntegrator::new(settings.cfo_half_life_s),
            links: BTreeMap::new(),
            pairs: BTreeMap::new(),
            clock: RoundClock::default(),
            config: None,
            settings,
            calibration: None,
            host_offsets: BTreeMap::new(),
            offset_history: Vec::new(),
            configuration_epoch: 0,
            processing_epoch: 1,
            stream_sequence: 0,
            records: 0,
            observations: 0,
            prehello_skipped: 0,
            rejected: 0,
        }
    }

    pub fn feed(&mut self, bytes: &[u8]) -> Vec<Vec<u8>> {
        self.feed_with_topics(bytes, u8::MAX)
    }

    pub fn reset_connection(&mut self) {
        self.parser = StreamParser::default();
        self.canonical = CanonicalProcessor::new();
        self.config = None;
        self.links.clear();
        self.pairs.clear();
        self.cfo = DirectionalCfoIntegrator::new(self.settings.cfo_half_life_s);
        self.clock = RoundClock::default();
        self.calibration = None;
    }

    pub fn feed_with_stream(&mut self, bytes: &[u8], emit_stream: bool) -> Vec<Vec<u8>> {
        self.feed_with_topics(bytes, if emit_stream { u8::MAX } else { 0 })
    }

    pub fn feed_with_topics(&mut self, bytes: &[u8], topics: u8) -> Vec<Vec<u8>> {
        let mut messages = Vec::new();
        for record in self.parser.feed(bytes) {
            self.records += 1;
            match self.canonical.process(&record) {
                Ok(output) => {
                    if let DecodedRecord::Hello(hello) = &output.decoded {
                        if output.configuration_changed {
                            self.configure(hello);
                        }
                    }
                    for observation in output.observations {
                        messages.extend(self.consume_canonical_inner(observation, topics));
                    }
                }
                Err(ProtocolError::Invalid("HELLO is required before this record")) => {
                    self.prehello_skipped += 1;
                }
                Err(_) => self.rejected += 1,
            }
        }
        messages
    }

    pub fn configure(&mut self, hello: &HelloRecord) {
        let superslot_duration_us = hello.m_slots as u64 * hello.slot_duration_us as u64;
        self.config = Some(ConfigInfo {
            n_nodes: hello.n_nodes,
            m_slots: hello.m_slots,
            node_id: hello.node_id,
            master_node_id: hello.master_node_id,
            config_hash: hello.config_hash,
            cir_taps: hello.cir_taps,
            cir_left_taps: hello.cir_left_taps,
            slot_duration_us: hello.slot_duration_us,
            superslot_duration_us,
            cycle_us: hello.cycle_us,
            device_id: hello.device_id,
            firmware_id: hello.firmware_id,
        });
        self.configuration_epoch += 1;
        self.links.clear();
        self.pairs.clear();
        self.cfo = DirectionalCfoIntegrator::new(self.settings.cfo_half_life_s);
        self.calibration = None;
        self.clock.reset(superslot_duration_us as f64 / 1_000_000.0);
    }

    /// Public deterministic hook used by replay adapters and synthetic integration tests.
    pub fn consume_canonical(&mut self, observation: CanonicalObservation) -> Vec<Vec<u8>> {
        self.consume_canonical_inner(observation, u8::MAX)
    }

    fn consume_canonical_inner(
        &mut self,
        observation: CanonicalObservation,
        topics: u8,
    ) -> Vec<Vec<u8>> {
        if self.config.is_none() {
            self.rejected += 1;
            return Vec::new();
        }
        self.observations += 1;
        let from = observation.observed_node_id;
        let to = observation.reporting_node_id;
        let (event_tick, event_s) = self.clock.event(observation.observed_k);
        let cfo_ratio = observation.cfo_raw as f64 / (1_u64 << 26) as f64;
        let cfo_filtered = self.cfo.update(from as u32, to as u32, event_s, cfo_ratio);
        let evidence_id = (observation.usb_sequence as u64) << 32 | observation.observed_k as u64;

        let mut messages = Vec::new();
        if topics & Topic::Cfo.bit() != 0 {
            messages.push(self.message(
                Topic::Cfo,
                json!({
                    "from": from, "to": to, "round": observation.observed_k,
                    "event_s": event_s, "raw_ratio": cfo_ratio, "raw_ppm": cfo_ratio * 1_000_000.0,
                    "filtered_ppm": cfo_filtered * 1_000_000.0, "half_life_s": self.settings.cfo_half_life_s,
                    "filter_revision": self.processing_epoch, "evidence": evidence_id
                }),
            ));
        }
        messages.extend(self.consume_cir(&observation, event_tick, event_s, cfo_filtered, topics));
        messages.extend(self.consume_timing(
            TimingObservation {
                event_tick,
                event_s,
                round: observation.observed_k,
                from,
                to,
                tx: observation.observed_tx_timestamp,
                rx: observation.rx_timestamp,
                cfo_ratio: cfo_filtered,
                evidence_id,
            },
            topics & Topic::Distance.bit() != 0,
        ));
        messages
    }

    fn consume_cir(
        &mut self,
        observation: &CanonicalObservation,
        event_tick: i64,
        event_s: f64,
        cfo_filtered: f64,
        topics: u8,
    ) -> Vec<Vec<u8>> {
        let key = (observation.observed_node_id, observation.reporting_node_id);
        let raw = observation
            .cir_bytes
            .chunks_exact(4)
            .map(|tap| {
                Complex64::new(
                    i16::from_le_bytes([tap[0], tap[1]]) as f64,
                    i16::from_le_bytes([tap[2], tap[3]]) as f64,
                )
            })
            .collect::<Vec<_>>();
        let correction_db = (3.0 - observation.dgc_decision as f64) * 2.65;
        let dgc_linear = 10.0_f64.powf(correction_db / 20.0);
        let (scaled, mut quality) = scale_cir(&raw, dgc_linear, observation.accum_count as u32);
        let reference_mode = reference_mode(&self.settings);
        let link = self.links.entry(key).or_insert_with(|| LinkState {
            reference: CirReference::new(reference_mode),
            cir: Vec::new(),
            observations: 0,
            first_event_s: None,
            latest_event_s: None,
            last_slow_fft_s: None,
            latest_cfo_ppm: None,
            cfo_history: Vec::new(),
            latest_fast_fft: None,
            latest_slow_fft: None,
        });
        let reference = link
            .reference
            .update(event_s, &scaled)
            .map(|reference| reference.to_vec());
        let delay = reference.as_ref().and_then(|reference| {
            normalized_correlation_delay(reference, &scaled, self.settings.cir_max_lag)
        });
        let delay_samples = delay.map_or(0.0, |value| value.delay_samples);
        let correlation = delay.map_or(0.0, |value| value.correlation);
        if let Some(delay) = delay {
            quality.insert(delay.quality);
        } else {
            quality.insert(QualityFlags::LOW_CORRELATION);
        }
        let mut aligned = fractional_align_non_circular(&scaled, delay_samples);
        let phase = reference
            .as_ref()
            .and_then(|reference| common_phase(reference, &aligned))
            .unwrap_or(0.0);
        let rotation = Complex64::from_polar(1.0, -phase);
        for value in &mut aligned {
            *value *= rotation;
        }
        let marker_raw =
            observation.fp_index_q10_6 as f64 / 64.0 - observation.cir_start_offset as f64;
        let marker_aligned = marker_raw - delay_samples;
        let evidence_id = (observation.usb_sequence as u64) << 32 | observation.observed_k as u64;
        let want_cir = topics & Topic::Cir.bit() != 0;
        let want_waterfall = topics & Topic::Waterfall.bit() != 0;
        let want_fast = topics & Topic::FastFft.bit() != 0;
        let want_slow = topics & Topic::SlowFft.bit() != 0;
        let magnitude_16x = if want_waterfall || want_cir {
            resample_cir_16x(&aligned)
                .iter()
                .map(|value| value.norm() as f32)
                .collect::<Vec<_>>()
        } else {
            Vec::new()
        };
        let frame = CirFrame {
            event_tick,
            event_s,
            round: observation.observed_k,
            usb_sequence: observation.usb_sequence,
            aligned: aligned.clone(),
            magnitude_16x: magnitude_16x.clone(),
            marker_raw,
            marker_aligned,
            correlation,
            quality: quality.0,
        };
        if !link
            .cir
            .iter()
            .any(|item| item.round == frame.round && item.usb_sequence == frame.usb_sequence)
        {
            let at = link
                .cir
                .binary_search_by_key(&event_tick, |item| item.event_tick)
                .unwrap_or_else(|at| at);
            link.cir.insert(at, frame.clone());
        }
        let newest = link.cir.last().map_or(event_s, |item| item.event_s);
        link.cir
            .retain(|item| newest - item.event_s <= HISTORY_SECONDS);
        if link.cir.len() > MAX_CIR_FRAMES {
            let excess = link.cir.len() - MAX_CIR_FRAMES;
            link.cir.drain(..excess);
        }
        link.observations += 1;
        link.first_event_s = Some(
            link.first_event_s
                .map_or(event_s, |first| first.min(event_s)),
        );
        link.latest_event_s = Some(
            link.latest_event_s
                .map_or(event_s, |last| last.max(event_s)),
        );
        link.latest_cfo_ppm = Some(cfo_filtered * 1_000_000.0);
        let cfo_sample = (
            event_s,
            observation.cfo_raw as f64 / (1_u64 << 26) as f64 * 1_000_000.0,
            cfo_filtered * 1_000_000.0,
        );
        let cfo_at = link
            .cfo_history
            .binary_search_by(|sample| sample.0.total_cmp(&event_s))
            .unwrap_or_else(|at| at);
        link.cfo_history.insert(cfo_at, cfo_sample);
        link.cfo_history
            .retain(|sample| newest - sample.0 <= HISTORY_SECONDS);
        if link.cfo_history.len() > MAX_CIR_FRAMES {
            let excess = link.cfo_history.len() - MAX_CIR_FRAMES;
            link.cfo_history.drain(..excess);
        }

        if !(want_cir || want_waterfall || want_fast || want_slow) {
            return Vec::new();
        }
        let mut payloads = Vec::new();
        if want_cir {
            payloads.push((
                Topic::Cir,
                json!({
                    "from": key.0, "to": key.1, "round": observation.observed_k,
                    "event_s": event_s, "dgc_decision": observation.dgc_decision,
                    "dgc_correction_db": correction_db, "accum_count": observation.accum_count,
                    "delay_samples": delay_samples, "common_phase_rad": phase,
                    "correlation": correlation, "quality": quality.0,
                    "evidence": evidence_id,
                    "marker_raw": marker_raw, "marker_aligned": marker_aligned,
                    "magnitude": aligned.iter().map(|value| value.norm() as f32).collect::<Vec<_>>(),
                    "resampled": &frame.magnitude_16x,
                }),
            ));
        }
        if want_waterfall {
            let row = downsample_row(&frame.magnitude_16x, 256);
            let width = row.len();
            payloads.push((
                Topic::Waterfall,
                json!({
                    "from": key.0, "to": key.1, "round": observation.observed_k,
                    "event_s": event_s, "row": row, "width": width,
                    "marker": marker_aligned, "quality": quality.0, "evidence": evidence_id
                }),
            ));
        }
        if want_fast {
            let fast = fast_fft_complex(
                &aligned,
                self.settings.fast_time_sample_rate_hz,
                fft_window(&self.settings.fft_window),
                quality,
            );
            let n = fast.bins.len();
            let mut shifted_freq = vec![0.0_f32; n];
            let mut shifted_mag = vec![0.0_f32; n];
            let mut shifted_phase = vec![0.0_f32; n];
            let half = n / 2;
            let last_freq = fast.frequencies_hz.last().copied().unwrap_or(0.0) as f32;
            for i in 0..n {
                let src = (i + half) % n;
                shifted_freq[i] = if i < half {
                    fast.frequencies_hz[src] as f32 - last_freq
                } else {
                    fast.frequencies_hz[src] as f32
                };
                shifted_mag[i] = fast.bins[src].norm() as f32;
                shifted_phase[i] = fast.bins[src].arg() as f32;
            }
            let payload = json!({
                "from": key.0, "to": key.1, "round": observation.observed_k,
                "event_s": event_s, "frequencies_hz": shifted_freq,
                "magnitude": shifted_mag,
                "phase": shifted_phase,
                "quality": fast.quality.0, "evidence": evidence_id
            });
            link.latest_fast_fft = Some(payload.clone());
            payloads.push((Topic::FastFft, payload));
        }
        if want_slow
            && link
                .last_slow_fft_s
                .is_none_or(|last| event_s - last >= self.settings.slow_fft_cadence_s)
        {
            link.last_slow_fft_s = Some(event_s);
            if let Some(payload) = slow_fft_payload(key, link, &self.settings, self.config.as_ref())
            {
                link.latest_slow_fft = Some(payload.clone());
                payloads.push((Topic::SlowFft, payload));
            }
        }
        payloads
            .into_iter()
            .map(|(topic, payload)| self.message(topic, payload))
            .collect()
    }

    fn consume_timing(
        &mut self,
        observation: TimingObservation,
        emit_stream: bool,
    ) -> Vec<Vec<u8>> {
        let key = pair_key(observation.from, observation.to);
        let n = self
            .config
            .as_ref()
            .map_or(0, |config| config.n_nodes as i64);
        let pair = self.pairs.entry(key).or_default();
        if pair.timing.iter().any(|item| {
            (item.event_tick == observation.event_tick
                && item.from == observation.from
                && item.to == observation.to
                && item.tx == observation.tx
                && item.rx == observation.rx)
                || (item.evidence_id == observation.evidence_id && item.from == observation.from)
        }) {
            return Vec::new();
        }
        let at = pair
            .timing
            .binary_search_by(|item| {
                (item.event_tick, item.from, item.evidence_id).cmp(&(
                    observation.event_tick,
                    observation.from,
                    observation.evidence_id,
                ))
            })
            .unwrap_or_else(|at| at);
        pair.timing.insert(at, observation);
        let newest = pair.timing.last().map_or(0.0, |item| item.event_s);
        pair.timing
            .retain(|item| newest - item.event_s <= TIMING_HISTORY_SECONDS);
        if pair.timing.len() > MAX_TIMING_OBSERVATIONS {
            let excess = pair.timing.len() - MAX_TIMING_OBSERVATIONS;
            pair.timing.drain(..excess);
        }
        let retained = pair
            .timing
            .iter()
            .map(|item| item.evidence_id)
            .collect::<BTreeSet<_>>();
        pair.processed_ss
            .retain(|(a, b)| retained.contains(a) && retained.contains(b));
        pair.processed_ds.retain(|(a, b, c)| {
            retained.contains(a) && retained.contains(b) && retained.contains(c)
        });

        let mut estimates = Vec::new();
        for window in pair.timing.windows(2) {
            let (first, second) = (&window[0], &window[1]);
            let evidence = (first.evidence_id, second.evidence_id);
            let expected_gap = schedule_gap(first.from, second.from, n as u8);
            if expected_gap.is_some_and(|gap| second.event_tick - first.event_tick == gap)
                && first.from == second.to
                && first.to == second.from
                && !pair.processed_ss.contains(&evidence)
            {
                if let Ok(estimate) = ss_twr(
                    SsTwrInput {
                        poll_tx: first.tx,
                        response_rx: second.rx,
                        poll_rx: first.rx,
                        response_tx: second.tx,
                        remote_clock_offset: Some(second.cfo_ratio),
                    },
                    vec![first.evidence_id, second.evidence_id],
                ) {
                    pair.processed_ss.insert(evidence);
                    estimates.push((
                        second.event_s,
                        second.round,
                        "ss",
                        estimate.distance_m,
                        estimate.evidence.quality.0,
                        estimate.evidence.ids,
                        first.from,
                        first.to,
                        None,
                    ));
                }
            }
        }
        for window in pair.timing.windows(3) {
            let (first, second, third) = (&window[0], &window[1], &window[2]);
            let evidence = (first.evidence_id, second.evidence_id, third.evidence_id);
            let span = third.event_s - first.event_s;
            let first_gap = second.event_tick - first.event_tick;
            let second_gap = third.event_tick - second.event_tick;
            let total_ticks = third.event_tick - first.event_tick;
            let expected_first = schedule_gap(first.from, second.from, n as u8);
            let expected_second = schedule_gap(second.from, third.from, n as u8);
            if first.from == third.from
                && first.to == third.to
                && first.from == second.to
                && first.to == second.from
                && first_gap > 0
                && second_gap > 0
                && total_ticks <= 2 * n
                && (0.0..=0.100).contains(&span)
                && !pair.processed_ds.contains(&evidence)
            {
                if let Ok(estimate) = asymmetric_ds_twr(
                    DsTwrInput {
                        poll_tx: first.tx,
                        poll_rx: first.rx,
                        response_tx: second.tx,
                        response_rx: second.rx,
                        final_tx: third.tx,
                        final_rx: third.rx,
                    },
                    vec![first.evidence_id, second.evidence_id, third.evidence_id],
                ) {
                    let bridged = total_ticks != n
                        || expected_first != Some(first_gap)
                        || expected_second != Some(second_gap);
                    let quality = estimate.evidence.quality.0
                        | if bridged { QUALITY_BRIDGED_SPAN } else { 0 };
                    pair.processed_ds.insert(evidence);
                    estimates.push((
                        (first.event_s + third.event_s) * 0.5,
                        third.round,
                        "ds",
                        estimate.distance_m,
                        quality,
                        estimate.evidence.ids,
                        first.from,
                        first.to,
                        bridged.then_some(span),
                    ));
                }
            }
        }

        let mut messages = Vec::new();
        for (event_s, round, kind, raw_m, quality, evidence, from, to, bridge_duration_s) in
            estimates
        {
            self.insert_distance(
                key,
                event_s,
                round,
                kind,
                from,
                to,
                raw_m,
                quality,
                evidence.clone(),
                bridge_duration_s,
            );
            if let Some(pair) = self.pairs.get_mut(&key) {
                try_store_distance_history(pair, event_s, kind, from, to);
            }
            if !emit_stream {
                continue;
            }
            if let Some(sample) = self.pairs.get(&key).and_then(|pair| {
                pair.distances
                    .iter()
                    .find(|sample| sample.evidence == evidence)
                    .cloned()
            }) {
                let sample_evidence = sample.evidence.clone();
                let raw_distance = sample.raw_m;
                let smoothed_distance = sample.moving_average_m;
                let mut payload = json!({
                    "from": from, "to": to, "a": key.0, "b": key.1,
                    "sample": sample, "evidence": sample_evidence
                });
                if kind == "ss" {
                    payload["raw_ss"] = json!(raw_distance);
                    payload["smoothed_ss"] = json!(smoothed_distance);
                    if emit_stream {
                        messages.push(self.message(Topic::Distance, payload));
                    }
                } else {
                    payload["raw_ds"] = json!(raw_distance);
                    payload["smoothed_ds"] = json!(smoothed_distance);
                    if emit_stream {
                        messages.push(self.message(Topic::Distance, payload.clone()));
                    }
                    payload["from"] = json!(to);
                    payload["to"] = json!(from);
                    if emit_stream {
                        messages.push(self.message(Topic::Distance, payload));
                    }
                }
            }
        }
        messages
    }

    fn insert_distance(
        &mut self,
        key: (u8, u8),
        event_s: f64,
        round: u32,
        kind: &'static str,
        from: u8,
        to: u8,
        raw_m: f64,
        quality: u32,
        evidence: Vec<u64>,
        bridge_duration_s: Option<f64>,
    ) {
        let offset = self.host_offsets.get(&key.0).copied().unwrap_or(0.0)
            + self.host_offsets.get(&key.1).copied().unwrap_or(0.0);
        let pair = self.pairs.entry(key).or_default();
        if !pair.distance_evidence.insert(evidence.clone()) {
            return;
        }
        let sample = DistanceSample {
            event_s,
            round,
            kind,
            from,
            to,
            raw_m,
            raw_mm: metres_to_mm(raw_m),
            calibrated_m: raw_m - offset,
            calibrated_mm: metres_to_mm(raw_m - offset),
            hampel_m: raw_m - offset,
            moving_average_m: raw_m - offset,
            moving_average_mm: metres_to_mm(raw_m - offset),
            outlier: false,
            bridged: quality & QUALITY_BRIDGED_SPAN != 0,
            bridge_duration_s,
            quality,
            evidence,
        };
        let at = pair
            .distances
            .partition_point(|item| item.event_s <= event_s);
        let appended = at == pair.distances.len();
        pair.distances.insert(at, sample);
        let newest = pair.distances.back().map_or(event_s, |item| item.event_s);
        while pair
            .distances
            .front()
            .is_some_and(|item| newest - item.event_s > HISTORY_SECONDS)
        {
            if let Some(expired) = pair.distances.pop_front() {
                pair.distance_evidence.remove(&expired.evidence);
            }
        }
        while pair.distances.len() > MAX_DISTANCE_SAMPLES {
            if let Some(expired) = pair.distances.pop_front() {
                pair.distance_evidence.remove(&expired.evidence);
            }
        }
        if appended {
            update_latest_distance_filter(pair, &self.settings);
        } else {
            recompute_distance_series(pair, &self.settings, kind, from, to);
        }
        if kind == "ds" {
            if let Some(collection) = &mut self.calibration
                && collection.started.elapsed().as_secs_f64() <= collection.duration_s
            {
                let samples = collection.samples.entry(key).or_default();
                if samples.len() < 4096 {
                    samples.push(raw_m);
                }
            }
        }
    }

    fn message(&mut self, topic: Topic, payload: Value) -> Vec<u8> {
        self.stream_sequence += 1;
        let payload = serde_json::to_vec(&payload).unwrap_or_default();
        envelope(
            topic,
            self.stream_sequence,
            self.configuration_epoch,
            self.processing_epoch,
            0,
            &payload,
        )
    }

    pub fn settings(&self) -> DspSettings {
        self.settings.clone()
    }

    pub fn update_settings(&mut self, value: &Value) -> Result<DspSettings> {
        let mut merged = serde_json::to_value(&self.settings)?;
        let Some(update) = value.as_object() else {
            bail!("settings must be a JSON object");
        };
        let target = merged
            .as_object_mut()
            .expect("settings serialize as object");
        for (key, value) in update {
            if !target.contains_key(key) {
                bail!("unknown DSP setting {key}");
            }
            target.insert(key.clone(), value.clone());
        }
        let next: DspSettings = serde_json::from_value(merged)?;
        validate_settings(&next)?;
        self.settings = next;
        self.processing_epoch += 1;
        self.cfo = DirectionalCfoIntegrator::new(self.settings.cfo_half_life_s);
        for link in self.links.values_mut() {
            link.reference = CirReference::new(reference_mode(&self.settings));
            link.last_slow_fft_s = None;
        }
        for pair in self.pairs.values_mut() {
            recompute_distance_filters(pair, &self.settings);
        }
        Ok(self.settings.clone())
    }

    pub fn start_calibration(&mut self, request: &Value) -> Result<Value> {
        let references_m = if request.get("references_m").is_some() {
            calibration_references(request)?
        } else {
            BTreeMap::new()
        };
        self.calibration = Some(CalibrationCollection {
            started: Instant::now(),
            duration_s: 10.0,
            samples: BTreeMap::new(),
            references_m,
        });
        Ok(self.calibration_snapshot())
    }

    pub fn set_calibration_references(&mut self, request: &Value) -> Result<Value> {
        let references = calibration_references(request)?;
        let collection = self
            .calibration
            .as_mut()
            .ok_or_else(|| anyhow::anyhow!("calibration is idle"))?;
        if collection.started.elapsed().as_secs_f64() < collection.duration_s {
            bail!("calibration collection is not complete");
        }
        collection.references_m.extend(references);
        Ok(self.calibration_snapshot())
    }

    pub fn calibration_snapshot(&self) -> Value {
        let Some(collection) = &self.calibration else {
            return json!({"status": "idle", "duration_s": 10.0});
        };
        let elapsed = collection.started.elapsed().as_secs_f64();
        json!({
            "status": if elapsed >= collection.duration_s { "complete" } else { "collecting" },
            "elapsed_s": elapsed, "remaining_s": (collection.duration_s - elapsed).max(0.0),
            "pairs": calibration_pairs(collection)
        })
    }

    pub fn solve_calibration(&self) -> Result<CalibrationSolution> {
        let collection = self
            .calibration
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("calibration is idle"))?;
        if collection.started.elapsed().as_secs_f64() < collection.duration_s {
            bail!("calibration collection is not complete");
        }
        let pairs = calibration_pairs(collection);
        let observations = pairs
            .iter()
            .filter_map(|pair| {
                Some(PairCalibrationObservation {
                    board_a: pair.a as u32,
                    board_b: pair.b as u32,
                    measured_bias: pair.measured_bias_m?,
                    variance: pair.variance_m2.max(1e-9),
                })
            })
            .collect::<Vec<_>>();
        let regularization = 1e-6;
        let result = calibrate_offsets(&observations, regularization)
            .map_err(|error| anyhow::anyhow!("calibration solve failed: {error:?}"))?;
        let elapsed = collection.started.elapsed().as_secs_f64();
        let expected_columns = self
            .config
            .as_ref()
            .map_or(result.diagnostics.columns, |config| config.n_nodes as usize);
        let roster_complete = (0..expected_columns as u32).all(|board| {
            result
                .board_offsets
                .iter()
                .any(|(candidate, _)| *candidate == board)
        });
        let residual_rmse_m = if result.residuals.is_empty() {
            0.0
        } else {
            (result
                .residuals
                .iter()
                .map(|value| value * value)
                .sum::<f64>()
                / result.residuals.len() as f64)
                .sqrt()
        };
        let recommended_next_pair = if roster_complete {
            result.recommended_next_pair
        } else {
            let missing = (0..expected_columns as u32).find(|board| {
                !result
                    .board_offsets
                    .iter()
                    .any(|(candidate, _)| candidate == board)
            });
            missing.map(|board| (board, if board == 0 { 1 } else { 0 }))
        };
        Ok(CalibrationSolution {
            elapsed_s: elapsed,
            complete: elapsed >= collection.duration_s,
            pairs,
            board_offsets: result.board_offsets,
            residuals: result.residuals,
            rank: result.diagnostics.rank,
            columns: expected_columns,
            condition_number: result.diagnostics.condition_number,
            has_full_rank: result.diagnostics.has_full_rank && roster_complete,
            residual_rmse_m,
            poor_fit: residual_rmse_m > 0.05,
            regularization,
            recommended_next_pair,
        })
    }

    pub fn apply_calibration(&mut self, solution: &CalibrationSolution) {
        self.offset_history.push(self.host_offsets.clone());
        self.host_offsets = solution
            .board_offsets
            .iter()
            .map(|(board, offset)| (*board as u8, *offset))
            .collect();
        self.processing_epoch += 1;
        for (&key, pair) in &mut self.pairs {
            let offset = self.host_offsets.get(&key.0).copied().unwrap_or(0.0)
                + self.host_offsets.get(&key.1).copied().unwrap_or(0.0);
            for sample in &mut pair.distances {
                sample.calibrated_m = sample.raw_m - offset;
                sample.calibrated_mm = metres_to_mm(sample.calibrated_m);
            }
            recompute_distance_filters(pair, &self.settings);
            recalibrate_distance_history(pair, offset);
        }
    }

    pub fn rollback_calibration(&mut self) -> bool {
        let Some(previous) = self.offset_history.pop() else {
            return false;
        };
        self.host_offsets = previous;
        self.processing_epoch += 1;
        for (&key, pair) in &mut self.pairs {
            let offset = self.host_offsets.get(&key.0).copied().unwrap_or(0.0)
                + self.host_offsets.get(&key.1).copied().unwrap_or(0.0);
            for sample in &mut pair.distances {
                sample.calibrated_m = sample.raw_m - offset;
                sample.calibrated_mm = metres_to_mm(sample.calibrated_m);
            }
            recompute_distance_filters(pair, &self.settings);
            recalibrate_distance_history(pair, offset);
        }
        true
    }

    pub fn restore_calibration_stack(&mut self, stack: Vec<Vec<(u32, f64)>>) {
        self.offset_history = if stack.is_empty() {
            Vec::new()
        } else {
            std::iter::once(BTreeMap::new())
                .chain(stack.iter().take(stack.len() - 1).map(|offsets| {
                    offsets
                        .iter()
                        .map(|(board, offset)| (*board as u8, *offset))
                        .collect()
                }))
                .collect()
        };
        self.host_offsets = stack
            .last()
            .into_iter()
            .flatten()
            .map(|(board, offset)| (*board as u8, *offset))
            .collect();
        if !stack.is_empty() {
            self.processing_epoch += 1;
        }
    }

    pub fn summary(&self) -> PipelineSummary {
        let nodes = self
            .config
            .as_ref()
            .map_or(0, |config| config.n_nodes as usize);
        PipelineSummary {
            records: self.records,
            observations: self.observations,
            prehello_skipped: self.prehello_skipped,
            rejected: self.rejected,
            links_with_samples: self.links.len(),
            expected_links: nodes.saturating_mul(nodes.saturating_sub(1)),
            configuration_epoch: self.configuration_epoch,
            processing_epoch: self.processing_epoch,
            config: self.config.clone(),
            parser: parser_health(self.parser.stats(), self.parser.buffered_len()),
        }
    }

    pub fn topology(&self) -> Value {
        let Some(config) = &self.config else {
            return json!({"config": null, "links": [], "parser": parser_health(self.parser.stats(), self.parser.buffered_len())});
        };
        let mut links = Vec::new();
        for from in 0..config.n_nodes {
            for to in 0..config.n_nodes {
                if from == to {
                    continue;
                }
                let state = self.links.get(&(from, to));
                let span = state
                    .and_then(|state| Some(state.latest_event_s? - state.first_event_s?))
                    .unwrap_or(0.0);
                let pair = self.pairs.get(&pair_key(from, to));
                links.push(json!({
                    "from": from, "to": to, "id": format!("{from}>{to}"),
                    "observations": state.map_or(0, |state| state.observations),
                    "rate_hz": if span > 0.0 { state.map_or(0.0, |state| (state.observations.saturating_sub(1)) as f64 / span) } else { 0.0 },
                    "latest_event_s": state.and_then(|state| state.latest_event_s),
                    "cfo_ppm": state.and_then(|state| state.latest_cfo_ppm),
                    "cfo_history": state.map_or(0, |state| state.cfo_history.len()),
                    "cir_history": state.map_or(0, |state| state.cir.len()),
                    "latest_cir": state.and_then(|state| state.cir.last()).map(|frame| json!({
                        "round": frame.round, "event_s": frame.event_s, "correlation": frame.correlation,
                        "marker_raw": frame.marker_raw, "marker_aligned": frame.marker_aligned, "quality": frame.quality
                    })),
                    "latest_fast_fft": state.and_then(|state| state.latest_fast_fft.as_ref()),
                    "latest_slow_fft": state.and_then(|state| state.latest_slow_fft.as_ref()),
                    "distance": pair.and_then(|pair| pair.distances.back())
                }));
            }
        }
        json!({
            "config": config, "configuration_epoch": self.configuration_epoch,
            "processing_epoch": self.processing_epoch, "settings": self.settings,
            "host_offsets": self.host_offsets, "links": links,
            "parser": parser_health(self.parser.stats(), self.parser.buffered_len())
        })
    }

    pub fn distance_history(&self) -> Value {
        let Some(config) = &self.config else {
            return json!({"duration_s": DISTANCE_HISTORY_SECONDS, "links": []});
        };
        let mut links = Vec::new();
        for from in 0..config.n_nodes {
            for to in 0..config.n_nodes {
                if from == to {
                    continue;
                }
                let samples = self
                    .pairs
                    .get(&pair_key(from, to))
                    .map(|pair| {
                        pair.distance_history
                            .iter()
                            .filter(|sample| sample.from == from && sample.to == to)
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default();
                let stride = samples.len().div_ceil(256).max(1);
                let selected = samples.into_iter().step_by(stride).collect::<Vec<_>>();
                let series = |kind: &str, field: fn(&DistanceSample) -> f64| {
                    selected
                        .iter()
                        .filter(|sample| sample.kind == kind)
                        .map(|sample| field(sample))
                        .collect::<Vec<_>>()
                };
                links.push(json!({
                    "from": from, "to": to,
                    "raw_ss": series("ss", |sample| sample.raw_m),
                    "smoothed_ss": series("ss", |sample| sample.moving_average_m),
                    "raw_ds": series("ds", |sample| sample.raw_m),
                    "smoothed_ds": series("ds", |sample| sample.moving_average_m)
                }));
            }
        }
        json!({"duration_s": DISTANCE_HISTORY_SECONDS, "links": links})
    }
}

fn pair_key(a: u8, b: u8) -> (u8, u8) {
    if a < b { (a, b) } else { (b, a) }
}

fn try_store_distance_history(
    pair: &mut PairState,
    event_s: f64,
    kind: &'static str,
    from: u8,
    to: u8,
) {
    let key = (kind, from, to);
    if pair
        .distance_history_last_event
        .get(&key)
        .is_some_and(|last| event_s - last < 1.0)
    {
        return;
    }
    pair.distance_history_last_event.insert(key, event_s);
    if let Some(sample) = pair
        .distances
        .back()
        .filter(|s| s.kind == kind && s.from == from && s.to == to)
    {
        pair.distance_history.push_back(sample.clone());
        let newest = pair
            .distance_history
            .back()
            .map_or(0.0, |item| item.event_s);
        while pair
            .distance_history
            .front()
            .is_some_and(|item| newest - item.event_s > DISTANCE_HISTORY_SECONDS)
            || pair.distance_history.len() > MAX_DISTANCE_HISTORY_SAMPLES
        {
            pair.distance_history.pop_front();
        }
    }
}

fn recalibrate_distance_history(pair: &mut PairState, offset: f64) {
    for sample in &mut pair.distance_history {
        let calibrated = sample.raw_m - offset;
        let delta = calibrated - sample.calibrated_m;
        sample.calibrated_m = calibrated;
        sample.calibrated_mm = metres_to_mm(calibrated);
        sample.hampel_m += delta;
        sample.moving_average_m += delta;
        sample.moving_average_mm = metres_to_mm(sample.moving_average_m);
    }
}

fn metres_to_mm(value: f64) -> i64 {
    (value * 1_000.0).round() as i64
}

fn schedule_gap(from: u8, to: u8, n: u8) -> Option<i64> {
    if n < 2 || from >= n || to >= n {
        return None;
    }
    let gap = (to as u16 + n as u16 - from as u16) % n as u16;
    (gap != 0).then_some(gap as i64)
}

fn downsample_row(values: &[f32], target_width: usize) -> Vec<f32> {
    if values.len() <= target_width || target_width == 0 {
        return values.to_vec();
    }
    let stride = values.len().div_ceil(target_width);
    values
        .chunks(stride)
        .map(|chunk| chunk.iter().copied().fold(0.0_f32, f32::max))
        .collect()
}

fn parse_pair(value: &str) -> Option<(u8, u8)> {
    let (a, b) = value.split_once('>').or_else(|| value.split_once('-'))?;
    let (a, b) = (a.parse().ok()?, b.parse().ok()?);
    (a != b).then_some((a, b))
}

fn recompute_distance_filters(pair: &mut PairState, settings: &DspSettings) {
    let series = pair
        .distances
        .iter()
        .map(distance_series_key)
        .collect::<BTreeSet<_>>();
    for (kind, from, to) in series {
        recompute_distance_series(pair, settings, kind, from, to);
    }
}

fn recompute_distance_series(
    pair: &mut PairState,
    settings: &DspSettings,
    kind: &'static str,
    from: u8,
    to: u8,
) {
    let key = if kind == "ds" {
        (kind, 0, 0)
    } else {
        (kind, from, to)
    };
    let indices = pair
        .distances
        .iter()
        .enumerate()
        .filter(|(_, sample)| distance_series_key(sample) == key)
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    let values = indices
        .iter()
        .map(|index| pair.distances[*index].calibrated_m)
        .collect::<Vec<_>>();
    let mut average = TimeMovingAverage::new(settings.distance_smoothing_s);
    for (position, index) in indices.into_iter().enumerate() {
        let filtered = hampel(
            &values[..=position],
            position,
            settings.hampel_radius,
            settings.hampel_threshold_sigma,
        );
        let sample = &mut pair.distances[index];
        sample.hampel_m = filtered
            .as_ref()
            .map_or(sample.calibrated_m, |value| value.value);
        sample.outlier = filtered.is_some_and(|value| value.is_outlier);
        sample.moving_average_m = average.push(sample.event_s, sample.hampel_m);
        sample.moving_average_mm = metres_to_mm(sample.moving_average_m);
    }
}

fn update_latest_distance_filter(pair: &mut PairState, settings: &DspSettings) {
    let Some(index) = pair.distances.len().checked_sub(1) else {
        return;
    };
    update_distance_filter_at(pair, index, settings);
}

fn update_distance_filter_at(pair: &mut PairState, index: usize, settings: &DspSettings) {
    let key = distance_series_key(&pair.distances[index]);
    let mut values = pair
        .distances
        .iter()
        .take(index + 1)
        .rev()
        .filter(|sample| distance_series_key(sample) == key)
        .take(settings.hampel_radius + 1)
        .map(|sample| sample.calibrated_m)
        .collect::<Vec<_>>();
    values.reverse();
    let filtered = hampel(
        &values,
        values.len() - 1,
        settings.hampel_radius,
        settings.hampel_threshold_sigma,
    );
    let event_s = pair.distances[index].event_s;
    let hampel_m = filtered
        .as_ref()
        .map_or(pair.distances[index].calibrated_m, |value| value.value);
    let outlier = filtered.is_some_and(|value| value.is_outlier);
    let mut sum = 0.0;
    let mut count = 0;
    for sample in pair.distances.iter().take(index + 1).rev() {
        if !same_distance_series(&pair.distances[index], sample) {
            continue;
        }
        if event_s - sample.event_s > settings.distance_smoothing_s {
            break;
        }
        sum += if count == 0 {
            hampel_m
        } else {
            sample.hampel_m
        };
        count += 1;
    }
    let sample = &mut pair.distances[index];
    sample.hampel_m = hampel_m;
    sample.outlier = outlier;
    sample.moving_average_m = sum / count as f64;
    sample.moving_average_mm = metres_to_mm(sample.moving_average_m);
}

fn same_distance_series(a: &DistanceSample, b: &DistanceSample) -> bool {
    distance_series_key(a) == distance_series_key(b)
}

fn distance_series_key(sample: &DistanceSample) -> (&'static str, u8, u8) {
    if sample.kind == "ds" {
        (sample.kind, 0, 0)
    } else {
        (sample.kind, sample.from, sample.to)
    }
}

fn reference_mode(settings: &DspSettings) -> ReferenceMode {
    match settings.reference_mode.as_str() {
        "first" => ReferenceMode::First,
        "adaptive" => ReferenceMode::Adaptive {
            minimum_energy: settings.reference_minimum_energy,
            half_life_s: settings.reference_half_life_s,
        },
        _ => ReferenceMode::Qualified {
            minimum_energy: settings.reference_minimum_energy,
        },
    }
}

fn fft_window(value: &str) -> FftWindow {
    match value {
        "rectangular" => FftWindow::Rectangular,
        "hamming" => FftWindow::Hamming,
        "blackman" => FftWindow::Blackman,
        _ => FftWindow::Hann,
    }
}

fn validate_settings(settings: &DspSettings) -> Result<()> {
    if !settings.cfo_half_life_s.is_finite()
        || !(0.1..=30.0).contains(&settings.cfo_half_life_s)
        || !settings.distance_smoothing_s.is_finite()
        || !(1.0..=30.0).contains(&settings.distance_smoothing_s)
        || !settings.hampel_threshold_sigma.is_finite()
        || settings.hampel_threshold_sigma <= 0.0
        || !settings.reference_minimum_energy.is_finite()
        || settings.reference_minimum_energy < 0.0
        || !settings.reference_half_life_s.is_finite()
        || !(0.1..=30.0).contains(&settings.reference_half_life_s)
        || !settings.slow_fft_cadence_s.is_finite()
        || settings.slow_fft_cadence_s <= 0.0
        || !settings.slow_fft_history_s.is_finite()
        || !(1.0..=30.0).contains(&settings.slow_fft_history_s)
        || !settings.fast_time_sample_rate_hz.is_finite()
        || settings.fast_time_sample_rate_hz <= 0.0
        || !["rectangular", "hann", "hamming", "blackman"].contains(&settings.fft_window.as_str())
        || !["first", "qualified", "adaptive"].contains(&settings.reference_mode.as_str())
    {
        bail!("invalid DSP settings");
    }
    Ok(())
}

fn slow_fft_payload(
    key: (u8, u8),
    link: &LinkState,
    settings: &DspSettings,
    config: Option<&ConfigInfo>,
) -> Option<Value> {
    let config = config?;
    let sample_rate_hz = 1.0 / (config.cycle_us as f64 / 1_000_000.0);
    let history_samples = (settings.slow_fft_history_s * sample_rate_hz)
        .round()
        .clamp(2.0, MAX_CIR_FRAMES as f64) as usize;
    let frames = link
        .cir
        .iter()
        .rev()
        .take(history_samples)
        .cloned()
        .collect::<Vec<_>>();
    if frames.len() < 2 {
        return None;
    }
    let mut frames = frames.into_iter().rev().collect::<Vec<_>>();
    frames.sort_by_key(|frame| frame.event_tick);
    let step = config.n_nodes as i64;
    let first = frames.first()?.event_tick;
    let last = frames.last()?.event_tick;
    let count = ((last - first) / step + 1).clamp(2, history_samples as i64) as usize;
    let start = last - (count as i64 - 1) * step;
    let taps = frames
        .iter()
        .map(|frame| frame.aligned.len())
        .min()?
        .min(SLOW_FFT_TAPS);
    let mut gap_mask = Vec::with_capacity(count);
    let slots = (0..count)
        .map(|index| {
            let tick = start + index as i64 * step;
            let frame = frames.iter().find(|frame| frame.event_tick == tick);
            gap_mask.push(frame.is_none());
            frame
        })
        .collect::<Vec<_>>();
    let mut products = Vec::new();
    let mut frequencies = Vec::new();
    let mut values = Vec::new();
    for tap in 0..taps {
        let samples = slots
            .iter()
            .map(|frame| frame.map(|frame| frame.aligned[tap].norm()))
            .collect::<Vec<_>>();
        let Some((samples, quality)) = interpolate_short_gaps(&samples, settings.slow_fft_max_gap)
        else {
            continue;
        };
        let spectrum = fast_fft(
            &samples,
            sample_rate_hz,
            fft_window(&settings.fft_window),
            quality,
        );
        if frequencies.is_empty() {
            frequencies = spectrum.frequencies_hz.clone();
        }
        let magnitude = spectrum
            .bins
            .iter()
            .map(|value| value.norm() as f32)
            .collect::<Vec<_>>();
        values.extend_from_slice(&magnitude);
        products.push(json!({
            "tap": tap,
            "magnitude": magnitude,
            "phase": spectrum.bins.iter().map(|value| value.arg() as f32).collect::<Vec<_>>(),
            "quality": spectrum.quality.0
        }));
    }
    let width = frequencies.len();
    let height = products.len();
    let filled_samples = gap_mask.iter().filter(|missing| **missing).count();
    let filled_percent = filled_samples as f64 * 100.0 / gap_mask.len() as f64;
    Some(json!({
        "from": key.0, "to": key.1, "event_s": frames.last()?.event_s,
        "sample_rate_hz": sample_rate_hz, "gap_mask": gap_mask,
        "frequencies_hz": frequencies, "taps": products,
        "values": values, "width": width, "height": height,
        "filled_samples": filled_samples, "filled_percent": filled_percent,
        "quality": if filled_percent > 10.0 { "degraded" } else { "ok" }
    }))
}

fn calibration_references(request: &Value) -> Result<BTreeMap<(u8, u8), f64>> {
    let mut references = BTreeMap::new();
    let values = request
        .get("references_m")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow::anyhow!("references_m must be a JSON object"))?;
    for (pair, value) in values {
        let Some((a, b)) = parse_pair(pair) else {
            bail!("invalid calibration pair {pair}");
        };
        let reference = value
            .as_f64()
            .filter(|value| value.is_finite() && *value > 0.0)
            .ok_or_else(|| anyhow::anyhow!("invalid reference distance for {pair}"))?;
        references.insert(pair_key(a, b), reference);
    }
    Ok(references)
}

fn calibration_pairs(collection: &CalibrationCollection) -> Vec<CalibrationPair> {
    collection
        .samples
        .iter()
        .filter(|(_, samples)| !samples.is_empty())
        .map(|(&(a, b), samples)| {
            let mean = samples.iter().sum::<f64>() / samples.len() as f64;
            let variance = if samples.len() > 1 {
                samples
                    .iter()
                    .map(|value| (value - mean).powi(2))
                    .sum::<f64>()
                    / (samples.len() - 1) as f64
            } else {
                1e-6
            };
            CalibrationPair {
                a,
                b,
                samples: samples.len(),
                measured_mean_m: mean,
                reference_m: collection.references_m.get(&(a, b)).copied(),
                measured_bias_m: collection
                    .references_m
                    .get(&(a, b))
                    .map(|reference| mean - reference),
                variance_m2: variance,
            }
        })
        .collect()
}

fn parser_health(stats: &ParserStats, buffered_bytes: usize) -> ParserHealth {
    ParserHealth {
        buffered_bytes,
        crc_failures: stats.crc_failures,
        framing_errors: stats.framing_errors,
        unsupported_versions: stats.unsupported_versions,
        unknown_types: stats.unknown_types,
        sequence_gaps: stats.sequence_gaps,
        duplicates_or_old: stats.duplicates_or_old,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use heimdall_protocol::{ObservationProvenance, ObservationRoute, RecordKind, encode_record};

    fn hello_record() -> HelloRecord {
        HelloRecord {
            heimdall_version: 1,
            usb_version: 1,
            n_nodes: 2,
            m_slots: 1,
            node_id: 0,
            master_node_id: 0,
            cir_taps: 8,
            cir_left_taps: 2,
            config_hash: 7,
            subreport_bytes: 72,
            frame_payload_bytes: 72,
            max_frame_bytes: 127,
            slot_duration_us: 10_000,
            cycle_us: 20_000,
            device_id: 42,
            firmware_id: 3,
        }
    }

    fn hello_for_nodes(n_nodes: u8) -> HelloRecord {
        HelloRecord {
            n_nodes,
            slot_duration_us: 5_000,
            cycle_us: n_nodes as u32 * 5_000,
            ..hello_record()
        }
    }

    fn hello_bytes() -> Vec<u8> {
        let hello = hello_record();
        let mut p = vec![1, 1, 2, 1, 0, 0, 8, 2];
        p.extend_from_slice(&hello.config_hash.to_le_bytes());
        p.extend_from_slice(&hello.subreport_bytes.to_le_bytes());
        p.extend_from_slice(&hello.frame_payload_bytes.to_le_bytes());
        p.extend_from_slice(&hello.max_frame_bytes.to_le_bytes());
        p.extend_from_slice(&hello.slot_duration_us.to_le_bytes());
        p.extend_from_slice(&hello.cycle_us.to_le_bytes());
        p.extend_from_slice(&hello.device_id.to_le_bytes());
        p.extend_from_slice(&hello.firmware_id.to_le_bytes());
        encode_record(RecordKind::Hello, 0, 1, &p).unwrap()
    }

    fn observation(
        from: u8,
        to: u8,
        k: u32,
        tx: u64,
        rx: u64,
        sequence: u32,
    ) -> CanonicalObservation {
        let cir = (0..8_i16)
            .flat_map(|value| [value, 0])
            .flat_map(i16::to_le_bytes)
            .collect::<Vec<_>>();
        CanonicalObservation {
            route: ObservationRoute::Local,
            provenance: ObservationProvenance::CompleteReport,
            reporting_node_id: to,
            observed_node_id: from,
            observed_k: k,
            report_k: None,
            usb_sequence: sequence,
            obs_flags: 1,
            observed_m: 0,
            round_delta: 1,
            observed_tx_timestamp: tx,
            rx_timestamp: rx,
            cfo_raw: 0,
            fp_index_q10_6: 4 * 64,
            f1: 1,
            f2: 1,
            f3: 1,
            ip_power: 1,
            accum_count: 64,
            dgc_decision: 3,
            cir_start_offset: 0,
            cir_taps: 8,
            cir_bytes: cir.clone(),
            subreport_bytes: cir,
        }
    }

    fn payload(message: &[u8]) -> Value {
        let table = u32::from_le_bytes(message[0..4].try_into().unwrap()) as usize;
        let vtable =
            table - i32::from_le_bytes(message[table..table + 4].try_into().unwrap()) as usize;
        let field = u16::from_le_bytes(message[vtable + 16..vtable + 18].try_into().unwrap())
            as usize
            + table;
        let vector =
            field + u32::from_le_bytes(message[field..field + 4].try_into().unwrap()) as usize;
        let length = u32::from_le_bytes(message[vector..vector + 4].try_into().unwrap()) as usize;
        serde_json::from_slice(&message[vector + 4..vector + 4 + length]).unwrap()
    }

    #[test]
    fn chunk_boundaries_are_equivalent() {
        let data = hello_bytes();
        let mut whole = Pipeline::new();
        whole.feed(&data);
        let mut chunked = Pipeline::new();
        for chunk in data.chunks(3) {
            chunked.feed(chunk);
        }
        assert_eq!(whole.summary(), chunked.summary());
        assert_eq!(whole.summary().records, 1);
    }

    #[test]
    fn event_time_is_superslot_based_and_wrap_safe() {
        let mut pipeline = Pipeline::new();
        pipeline.configure(&hello_record());
        pipeline.consume_canonical(observation(1, 0, u32::MAX, 0, 100, 1));
        pipeline.consume_canonical(observation(0, 1, 0, 1100, 1200, 2));
        let times = pipeline.pairs[&(0, 1)]
            .timing
            .iter()
            .map(|item| item.event_s)
            .collect::<Vec<_>>();
        assert_eq!(times, [0.0, 0.01]);
    }

    #[test]
    fn synthetic_ss_and_ds_emit_all_live_topics() {
        let mut pipeline = Pipeline::new();
        pipeline.configure(&hello_record());
        pipeline
            .update_settings(&json!({"slow_fft_cadence_s": 0.001}))
            .unwrap();
        let mut messages = pipeline.consume_canonical(observation(0, 1, 10, 0, 100, 1));
        messages.extend(pipeline.consume_canonical(observation(
            0,
            1,
            12,
            600_000_000_200,
            600_000_000_300,
            3,
        )));
        // The relayed middle observation arrives late but is inserted by radio event time.
        messages.extend(pipeline.consume_canonical(observation(
            1,
            0,
            11,
            500_000_000_100,
            500_000_000_200,
            2,
        )));
        let pair = &pipeline.pairs[&(0, 1)];
        assert!(pair.distances.iter().any(|sample| sample.kind == "ss"));
        assert!(
            pair.distances
                .iter()
                .any(|sample| sample.kind == "ds" && !sample.bridged)
        );
        assert!(pair.distances.iter().all(|sample| sample.raw_m > 0.0));
        let topics = messages
            .iter()
            .filter_map(|message| crate::telemetry::envelope_topic(message))
            .collect::<BTreeSet<_>>();
        for topic in [
            Topic::Distance,
            Topic::Cfo,
            Topic::Cir,
            Topic::Waterfall,
            Topic::SlowFft,
            Topic::FastFft,
        ] {
            assert!(topics.contains(&topic), "missing {topic:?}");
        }

        let distance = messages
            .iter()
            .filter(|message| crate::telemetry::envelope_topic(message) == Some(Topic::Distance))
            .map(|message| payload(message))
            .collect::<Vec<_>>();
        assert!(distance.iter().any(|value| {
            value["raw_ss"].is_number()
                && value["smoothed_ss"].is_number()
                && value["from"] == 0
                && value["to"] == 1
                && value["sample"]["evidence"].is_array()
        }));
        let ds = distance
            .iter()
            .filter(|value| value["raw_ds"].is_number() && value["smoothed_ds"].is_number())
            .collect::<Vec<_>>();
        assert_eq!(ds.len(), 2);
        assert!(
            ds.iter()
                .any(|value| value["from"] == 0 && value["to"] == 1)
        );
        assert!(
            ds.iter()
                .any(|value| value["from"] == 1 && value["to"] == 0)
        );

        let cir = messages
            .iter()
            .find(|message| crate::telemetry::envelope_topic(message) == Some(Topic::Cir))
            .map(|message| payload(message))
            .unwrap();
        assert!(cir["magnitude"].is_array());
        assert!(cir.get("resampled").is_none());
        let waterfall = messages
            .iter()
            .find(|message| crate::telemetry::envelope_topic(message) == Some(Topic::Waterfall))
            .map(|message| payload(message))
            .unwrap();
        assert_eq!(
            waterfall["width"].as_u64().unwrap() as usize,
            waterfall["row"].as_array().unwrap().len()
        );
        assert!(waterfall["width"].as_u64().unwrap() <= 256);
        let slow = messages
            .iter()
            .find(|message| crate::telemetry::envelope_topic(message) == Some(Topic::SlowFft))
            .map(|message| payload(message))
            .unwrap();
        assert_eq!(
            slow["values"].as_array().unwrap().len(),
            slow["width"].as_u64().unwrap() as usize * slow["height"].as_u64().unwrap() as usize
        );
    }

    #[test]
    fn topic_demand_emits_only_requested_products() {
        let mut pipeline = Pipeline::new();
        pipeline.configure(&hello_record());
        let messages =
            pipeline.consume_canonical_inner(observation(0, 1, 10, 0, 100, 1), Topic::Cir.bit());
        assert!(!messages.is_empty());
        assert!(
            messages
                .iter()
                .all(|message| { crate::telemetry::envelope_topic(message) == Some(Topic::Cir) })
        );
    }

    #[test]
    fn n5_ss_uses_source_ownership_gap() {
        let mut pipeline = Pipeline::new();
        pipeline.configure(&hello_for_nodes(5));
        pipeline.consume_canonical(observation(1, 4, 101, 0, 100, 1));
        let messages = pipeline.consume_canonical(observation(4, 1, 104, 1100, 1200, 2));
        let ss = pipeline.pairs[&(1, 4)]
            .distances
            .iter()
            .filter(|sample| sample.kind == "ss")
            .collect::<Vec<_>>();
        assert_eq!(ss.len(), 1);
        let distance = messages
            .iter()
            .filter(|message| crate::telemetry::envelope_topic(message) == Some(Topic::Distance))
            .map(|message| payload(message))
            .collect::<Vec<_>>();
        assert_eq!(distance.len(), 1);
        let distance = &distance[0];
        assert_eq!(distance["from"], 1);
        assert_eq!(distance["to"], 4);
    }

    #[test]
    fn n5_ds_normal_and_bridge_flags_follow_cycle_span() {
        let mut normal = Pipeline::new();
        normal.configure(&hello_for_nodes(5));
        normal.consume_canonical(observation(1, 4, 11, 0, 100, 1));
        normal.consume_canonical(observation(4, 1, 14, 500_000_000_100, 500_000_000_200, 2));
        normal.consume_canonical(observation(1, 4, 16, 600_000_000_200, 600_000_000_300, 3));
        let ds = normal.pairs[&(1, 4)]
            .distances
            .iter()
            .find(|sample| sample.kind == "ds")
            .unwrap();
        assert!(!ds.bridged);

        let mut bridged = Pipeline::new();
        bridged.configure(&hello_for_nodes(5));
        bridged.consume_canonical(observation(1, 4, 11, 0, 100, 1));
        bridged.consume_canonical(observation(4, 1, 19, 500_000_000_100, 500_000_000_200, 2));
        bridged.consume_canonical(observation(1, 4, 21, 600_000_000_200, 600_000_000_300, 3));
        let ds = bridged.pairs[&(1, 4)]
            .distances
            .iter()
            .find(|sample| sample.kind == "ds")
            .unwrap();
        assert!(ds.bridged);
        assert_ne!(ds.quality & QUALITY_BRIDGED_SPAN, 0);
        assert!(ds.bridge_duration_s.is_some_and(|duration| duration > 0.0));
        assert_eq!(ds.raw_mm, metres_to_mm(ds.raw_m));
    }

    #[test]
    fn settings_change_processing_epoch() {
        let mut pipeline = Pipeline::new();
        assert_eq!(pipeline.settings().cfo_half_life_s, 2.0);
        let epoch = pipeline.processing_epoch;
        let settings = pipeline
            .update_settings(&json!({
                "cfo_half_life_s": 5.0, "fft_window": "blackman", "reference_mode": "adaptive"
            }))
            .unwrap();
        assert_eq!(settings.cfo_half_life_s, 5.0);
        assert_eq!(pipeline.processing_epoch, epoch + 1);
    }

    #[test]
    fn bridged_ds_is_bounded_and_flagged() {
        let mut pipeline = Pipeline::new();
        pipeline.configure(&hello_record());
        pipeline.consume_canonical(observation(0, 1, 0, 0, 100, 1));
        pipeline.consume_canonical(observation(1, 0, 3, 500_000_000_100, 500_000_000_200, 2));
        pipeline.consume_canonical(observation(0, 1, 4, 600_000_000_200, 600_000_000_300, 3));
        let ds = pipeline.pairs[&(0, 1)]
            .distances
            .iter()
            .find(|sample| sample.kind == "ds")
            .unwrap();
        assert!(ds.bridged);
        assert_ne!(ds.quality & QUALITY_BRIDGED_SPAN, 0);
    }

    #[test]
    fn calibration_references_can_be_entered_after_collection() {
        let mut pipeline = Pipeline::new();
        pipeline.start_calibration(&json!({})).unwrap();
        let collection = pipeline.calibration.as_mut().unwrap();
        collection.started = Instant::now() - std::time::Duration::from_secs(11);
        collection.samples.insert((0, 1), vec![1.20, 1.30]);
        pipeline
            .set_calibration_references(&json!({"references_m": {"0>1": 1.23}}))
            .unwrap();
        let solution = pipeline.solve_calibration().unwrap();
        assert_eq!(solution.pairs[0].reference_m, Some(1.23));
        assert!((solution.pairs[0].measured_bias_m.unwrap() - 0.02).abs() < 1e-12);
    }

    #[test]
    fn calibration_requires_the_complete_configured_roster() {
        let mut pipeline = Pipeline::new();
        pipeline.configure(&hello_for_nodes(5));
        pipeline.start_calibration(&json!({})).unwrap();
        let collection = pipeline.calibration.as_mut().unwrap();
        collection.started = Instant::now() - std::time::Duration::from_secs(11);
        collection.samples.insert((0, 1), vec![1.20, 1.30]);
        collection.references_m.insert((0, 1), 1.23);
        let solution = pipeline.solve_calibration().unwrap();
        assert_eq!(solution.columns, 5);
        assert!(!solution.has_full_rank);
        assert_eq!(solution.recommended_next_pair, Some((2, 0)));
    }

    #[test]
    fn long_distance_input_keeps_processing_window_bounded() {
        let mut pipeline = Pipeline::new();
        for index in 0..20_000_u64 {
            pipeline.insert_distance(
                (0, 1),
                index as f64 * 0.01,
                index as u32,
                "ss",
                0,
                1,
                1.0 + index as f64 * 1e-6,
                0,
                vec![index],
                None,
            );
        }
        let pair = &pipeline.pairs[&(0, 1)];
        assert_eq!(pair.distances.len(), MAX_DISTANCE_SAMPLES);
        assert!(pair.distances.back().unwrap().moving_average_m.is_finite());
    }

    #[test]
    fn reconnect_history_is_compacted_online() {
        let mut pair = PairState::default();
        for index in 0..1_000_u64 {
            let sample = DistanceSample {
                event_s: index as f64 * 0.01,
                round: index as u32,
                kind: "ss",
                from: 0,
                to: 1,
                raw_m: 1.0,
                raw_mm: 1_000,
                calibrated_m: 1.0,
                calibrated_mm: 1_000,
                hampel_m: 1.0,
                moving_average_m: 1.0,
                moving_average_mm: 1_000,
                outlier: false,
                bridged: false,
                bridge_duration_s: None,
                quality: 0,
                evidence: vec![index],
            };
            pair.distances.push_back(sample);
            try_store_distance_history(&mut pair, index as f64 * 0.01, "ss", 0, 1);
        }
        assert!(pair.distance_history.len() <= 10);
    }
}
