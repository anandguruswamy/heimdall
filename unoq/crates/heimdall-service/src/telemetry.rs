use flatbuffers::{FlatBufferBuilder, WIPOffset};

/// The number of topics that participate in the pipeline's u8 DSP demand
/// mask. SeatInference (8) is produced outside the DSP pipeline and must
/// never be shifted into that mask.
pub const DSP_TOPIC_COUNT: usize = 8;
/// Total number of subscribable topics (demand-counter array length).
pub const TOPIC_COUNT: usize = 9;

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
    SeatInference = 8,
}

impl Topic {
    /// DSP demand-mask bit; only valid for the first DSP_TOPIC_COUNT topics.
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
            "seat-inference" => Self::SeatInference,
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
        8 => Topic::SeatInference,
        _ => return None,
    })
}

pub fn envelope_key(bytes: &[u8]) -> Option<String> {
    let topic = envelope_topic(bytes)?;
    let table = u32::from_le_bytes(bytes.get(0..4)?.try_into().ok()?) as usize;
    let distance = i32::from_le_bytes(bytes.get(table..table + 4)?.try_into().ok()?);
    let vtable = table.checked_sub(usize::try_from(distance).ok()?)?;
    let vtable_len = u16::from_le_bytes(bytes.get(vtable..vtable + 2)?.try_into().ok()?) as usize;
    if vtable_len < 18 {
        return Some(format!("{}", topic as u8));
    }
    let offset = u16::from_le_bytes(bytes.get(vtable + 16..vtable + 18)?.try_into().ok()?) as usize;
    if offset == 0 {
        return Some(format!("{}", topic as u8));
    }
    let field = table.checked_add(offset)?;
    let vector = field
        .checked_add(u32::from_le_bytes(bytes.get(field..field + 4)?.try_into().ok()?) as usize)?;
    let length = u32::from_le_bytes(bytes.get(vector..vector + 4)?.try_into().ok()?) as usize;
    let payload = bytes.get(vector + 4..vector + 4 + length)?;
    let value: serde_json::Value = serde_json::from_slice(payload).ok()?;
    let Some(from) = value.get("from").and_then(serde_json::Value::as_u64) else {
        return Some(format!("{}", topic as u8));
    };
    let to = value
        .get("to")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    let subtype = if topic == Topic::Distance {
        value
            .get("sample")
            .and_then(|sample| sample.get("kind"))
            .and_then(serde_json::Value::as_str)
            .or_else(|| value.get("raw_ds").map(|_| "ds"))
            .unwrap_or("ss")
    } else {
        ""
    };
    Some(format!("{}:{from}:{to}:{subtype}", topic as u8))
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

pub fn envelope_batch(messages: impl IntoIterator<Item = Vec<u8>>) -> Vec<u8> {
    let messages = messages.into_iter().collect::<Vec<_>>();
    let count = u16::try_from(messages.len()).unwrap_or(u16::MAX) as usize;
    let capacity = 8 + messages
        .iter()
        .take(count)
        .map(|message| 4 + message.len())
        .sum::<usize>();
    let mut batch = Vec::with_capacity(capacity);
    batch.extend_from_slice(b"HMB1");
    batch.extend_from_slice(&(count as u16).to_le_bytes());
    batch.extend_from_slice(&0_u16.to_le_bytes());
    for message in messages.into_iter().take(count) {
        batch.extend_from_slice(&(message.len() as u32).to_le_bytes());
        batch.extend_from_slice(&message);
    }
    batch
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
        assert_eq!(envelope_key(&bytes).as_deref(), Some("2"));

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

    #[test]
    fn envelope_key_separates_distance_links_and_kinds() {
        let ss = envelope(
            Topic::Distance,
            1,
            1,
            1,
            0,
            br#"{"from":0,"to":1,"sample":{"kind":"ss"}}"#,
        );
        let ds = envelope(
            Topic::Distance,
            2,
            1,
            1,
            0,
            br#"{"from":0,"to":1,"sample":{"kind":"ds"}}"#,
        );
        assert_eq!(envelope_key(&ss).as_deref(), Some("1:0:1:ss"));
        assert_eq!(envelope_key(&ds).as_deref(), Some("1:0:1:ds"));
    }

    #[test]
    fn batch_retains_complete_envelopes() {
        let first = envelope(Topic::Health, 1, 1, 1, 0, br#"{"round":1}"#);
        let second = envelope(Topic::Distance, 2, 1, 1, 0, br#"{"from":0,"to":1}"#);
        let batch = envelope_batch([first.clone(), second.clone()]);
        assert_eq!(&batch[..4], b"HMB1");
        assert_eq!(u16::from_le_bytes(batch[4..6].try_into().unwrap()), 2);
        let first_len = u32::from_le_bytes(batch[8..12].try_into().unwrap()) as usize;
        assert_eq!(&batch[12..12 + first_len], first);
        let second_at = 12 + first_len;
        let second_len =
            u32::from_le_bytes(batch[second_at..second_at + 4].try_into().unwrap()) as usize;
        assert_eq!(&batch[second_at + 4..second_at + 4 + second_len], second);
    }
}
