//! Heimdall USB CDC v1 and beacon v1 protocol implementation.
//!
//! Parsing and conversion are deterministic and do not depend on USB read
//! boundaries or wall-clock time. Raw records, radio frames, subreports, and CIR
//! bytes are retained so callers can archive and audit the exact wire evidence.

use bytes::{Buf, BytesMut};
use crc32fast::hash as crc32;
use std::collections::{BTreeMap, HashMap};
use thiserror::Error;

pub const SYNC: [u8; 2] = [0xc3, 0xa5];
pub const USB_VERSION: u8 = 1;
pub const BEACON_VERSION: u8 = 1;
pub const MAX_PAYLOAD: usize = 4096;
pub const BEACON_HEADER_BYTES: usize = 31;
pub const U40_MASK: u64 = (1_u64 << 40) - 1;

pub const RX_VALID: u8 = 1 << 0;
pub const RX_CONFIG_MISMATCH: u8 = 1 << 1;
pub const RX_UNKNOWN_VERSION: u8 = 1 << 2;
pub const RX_TRUNCATED: u8 = 1 << 3;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ProtocolError {
    #[error("{0}")]
    Invalid(&'static str),
    #[error("{0}")]
    InvalidOwned(String),
    #[error("payload is too large for USB CDC v1")]
    PayloadTooLarge,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum RecordKind {
    Hello = 0x01,
    Heartbeat = 0x02,
    RadioFrame = 0x03,
    LocalObservation = 0x04,
    CycleSummary = 0x05,
    Error = 0x06,
    TxRecord = 0x07,
}

impl TryFrom<u8> for RecordKind {
    type Error = ();

    fn try_from(value: u8) -> Result<Self, ()> {
        Ok(match value {
            0x01 => Self::Hello,
            0x02 => Self::Heartbeat,
            0x03 => Self::RadioFrame,
            0x04 => Self::LocalObservation,
            0x05 => Self::CycleSummary,
            0x06 => Self::Error,
            0x07 => Self::TxRecord,
            _ => return Err(()),
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Record {
    pub kind: RecordKind,
    pub flags: u8,
    pub sequence: u32,
    pub payload: Vec<u8>,
    pub raw: Vec<u8>,
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct ParserStats {
    pub crc_failures: u64,
    pub framing_errors: u64,
    pub unsupported_versions: u64,
    pub unknown_types: u64,
    pub sequence_gaps: u64,
    pub duplicates_or_old: u64,
}

/// Incremental USB parser with CRC-based resynchronisation.
#[derive(Debug)]
pub struct StreamParser {
    buffer: BytesMut,
    max_payload: usize,
    expected_sequence: Option<u32>,
    pending_crc_records: u32,
    stats: ParserStats,
}

impl Default for StreamParser {
    fn default() -> Self {
        Self::new(MAX_PAYLOAD)
    }
}

impl StreamParser {
    pub fn new(max_payload: usize) -> Self {
        Self {
            buffer: BytesMut::new(),
            max_payload,
            expected_sequence: None,
            pending_crc_records: 0,
            stats: ParserStats::default(),
        }
    }

    pub fn stats(&self) -> &ParserStats {
        &self.stats
    }

    pub fn buffered_len(&self) -> usize {
        self.buffer.len()
    }

    pub fn feed(&mut self, data: &[u8]) -> Vec<Record> {
        self.buffer.extend_from_slice(data);
        let mut records = Vec::new();
        loop {
            let Some(sync_at) = self.buffer.windows(2).position(|bytes| bytes == SYNC) else {
                let retain = usize::from(self.buffer.last() == Some(&SYNC[0]));
                let discard = self.buffer.len().saturating_sub(retain);
                self.buffer.advance(discard);
                break;
            };
            if sync_at != 0 {
                self.buffer.advance(sync_at);
            }
            if self.buffer.len() < 12 {
                break;
            }
            let payload_len = le_u16(&self.buffer[6..8]) as usize;
            if payload_len > self.max_payload {
                self.stats.framing_errors += 1;
                self.buffer.advance(1);
                continue;
            }
            let total_len = 16 + payload_len;
            if self.buffer.len() < total_len {
                break;
            }
            let expected_crc = le_u32(&self.buffer[12 + payload_len..16 + payload_len]);
            let actual_crc = crc32(&self.buffer[2..12 + payload_len]);
            if expected_crc != actual_crc {
                self.stats.crc_failures += 1;
                if self.buffer[2] == USB_VERSION {
                    self.pending_crc_records = self.pending_crc_records.saturating_add(1);
                }
                self.buffer.advance(1);
                continue;
            }

            let raw = self.buffer.split_to(total_len).to_vec();
            let sequence = le_u32(&raw[8..12]);
            let is_current = self.account_sequence(sequence);
            if raw[2] != USB_VERSION {
                self.stats.unsupported_versions += 1;
                continue;
            }
            if raw[5] != 0 || raw[4] & !0x03 != 0 {
                self.stats.framing_errors += 1;
                continue;
            }
            let Ok(kind) = RecordKind::try_from(raw[3]) else {
                self.stats.unknown_types += 1;
                continue;
            };
            // Old records remain present in the raw archive but must not be fused twice.
            if !is_current {
                continue;
            }
            records.push(Record {
                kind,
                flags: raw[4],
                sequence,
                payload: raw[12..12 + payload_len].to_vec(),
                raw,
            });
        }
        records
    }

    fn account_sequence(&mut self, sequence: u32) -> bool {
        let Some(expected) = self.expected_sequence else {
            self.expected_sequence = Some(sequence.wrapping_add(1));
            self.pending_crc_records = 0;
            return true;
        };
        let delta = sequence.wrapping_sub(expected);
        let current = if delta == 0 {
            self.expected_sequence = Some(sequence.wrapping_add(1));
            true
        } else if delta < 0x8000_0000 {
            self.stats.sequence_gaps += delta.saturating_sub(self.pending_crc_records) as u64;
            self.expected_sequence = Some(sequence.wrapping_add(1));
            true
        } else {
            self.stats.duplicates_or_old += 1;
            false
        };
        self.pending_crc_records = 0;
        current
    }
}

pub fn encode_record(
    kind: RecordKind,
    flags: u8,
    sequence: u32,
    payload: &[u8],
) -> Result<Vec<u8>, ProtocolError> {
    let length = u16::try_from(payload.len()).map_err(|_| ProtocolError::PayloadTooLarge)?;
    let mut raw = Vec::with_capacity(16 + payload.len());
    raw.extend_from_slice(&SYNC);
    raw.extend_from_slice(&[USB_VERSION, kind as u8, flags, 0]);
    raw.extend_from_slice(&length.to_le_bytes());
    raw.extend_from_slice(&sequence.to_le_bytes());
    raw.extend_from_slice(payload);
    raw.extend_from_slice(&crc32(&raw[2..]).to_le_bytes());
    Ok(raw)
}

/// Decode a little-endian five-byte integer.
pub fn read_u40(bytes: &[u8]) -> Result<u64, ProtocolError> {
    if bytes.len() != 5 {
        return Err(ProtocolError::Invalid("u40 requires five bytes"));
    }
    Ok(bytes
        .iter()
        .enumerate()
        .fold(0_u64, |value, (shift, byte)| {
            value | ((*byte as u64) << (shift * 8))
        }))
}

/// Forward modular distance from `earlier` to `later` in the 40-bit clock domain.
pub fn u40_forward_delta(later: u64, earlier: u64) -> u64 {
    later.wrapping_sub(earlier) & U40_MASK
}

/// Shortest signed modular distance from `earlier` to `later`.
pub fn u40_signed_delta(later: u64, earlier: u64) -> i64 {
    let delta = u40_forward_delta(later, earlier);
    if delta & (1 << 39) != 0 {
        delta as i64 - (1_i64 << 40)
    } else {
        delta as i64
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HelloRecord {
    pub heimdall_version: u8,
    pub usb_version: u8,
    pub n_nodes: u8,
    pub m_slots: u8,
    pub node_id: u8,
    pub master_node_id: u8,
    pub cir_taps: u8,
    pub cir_left_taps: u8,
    pub config_hash: u16,
    pub subreport_bytes: u16,
    pub frame_payload_bytes: u16,
    pub max_frame_bytes: u16,
    pub slot_duration_us: u32,
    pub cycle_us: u32,
    pub device_id: u64,
    pub firmware_id: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HeartbeatRecord {
    pub uptime_ms: u32,
    pub cycles_completed: u32,
    pub sync_state: u8,
    pub evidence_age: u8,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RadioFrameRecord {
    pub rx_timestamp: u64,
    pub rx_flags: u8,
    pub frame: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Subreport {
    pub observed_node_id: u8,
    pub obs_flags: u8,
    pub observed_m: u8,
    pub round_delta: u8,
    pub observed_tx_timestamp: u64,
    pub rx_timestamp: u64,
    pub cfo_raw: i16,
    pub fp_index_q10_6: u16,
    pub f1: u32,
    pub f2: u32,
    pub f3: u32,
    pub ip_power: u32,
    pub accum_count: u16,
    pub dgc_decision: u8,
    pub cir_start_offset: u16,
    pub cir_iq: Vec<(i16, i16)>,
    pub cir_bytes: Vec<u8>,
}

impl Subreport {
    pub fn cfo_ratio(&self) -> f64 {
        self.cfo_raw as f64 / ((1_u64 << 26) as f64)
    }

    pub fn cfo_ppm(&self) -> f64 {
        self.cfo_ratio() * 1_000_000.0
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LocalObservationRecord {
    pub reporting_node_id: u8,
    pub k: u32,
    pub subreport: Subreport,
    pub subreport_bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CycleSummaryRecord {
    pub k_cycle_start: u32,
    pub cycle_index: u32,
    pub frames_received: u16,
    pub frames_expected: u16,
    pub fcs_errors: u16,
    pub filter_rejects: u16,
    pub validation_rejects: u16,
    pub subreport_crc_failures: u16,
    pub usb_queue_drops: u16,
    pub rx_callback_max_us: u16,
    pub peer_m0_miss: [u8; 8],
    pub evidence_age: u8,
    pub flags: u8,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrorRecord {
    pub code: u16,
    pub detail: u16,
    pub k: u32,
    pub text: String,
    pub text_bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TxRecord {
    pub k: u32,
    pub m: u8,
    pub tx_timestamp: u64,
    pub frame_length: u16,
    pub confirmed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecodedRecord {
    Hello(HelloRecord),
    Heartbeat(HeartbeatRecord),
    RadioFrame(RadioFrameRecord),
    LocalObservation(LocalObservationRecord),
    CycleSummary(CycleSummaryRecord),
    Error(ErrorRecord),
    TxRecord(TxRecord),
}

#[derive(Debug, Default, Clone)]
pub struct DecoderState {
    pub hello: Option<HelloRecord>,
}

pub fn decode_subreport(data: &[u8]) -> Result<Subreport, ProtocolError> {
    if data.len() < 40 {
        return Err(ProtocolError::Invalid(
            "subreport is shorter than fixed metadata",
        ));
    }
    let taps = data[35] as usize;
    let expected_len = 40 + 4 * taps;
    if taps == 0 || taps > 128 || data.len() != expected_len {
        return Err(ProtocolError::Invalid(
            "subreport tap count does not match its length",
        ));
    }
    if data[1] & !0x0f != 0 {
        return Err(ProtocolError::Invalid(
            "subreport has reserved observation flags",
        ));
    }
    if crc32(&data[..expected_len - 4]) != le_u32(&data[expected_len - 4..]) {
        return Err(ProtocolError::Invalid("subreport CRC32 mismatch"));
    }
    let cir_bytes = data[36..36 + 4 * taps].to_vec();
    let cir_iq = cir_bytes
        .chunks_exact(4)
        .map(|tap| (le_i16(&tap[..2]), le_i16(&tap[2..])))
        .collect();
    Ok(Subreport {
        observed_node_id: data[0],
        obs_flags: data[1],
        observed_m: data[2],
        round_delta: data[3],
        observed_tx_timestamp: read_u40(&data[4..9])?,
        rx_timestamp: read_u40(&data[9..14])?,
        cfo_raw: le_i16(&data[14..16]),
        fp_index_q10_6: le_u16(&data[16..18]),
        f1: le_u24(&data[18..21]),
        f2: le_u24(&data[21..24]),
        f3: le_u24(&data[24..27]),
        ip_power: le_u24(&data[27..30]),
        accum_count: le_u16(&data[30..32]),
        dgc_decision: data[32],
        cir_start_offset: le_u16(&data[33..35]),
        cir_iq,
        cir_bytes,
    })
}

pub fn decode_record(
    record: &Record,
    state: &mut DecoderState,
) -> Result<DecodedRecord, ProtocolError> {
    let payload = &record.payload;
    match record.kind {
        RecordKind::Hello => {
            if payload.len() != 36 {
                return Err(ProtocolError::Invalid("HELLO payload must be 36 bytes"));
            }
            let hello = HelloRecord {
                heimdall_version: payload[0],
                usb_version: payload[1],
                n_nodes: payload[2],
                m_slots: payload[3],
                node_id: payload[4],
                master_node_id: payload[5],
                cir_taps: payload[6],
                cir_left_taps: payload[7],
                config_hash: le_u16(&payload[8..10]),
                subreport_bytes: le_u16(&payload[10..12]),
                frame_payload_bytes: le_u16(&payload[12..14]),
                max_frame_bytes: le_u16(&payload[14..16]),
                slot_duration_us: le_u32(&payload[16..20]),
                cycle_us: le_u32(&payload[20..24]),
                device_id: le_u64(&payload[24..32]),
                firmware_id: le_u32(&payload[32..36]),
            };
            validate_hello(&hello)?;
            state.hello = Some(hello.clone());
            Ok(DecodedRecord::Hello(hello))
        }
        RecordKind::Heartbeat => {
            if payload.len() != 12 || payload[10..12] != [0, 0] || payload[8] > 2 {
                return Err(ProtocolError::Invalid("invalid HEARTBEAT payload"));
            }
            Ok(DecodedRecord::Heartbeat(HeartbeatRecord {
                uptime_ms: le_u32(&payload[..4]),
                cycles_completed: le_u32(&payload[4..8]),
                sync_state: payload[8],
                evidence_age: payload[9],
            }))
        }
        RecordKind::RadioFrame => {
            let hello = require_hello(state)?;
            if payload.len() < 8 {
                return Err(ProtocolError::Invalid("RADIO_FRAME payload is too short"));
            }
            let rx_flags = payload[5];
            if rx_flags & !0x0f != 0 {
                return Err(ProtocolError::Invalid("RADIO_FRAME has reserved RX flags"));
            }
            let frame_len = le_u16(&payload[6..8]) as usize;
            if payload.len() != 8 + frame_len || frame_len > hello.max_frame_bytes as usize - 2 {
                return Err(ProtocolError::Invalid("RADIO_FRAME length mismatch"));
            }
            Ok(DecodedRecord::RadioFrame(RadioFrameRecord {
                rx_timestamp: read_u40(&payload[..5])?,
                rx_flags,
                frame: payload[8..].to_vec(),
            }))
        }
        RecordKind::LocalObservation => {
            let hello = require_hello(state)?;
            if payload.len() < 45 {
                return Err(ProtocolError::Invalid("LOCAL_OBS payload is too short"));
            }
            let reporting_node_id = payload[0];
            let k = le_u32(&payload[1..5]);
            let raw = payload[5..].to_vec();
            let subreport = decode_subreport(&raw)?;
            if reporting_node_id != hello.node_id {
                return Err(ProtocolError::Invalid(
                    "local reporting node does not match HELLO",
                ));
            }
            validate_observation(&subreport, reporting_node_id, hello)?;
            if k % hello.n_nodes as u32 != subreport.observed_node_id as u32 {
                return Err(ProtocolError::Invalid(
                    "local observation superslot ownership is invalid",
                ));
            }
            Ok(DecodedRecord::LocalObservation(LocalObservationRecord {
                reporting_node_id,
                k,
                subreport,
                subreport_bytes: raw,
            }))
        }
        RecordKind::TxRecord => {
            let hello = require_hello(state)?;
            if payload.len() != 13 || payload[12] & !1 != 0 {
                return Err(ProtocolError::Invalid("invalid TX_RECORD payload"));
            }
            let k = le_u32(&payload[..4]);
            let m = payload[4];
            let frame_length = le_u16(&payload[10..12]);
            let expected_frame = BEACON_HEADER_BYTES + hello.frame_payload_bytes as usize + 2;
            if m >= hello.m_slots
                || k % hello.n_nodes as u32 != hello.node_id as u32
                || frame_length as usize != expected_frame
                || frame_length > hello.max_frame_bytes
            {
                return Err(ProtocolError::Invalid(
                    "TX_RECORD dimensions or ownership are invalid",
                ));
            }
            Ok(DecodedRecord::TxRecord(TxRecord {
                k,
                m,
                tx_timestamp: read_u40(&payload[5..10])?,
                frame_length,
                confirmed: payload[12] == 1,
            }))
        }
        RecordKind::CycleSummary => {
            let hello = require_hello(state)?;
            if payload.len() != 34 || payload[33] & !1 != 0 {
                return Err(ProtocolError::Invalid("invalid CYCLE_SUMMARY payload"));
            }
            let expected_frames = (hello.n_nodes as u16 - 1) * hello.m_slots as u16;
            let frames_received = le_u16(&payload[8..10]);
            let frames_expected = le_u16(&payload[10..12]);
            let mut peer_m0_miss = [0; 8];
            peer_m0_miss.copy_from_slice(&payload[24..32]);
            if frames_expected != expected_frames
                || frames_received > frames_expected
                || peer_m0_miss[hello.n_nodes as usize..]
                    .iter()
                    .any(|value| *value != 0)
                || peer_m0_miss.iter().any(|value| *value > 1)
                || le_u32(&payload[..4]) % hello.n_nodes as u32 != 0
            {
                return Err(ProtocolError::Invalid(
                    "CYCLE_SUMMARY dimensions are invalid",
                ));
            }
            Ok(DecodedRecord::CycleSummary(CycleSummaryRecord {
                k_cycle_start: le_u32(&payload[..4]),
                cycle_index: le_u32(&payload[4..8]),
                frames_received,
                frames_expected,
                fcs_errors: le_u16(&payload[12..14]),
                filter_rejects: le_u16(&payload[14..16]),
                validation_rejects: le_u16(&payload[16..18]),
                subreport_crc_failures: le_u16(&payload[18..20]),
                usb_queue_drops: le_u16(&payload[20..22]),
                rx_callback_max_us: le_u16(&payload[22..24]),
                peer_m0_miss,
                evidence_age: payload[32],
                flags: payload[33],
            }))
        }
        RecordKind::Error => {
            if payload.len() < 8 {
                return Err(ProtocolError::Invalid("ERROR payload is too short"));
            }
            let text_bytes = payload[8..].to_vec();
            Ok(DecodedRecord::Error(ErrorRecord {
                code: le_u16(&payload[..2]),
                detail: le_u16(&payload[2..4]),
                k: le_u32(&payload[4..8]),
                text: String::from_utf8_lossy(&text_bytes).into_owned(),
                text_bytes,
            }))
        }
    }
}

fn validate_hello(hello: &HelloRecord) -> Result<(), ProtocolError> {
    let n = hello.n_nodes as u32;
    let m = hello.m_slots as u32;
    let taps = hello.cir_taps as u32;
    let expected_subreport = 40 + 4 * taps;
    let pooled_max = (n.saturating_sub(1)) * expected_subreport;
    let frame_bytes = BEACON_HEADER_BYTES as u32 + hello.frame_payload_bytes as u32 + 2;
    let frame_capacity =
        (hello.max_frame_bytes as u32).saturating_sub(BEACON_HEADER_BYTES as u32 + 2);
    let expected_m = pooled_max.div_ceil(frame_capacity.max(1));
    let expected_frame_payload = pooled_max.div_ceil(m.max(1));
    let expected_cycle = n
        .checked_mul(m)
        .and_then(|slots| slots.checked_mul(hello.slot_duration_us));
    if hello.heimdall_version != BEACON_VERSION || hello.usb_version != USB_VERSION {
        return Err(ProtocolError::Invalid("unsupported HELLO protocol version"));
    }
    if !(2..=8).contains(&hello.n_nodes)
        || hello.m_slots == 0
        || hello.node_id >= hello.n_nodes
        || hello.master_node_id >= hello.n_nodes
        || !(1..=128).contains(&hello.cir_taps)
        || hello.cir_left_taps >= hello.cir_taps
        || hello.subreport_bytes as u32 != expected_subreport
        || hello.frame_payload_bytes == 0
        || hello.max_frame_bytes > 1023
        || frame_bytes > hello.max_frame_bytes as u32
        || m != expected_m
        || hello.frame_payload_bytes as u32 != expected_frame_payload
        || m * (hello.frame_payload_bytes as u32) < pooled_max
        || hello.slot_duration_us == 0
        || hello.slot_duration_us % 100 != 0
        || expected_cycle != Some(hello.cycle_us)
    {
        return Err(ProtocolError::Invalid(
            "HELLO dimensions or invariants are invalid",
        ));
    }
    Ok(())
}

fn require_hello(state: &DecoderState) -> Result<&HelloRecord, ProtocolError> {
    state.hello.as_ref().ok_or(ProtocolError::Invalid(
        "HELLO is required before this record",
    ))
}

fn validate_observation(
    subreport: &Subreport,
    reporting_node_id: u8,
    hello: &HelloRecord,
) -> Result<(), ProtocolError> {
    let expected_delta = (reporting_node_id as u16 + hello.n_nodes as u16
        - subreport.observed_node_id as u16)
        % hello.n_nodes as u16;
    if subreport.observed_node_id >= hello.n_nodes
        || subreport.observed_node_id == reporting_node_id
        || subreport.observed_m != 0
        || subreport.round_delta as u16 != expected_delta
        || subreport.cir_iq.len() > hello.cir_taps as usize
        || 40 + 4 * subreport.cir_iq.len() > hello.subreport_bytes as usize
    {
        return Err(ProtocolError::Invalid(
            "subreport dimensions or schedule metadata are inconsistent",
        ));
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BeaconHeader {
    pub mac_sequence: u8,
    pub network_id: u16,
    pub source_node_id: u8,
    pub protocol_version: u8,
    pub frame_type: u8,
    pub m: u8,
    pub k: u32,
    pub n_nodes: u8,
    pub m_slots: u8,
    pub config_hash: u16,
    pub tx_timestamp: u64,
    pub subreport_count: u8,
    pub pooled_total_bytes: u16,
    pub peer_observed_bitmap: u8,
    pub evidence_age: u8,
    pub flags: u8,
}

pub fn decode_beacon_header(
    frame: &[u8],
    hello: &HelloRecord,
) -> Result<BeaconHeader, ProtocolError> {
    if frame.len() != BEACON_HEADER_BYTES + hello.frame_payload_bytes as usize {
        return Err(ProtocolError::Invalid(
            "beacon frame length does not match HELLO",
        ));
    }
    if le_u16(&frame[..2]) != 0x8841 {
        return Err(ProtocolError::Invalid("invalid beacon frame control"));
    }
    if le_u16(&frame[5..7]) != 0xffff {
        return Err(ProtocolError::Invalid(
            "beacon destination is not broadcast",
        ));
    }
    let source_u16 = le_u16(&frame[7..9]);
    if source_u16 > u8::MAX as u16 {
        return Err(ProtocolError::Invalid("beacon source node is invalid"));
    }
    let header = BeaconHeader {
        mac_sequence: frame[2],
        network_id: le_u16(&frame[3..5]),
        source_node_id: source_u16 as u8,
        protocol_version: frame[9],
        frame_type: frame[10],
        m: frame[11],
        k: le_u32(&frame[12..16]),
        n_nodes: frame[16],
        m_slots: frame[17],
        config_hash: le_u16(&frame[18..20]),
        tx_timestamp: read_u40(&frame[20..25])?,
        subreport_count: frame[25],
        pooled_total_bytes: le_u16(&frame[26..28]),
        peer_observed_bitmap: frame[28],
        evidence_age: frame[29],
        flags: frame[30],
    };
    let reports = header.peer_observed_bitmap.count_ones() as usize;
    if header.source_node_id >= hello.n_nodes
        || header.k % hello.n_nodes as u32 != header.source_node_id as u32
    {
        return Err(ProtocolError::Invalid(
            "beacon source does not own superslot",
        ));
    }
    if header.protocol_version != hello.heimdall_version || header.frame_type != 0 {
        return Err(ProtocolError::Invalid(
            "unsupported beacon protocol or frame type",
        ));
    }
    if header.n_nodes != hello.n_nodes || header.m_slots != hello.m_slots {
        return Err(ProtocolError::Invalid(
            "beacon dimensions do not match HELLO",
        ));
    }
    if header.config_hash != hello.config_hash {
        return Err(ProtocolError::Invalid(
            "beacon config hash does not match HELLO",
        ));
    }
    if header.m >= hello.m_slots || header.flags & !0x03 != 0 {
        return Err(ProtocolError::Invalid(
            "beacon fragment index or flags are invalid",
        ));
    }
    if header.subreport_count as usize > hello.n_nodes as usize - 1
        || header.pooled_total_bytes as usize
            > (hello.n_nodes as usize - 1) * hello.subreport_bytes as usize
        || header.pooled_total_bytes as usize > reports * hello.subreport_bytes as usize
        || (header.pooled_total_bytes == 0) != (reports == 0)
        || header.peer_observed_bitmap & (1 << header.source_node_id) != 0
        || (header.peer_observed_bitmap as u16) >> hello.n_nodes != 0
    {
        return Err(ProtocolError::Invalid(
            "beacon pooled-report metadata is invalid",
        ));
    }
    Ok(header)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ObservationProvenance {
    CompleteReport,
    RecoveredPartialReport,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RelayedSubreport {
    pub header: BeaconHeader,
    pub subreport: Subreport,
    pub subreport_bytes: Vec<u8>,
    pub provenance: ObservationProvenance,
    pub usb_sequence: u32,
}

#[derive(Debug, Clone)]
struct Fragment {
    header: BeaconHeader,
    payload: Vec<u8>,
    usb_sequence: u32,
}

#[derive(Debug, Clone)]
struct PendingReport {
    header: BeaconHeader,
    fragments: BTreeMap<u8, Fragment>,
}

#[derive(Debug, Default)]
pub struct ReportReassembler {
    pending: HashMap<u8, PendingReport>,
    pub duplicate_fragments: u64,
    pub inconsistent_fragments: u64,
    pub partial_reports_superseded: u64,
    pub partial_subreports_recovered: u64,
}

impl ReportReassembler {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn reset(&mut self) {
        self.pending.clear();
    }

    pub fn add(
        &mut self,
        radio: &RadioFrameRecord,
        hello: &HelloRecord,
        usb_sequence: u32,
    ) -> Result<Vec<RelayedSubreport>, ProtocolError> {
        if radio.rx_flags != RX_VALID {
            return Ok(Vec::new());
        }
        let header = decode_beacon_header(&radio.frame, hello)?;
        let payload = &radio.frame[BEACON_HEADER_BYTES..];
        let fragment_start = header.m as usize * hello.frame_payload_bytes as usize;
        let valid_len = (header.pooled_total_bytes as usize)
            .saturating_sub(fragment_start)
            .min(hello.frame_payload_bytes as usize);
        if payload[valid_len..].iter().any(|byte| *byte != 0) {
            return Err(ProtocolError::Invalid(
                "beacon frame has non-zero report padding",
            ));
        }

        let source = header.source_node_id;
        let mut recovered = Vec::new();
        if let Some(prior) = self.pending.get(&source) {
            if prior.header.k != header.k {
                if u32_is_newer(header.k, prior.header.k) {
                    let prior = self.pending.remove(&source).expect("pending entry exists");
                    recovered = recover_partial(&prior, hello);
                    self.partial_reports_superseded += 1;
                    self.partial_subreports_recovered += recovered.len() as u64;
                } else {
                    return Ok(Vec::new());
                }
            }
        }

        let pending = self.pending.entry(source).or_insert_with(|| PendingReport {
            header: header.clone(),
            fragments: BTreeMap::new(),
        });
        if !headers_consistent(&pending.header, &header) {
            self.inconsistent_fragments += 1;
            self.pending.remove(&source);
            return Err(ProtocolError::Invalid(
                "inconsistent fragments for one pooled report",
            ));
        }
        if let Some(prior) = pending.fragments.get(&header.m) {
            if prior.payload != payload || prior.header != header {
                self.inconsistent_fragments += 1;
                self.pending.remove(&source);
                return Err(ProtocolError::Invalid("duplicate fragment changed"));
            }
            self.duplicate_fragments += 1;
            return Ok(recovered);
        }
        pending.fragments.insert(
            header.m,
            Fragment {
                header: header.clone(),
                payload: payload.to_vec(),
                usb_sequence,
            },
        );
        if pending.fragments.len() != hello.m_slots as usize {
            return Ok(recovered);
        }

        let pending = self.pending.remove(&source).expect("pending entry exists");
        let mut pooled = Vec::with_capacity(hello.m_slots as usize * payload.len());
        for m in 0..hello.m_slots {
            pooled.extend_from_slice(&pending.fragments[&m].payload);
        }
        pooled.truncate(header.pooled_total_bytes as usize);
        let mut complete = decode_complete_report(&pending, &pooled, hello, usb_sequence)?;
        recovered.append(&mut complete);
        Ok(recovered)
    }
}

fn headers_consistent(first: &BeaconHeader, other: &BeaconHeader) -> bool {
    first.source_node_id == other.source_node_id
        && first.network_id == other.network_id
        && first.protocol_version == other.protocol_version
        && first.frame_type == other.frame_type
        && first.k == other.k
        && first.n_nodes == other.n_nodes
        && first.m_slots == other.m_slots
        && first.config_hash == other.config_hash
        && first.pooled_total_bytes == other.pooled_total_bytes
        && first.peer_observed_bitmap == other.peer_observed_bitmap
        && first.evidence_age == other.evidence_age
        && first.flags == other.flags
}

fn decode_complete_report(
    pending: &PendingReport,
    pooled: &[u8],
    hello: &HelloRecord,
    usb_sequence: u32,
) -> Result<Vec<RelayedSubreport>, ProtocolError> {
    let mut offset = 0;
    let mut decoded = Vec::new();
    let mut starts = vec![0_u8; hello.m_slots as usize];
    while offset < pooled.len() {
        if pooled.len() - offset < 40 {
            return Err(ProtocolError::Invalid(
                "pooled report ends inside subreport metadata",
            ));
        }
        let length = 40 + 4 * pooled[offset + 35] as usize;
        if offset + length > pooled.len() {
            return Err(ProtocolError::Invalid(
                "pooled report ends inside a subreport",
            ));
        }
        let raw = pooled[offset..offset + length].to_vec();
        let subreport = decode_subreport(&raw)?;
        validate_observation(&subreport, pending.header.source_node_id, hello)?;
        starts[offset / hello.frame_payload_bytes as usize] += 1;
        decoded.push(RelayedSubreport {
            header: pending.header.clone(),
            subreport,
            subreport_bytes: raw,
            provenance: ObservationProvenance::CompleteReport,
            usb_sequence,
        });
        offset += length;
    }
    validate_report_membership(&pending.header, &decoded, hello)?;
    if (0..hello.m_slots)
        .any(|m| pending.fragments[&m].header.subreport_count != starts[m as usize])
    {
        return Err(ProtocolError::Invalid(
            "subreport starts do not match per-frame counts",
        ));
    }
    Ok(decoded)
}

/// Recover only self-contained subreports. CRC and semantic checks prevent bytes
/// at a missing-fragment boundary from becoming observations.
fn recover_partial(pending: &PendingReport, hello: &HelloRecord) -> Vec<RelayedSubreport> {
    let mut candidates: Vec<(usize, RelayedSubreport)> = Vec::new();
    for fragment in pending.fragments.values() {
        let global_start = fragment.header.m as usize * hello.frame_payload_bytes as usize;
        let valid_len = (fragment.header.pooled_total_bytes as usize)
            .saturating_sub(global_start)
            .min(hello.frame_payload_bytes as usize);
        let bytes = &fragment.payload[..valid_len];
        let mut local = 0;
        while local + 40 <= bytes.len() {
            let length = 40 + 4 * bytes[local + 35] as usize;
            if length <= hello.subreport_bytes as usize && local + length <= bytes.len() {
                let raw = &bytes[local..local + length];
                if let Ok(subreport) = decode_subreport(raw)
                    && validate_observation(&subreport, pending.header.source_node_id, hello)
                        .is_ok()
                    && pending.header.peer_observed_bitmap & (1 << subreport.observed_node_id) != 0
                {
                    candidates.push((
                        global_start + local,
                        RelayedSubreport {
                            header: pending.header.clone(),
                            subreport,
                            subreport_bytes: raw.to_vec(),
                            provenance: ObservationProvenance::RecoveredPartialReport,
                            usb_sequence: fragment.usb_sequence,
                        },
                    ));
                    local += length;
                    continue;
                }
            }
            local += 1;
        }
    }
    candidates.sort_by_key(|(offset, _)| *offset);
    let order = expected_observed_ids(&pending.header, hello);
    let mut last_rank = None;
    candidates
        .into_iter()
        .filter_map(|(_, item)| {
            let rank = order
                .iter()
                .position(|id| *id == item.subreport.observed_node_id)?;
            if last_rank.is_some_and(|prior| rank <= prior) {
                return None;
            }
            last_rank = Some(rank);
            Some(item)
        })
        .collect()
}

fn validate_report_membership(
    header: &BeaconHeader,
    decoded: &[RelayedSubreport],
    hello: &HelloRecord,
) -> Result<(), ProtocolError> {
    let actual: Vec<u8> = decoded
        .iter()
        .map(|item| item.subreport.observed_node_id)
        .collect();
    if actual != expected_observed_ids(header, hello) {
        return Err(ProtocolError::Invalid(
            "subreports do not match rotated bitmap order",
        ));
    }
    Ok(())
}

fn expected_observed_ids(header: &BeaconHeader, hello: &HelloRecord) -> Vec<u8> {
    let start = ((header.k / hello.n_nodes as u32 + 1) % hello.n_nodes as u32) as u8;
    (0..hello.n_nodes)
        .map(|offset| (start + offset) % hello.n_nodes)
        .filter(|node| {
            *node != header.source_node_id && header.peer_observed_bitmap & (1 << *node) != 0
        })
        .collect()
}

fn u32_is_newer(candidate: u32, prior: u32) -> bool {
    let delta = candidate.wrapping_sub(prior);
    delta != 0 && delta < 0x8000_0000
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ObservationRoute {
    Local,
    Relayed,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CanonicalObservation {
    pub route: ObservationRoute,
    pub provenance: ObservationProvenance,
    pub reporting_node_id: u8,
    pub observed_node_id: u8,
    pub observed_k: u32,
    pub report_k: Option<u32>,
    pub usb_sequence: u32,
    pub obs_flags: u8,
    pub observed_m: u8,
    pub round_delta: u8,
    pub observed_tx_timestamp: u64,
    pub rx_timestamp: u64,
    pub cfo_raw: i16,
    pub fp_index_q10_6: u16,
    pub f1: u32,
    pub f2: u32,
    pub f3: u32,
    pub ip_power: u32,
    pub accum_count: u16,
    pub dgc_decision: u8,
    pub cir_start_offset: u16,
    pub cir_taps: u8,
    pub cir_bytes: Vec<u8>,
    pub subreport_bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CanonicalOutput {
    pub decoded: DecodedRecord,
    pub observations: Vec<CanonicalObservation>,
    pub configuration_changed: bool,
}

#[derive(Debug, Default)]
pub struct CanonicalProcessor {
    pub decoder_state: DecoderState,
    pub reassembler: ReportReassembler,
}

impl CanonicalProcessor {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn process(&mut self, record: &Record) -> Result<CanonicalOutput, ProtocolError> {
        let prior_hello = self.decoder_state.hello.clone();
        let decoded = decode_record(record, &mut self.decoder_state)?;
        let changed =
            matches!(&decoded, DecodedRecord::Hello(hello) if Some(hello) != prior_hello.as_ref());
        if changed {
            self.reassembler.reset();
        }
        let observations = match &decoded {
            DecodedRecord::LocalObservation(local) => vec![canonical_observation(
                ObservationRoute::Local,
                ObservationProvenance::CompleteReport,
                local.reporting_node_id,
                local.k,
                None,
                record.sequence,
                &local.subreport,
                &local.subreport_bytes,
            )],
            DecodedRecord::RadioFrame(radio) => {
                let hello = self
                    .decoder_state
                    .hello
                    .as_ref()
                    .expect("radio decode requires HELLO");
                self.reassembler
                    .add(radio, hello, record.sequence)?
                    .into_iter()
                    .map(|item| {
                        canonical_observation(
                            ObservationRoute::Relayed,
                            item.provenance,
                            item.header.source_node_id,
                            item.header
                                .k
                                .wrapping_sub(item.subreport.round_delta as u32),
                            Some(item.header.k),
                            item.usb_sequence,
                            &item.subreport,
                            &item.subreport_bytes,
                        )
                    })
                    .collect()
            }
            _ => Vec::new(),
        };
        Ok(CanonicalOutput {
            decoded,
            observations,
            configuration_changed: changed,
        })
    }
}

fn canonical_observation(
    route: ObservationRoute,
    provenance: ObservationProvenance,
    reporting_node_id: u8,
    observed_k: u32,
    report_k: Option<u32>,
    usb_sequence: u32,
    subreport: &Subreport,
    raw: &[u8],
) -> CanonicalObservation {
    CanonicalObservation {
        route,
        provenance,
        reporting_node_id,
        observed_node_id: subreport.observed_node_id,
        observed_k,
        report_k,
        usb_sequence,
        obs_flags: subreport.obs_flags,
        observed_m: subreport.observed_m,
        round_delta: subreport.round_delta,
        observed_tx_timestamp: subreport.observed_tx_timestamp,
        rx_timestamp: subreport.rx_timestamp,
        cfo_raw: subreport.cfo_raw,
        fp_index_q10_6: subreport.fp_index_q10_6,
        f1: subreport.f1,
        f2: subreport.f2,
        f3: subreport.f3,
        ip_power: subreport.ip_power,
        accum_count: subreport.accum_count,
        dgc_decision: subreport.dgc_decision,
        cir_start_offset: subreport.cir_start_offset,
        cir_taps: subreport.cir_iq.len() as u8,
        cir_bytes: subreport.cir_bytes.clone(),
        subreport_bytes: raw.to_vec(),
    }
}

fn le_u16(bytes: &[u8]) -> u16 {
    u16::from_le_bytes([bytes[0], bytes[1]])
}

fn le_i16(bytes: &[u8]) -> i16 {
    i16::from_le_bytes([bytes[0], bytes[1]])
}

fn le_u24(bytes: &[u8]) -> u32 {
    bytes[0] as u32 | (bytes[1] as u32) << 8 | (bytes[2] as u32) << 16
}

fn le_u32(bytes: &[u8]) -> u32 {
    u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]])
}

fn le_u64(bytes: &[u8]) -> u64 {
    u64::from_le_bytes(bytes[..8].try_into().expect("eight-byte slice"))
}

#[cfg(test)]
mod tests;
