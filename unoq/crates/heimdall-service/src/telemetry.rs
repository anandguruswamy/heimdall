use flatbuffers::{FlatBufferBuilder, WIPOffset};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[repr(u8)]
pub enum Topic {
    Health = 0,
    Distance = 1,
    Cir = 2,
    Waterfall = 3,
    SlowFft = 4,
    FastFft = 5,
    Cfo = 6,
    Calibration = 7,
}

impl Topic {
    pub const fn bit(self) -> u8 {
        1 << self as u8
    }

    pub fn from_subscription(value: &str) -> Option<Self> {
        Some(match value {
            "network-health" | "health" => Self::Health,
            "live-distance" | "distance" => Self::Distance,
            "instantaneous-cir" | "cir" => Self::Cir,
            "cir-waterfall" | "waterfall" => Self::Waterfall,
            "slow-time-fft" | "slow-fft" => Self::SlowFft,
            "fast-time-fft" | "fast-fft" => Self::FastFft,
            "cfo" => Self::Cfo,
            "distance-calibration" | "calibration" => Self::Calibration,
            _ => return None,
        })
    }
}

pub fn envelope_topic(bytes: &[u8]) -> Option<Topic> {
    if !flatbuffers::buffer_has_identifier(bytes, "HMT1", false) || bytes.len() < 12 {
        return None;
    }
    let table = u32::from_le_bytes(bytes.get(0..4)?.try_into().ok()?) as usize;
    let vtable_distance = i32::from_le_bytes(bytes.get(table..table + 4)?.try_into().ok()?);
    if vtable_distance <= 0 {
        return None;
    }
    let vtable = table.checked_sub(vtable_distance as usize)?;
    let vtable_len = u16::from_le_bytes(bytes.get(vtable..vtable + 2)?.try_into().ok()?) as usize;
    let value = if vtable_len >= 8 {
        let offset =
            u16::from_le_bytes(bytes.get(vtable + 6..vtable + 8)?.try_into().ok()?) as usize;
        if offset == 0 {
            0
        } else {
            *bytes.get(table + offset)?
        }
    } else {
        0
    };
    Some(match value {
        0 => Topic::Health,
        1 => Topic::Distance,
        2 => Topic::Cir,
        3 => Topic::Waterfall,
        4 => Topic::SlowFft,
        5 => Topic::FastFft,
        6 => Topic::Cfo,
        7 => Topic::Calibration,
        _ => return None,
    })
}

pub fn envelope(
    topic: Topic,
    stream_sequence: u64,
    configuration_epoch: u64,
    processing_epoch: u64,
    dropped_events: u32,
    payload: &[u8],
) -> Vec<u8> {
    let mut builder = FlatBufferBuilder::with_capacity(payload.len() + 128);
    let payload = builder.create_vector(payload);
    let start = builder.start_table();
    builder.push_slot::<u16>(4, 1, 1);
    builder.push_slot::<u8>(6, topic as u8, 0);
    builder.push_slot::<u64>(8, stream_sequence, 0);
    builder.push_slot::<u64>(10, configuration_epoch, 0);
    builder.push_slot::<u64>(12, processing_epoch, 0);
    builder.push_slot::<u32>(14, dropped_events, 0);
    builder.push_slot_always::<WIPOffset<_>>(16, payload);
    let root = builder.end_table(start);
    builder.finish(root, Some("HMT1"));
    builder.finished_data().to_vec()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn envelope_has_identifier_and_payload() {
        let payload = br#"{"link":"1>2"}"#;
        let bytes = envelope(Topic::Cir, 7, 2, 3, 1, payload);
        assert_eq!(&bytes[4..8], b"HMT1");
        assert!(flatbuffers::buffer_has_identifier(&bytes, "HMT1", false));
        assert_eq!(envelope_topic(&bytes), Some(Topic::Cir));

        let table = u32::from_le_bytes(bytes[0..4].try_into().unwrap()) as usize;
        let vtable =
            table - i32::from_le_bytes(bytes[table..table + 4].try_into().unwrap()) as usize;
        let payload_offset =
            u16::from_le_bytes(bytes[vtable + 16..vtable + 18].try_into().unwrap()) as usize;
        let vector_field = table + payload_offset;
        let vector = vector_field
            + u32::from_le_bytes(bytes[vector_field..vector_field + 4].try_into().unwrap())
                as usize;
        let length = u32::from_le_bytes(bytes[vector..vector + 4].try_into().unwrap()) as usize;
        assert_eq!(&bytes[vector + 4..vector + 4 + length], payload);
    }
}
