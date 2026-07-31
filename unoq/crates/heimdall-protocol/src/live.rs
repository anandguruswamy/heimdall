//! Datagram framing for the live UNO Q to host link.
//!
//! Each USB record is independently framed so loss never corrupts the next
//! record. Records larger than a safe LAN datagram are fragmented; the host
//! drops the complete record when any fragment is missing.

use crc32fast::hash as crc32;
use thiserror::Error;

pub const LIVE_MAGIC: [u8; 4] = *b"HML1";
pub const LIVE_VERSION: u8 = 1;
pub const LIVE_HEADER_BYTES: usize = 38;
pub const LIVE_PAYLOAD_BYTES: usize = 1_152;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum LiveError {
    #[error("live datagram is too short")]
    TooShort,
    #[error("invalid live datagram header")]
    InvalidHeader,
    #[error("invalid live datagram checksum")]
    InvalidChecksum,
    #[error("live record is too large")]
    TooLarge,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LiveFragment {
    pub session_id: u64,
    pub transport_sequence: u64,
    pub usb_sequence: u32,
    pub fragment_index: u16,
    pub fragment_count: u16,
    pub payload: Vec<u8>,
}

impl LiveFragment {
    pub fn encode(&self) -> Result<Vec<u8>, LiveError> {
        if self.fragment_count == 0
            || self.fragment_index >= self.fragment_count
            || self.payload.len() > LIVE_PAYLOAD_BYTES
        {
            return Err(LiveError::InvalidHeader);
        }
        let payload_len = u16::try_from(self.payload.len()).map_err(|_| LiveError::TooLarge)?;
        let mut bytes = Vec::with_capacity(LIVE_HEADER_BYTES + self.payload.len());
        bytes.extend_from_slice(&LIVE_MAGIC);
        bytes.push(LIVE_VERSION);
        bytes.push(0);
        bytes.extend_from_slice(&(LIVE_HEADER_BYTES as u16).to_le_bytes());
        bytes.extend_from_slice(&self.session_id.to_le_bytes());
        bytes.extend_from_slice(&self.transport_sequence.to_le_bytes());
        bytes.extend_from_slice(&self.usb_sequence.to_le_bytes());
        bytes.extend_from_slice(&self.fragment_index.to_le_bytes());
        bytes.extend_from_slice(&self.fragment_count.to_le_bytes());
        bytes.extend_from_slice(&payload_len.to_le_bytes());
        bytes.extend_from_slice(&crc32(&self.payload).to_le_bytes());
        bytes.extend_from_slice(&self.payload);
        Ok(bytes)
    }

    pub fn decode(bytes: &[u8]) -> Result<Self, LiveError> {
        if bytes.len() < LIVE_HEADER_BYTES {
            return Err(LiveError::TooShort);
        }
        if bytes[..4] != LIVE_MAGIC
            || bytes[4] != LIVE_VERSION
            || u16::from_le_bytes([bytes[6], bytes[7]]) as usize != LIVE_HEADER_BYTES
        {
            return Err(LiveError::InvalidHeader);
        }
        let payload_len = u16::from_le_bytes([bytes[32], bytes[33]]) as usize;
        if payload_len > LIVE_PAYLOAD_BYTES || bytes.len() != LIVE_HEADER_BYTES + payload_len {
            return Err(LiveError::InvalidHeader);
        }
        let payload = bytes[LIVE_HEADER_BYTES..].to_vec();
        if crc32(&payload) != u32::from_le_bytes([bytes[34], bytes[35], bytes[36], bytes[37]]) {
            return Err(LiveError::InvalidChecksum);
        }
        let fragment_index = u16::from_le_bytes([bytes[28], bytes[29]]);
        let fragment_count = u16::from_le_bytes([bytes[30], bytes[31]]);
        if fragment_count == 0 || fragment_index >= fragment_count {
            return Err(LiveError::InvalidHeader);
        }
        Ok(Self {
            session_id: u64::from_le_bytes(bytes[8..16].try_into().unwrap()),
            transport_sequence: u64::from_le_bytes(bytes[16..24].try_into().unwrap()),
            usb_sequence: u32::from_le_bytes(bytes[24..28].try_into().unwrap()),
            fragment_index,
            fragment_count,
            payload,
        })
    }
}

pub fn fragment_record(
    session_id: u64,
    transport_sequence: u64,
    usb_sequence: u32,
    record: &[u8],
) -> Result<Vec<Vec<u8>>, LiveError> {
    let count = record.len().div_ceil(LIVE_PAYLOAD_BYTES).max(1);
    let count = u16::try_from(count).map_err(|_| LiveError::TooLarge)?;
    record
        .chunks(LIVE_PAYLOAD_BYTES)
        .enumerate()
        .map(|(index, payload)| {
            LiveFragment {
                session_id,
                transport_sequence,
                usb_sequence,
                fragment_index: index as u16,
                fragment_count: count,
                payload: payload.to_vec(),
            }
            .encode()
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fragments_round_trip_independently() {
        let record = vec![0x5a; LIVE_PAYLOAD_BYTES + 1];
        let packets = fragment_record(7, 11, 13, &record).unwrap();
        assert_eq!(packets.len(), 2);
        let first = LiveFragment::decode(&packets[0]).unwrap();
        assert_eq!(first.session_id, 7);
        assert_eq!(first.transport_sequence, 11);
        assert_eq!(first.usb_sequence, 13);
        assert_eq!(first.fragment_count, 2);
        assert_eq!(first.payload.len(), LIVE_PAYLOAD_BYTES);
    }

    #[test]
    fn checksum_rejects_corruption() {
        let mut packet = fragment_record(1, 2, 3, b"record").unwrap().remove(0);
        *packet.last_mut().unwrap() ^= 1;
        assert_eq!(
            LiveFragment::decode(&packet),
            Err(LiveError::InvalidChecksum)
        );
    }
}
