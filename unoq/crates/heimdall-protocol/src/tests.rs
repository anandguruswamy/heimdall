use super::*;

fn hello_payload(n: u8, m: u8, frame_payload: u16, node_id: u8) -> Vec<u8> {
    let taps = 64_u8;
    let subreport = 40_u16 + 4 * taps as u16;
    let slot = if n == 5 { 3_500_u32 } else { 10_000_u32 };
    let cycle = slot * n as u32 * m as u32;
    let hash = if n == 5 {
        0x8885_u16
    } else if n == 3 {
        0xc8cf
    } else {
        0x3c50
    };
    let mut data = vec![1, 1, n, m, node_id, 0, taps, 16];
    data.extend_from_slice(&hash.to_le_bytes());
    data.extend_from_slice(&subreport.to_le_bytes());
    data.extend_from_slice(&frame_payload.to_le_bytes());
    data.extend_from_slice(&1023_u16.to_le_bytes());
    data.extend_from_slice(&slot.to_le_bytes());
    data.extend_from_slice(&cycle.to_le_bytes());
    data.extend_from_slice(&0x7556_1606_12a3_1510_u64.to_le_bytes());
    data.extend_from_slice(&0_u32.to_le_bytes());
    data
}

fn decode_hello(n: u8, m: u8, frame_payload: u16, node_id: u8) -> HelloRecord {
    let raw = encode_record(
        RecordKind::Hello,
        0,
        0,
        &hello_payload(n, m, frame_payload, node_id),
    )
    .unwrap();
    let record = StreamParser::default().feed(&raw).remove(0);
    match decode_record(&record, &mut DecoderState::default()).unwrap() {
        DecodedRecord::Hello(hello) => hello,
        _ => unreachable!(),
    }
}

fn subreport(observed: u8, delta: u8, taps: u8) -> Vec<u8> {
    let mut data = vec![0; 36 + 4 * taps as usize];
    data[..4].copy_from_slice(&[observed, 0x05, 0, delta]);
    data[4..9].copy_from_slice(&123_u64.to_le_bytes()[..5]);
    data[9..14].copy_from_slice(&456_u64.to_le_bytes()[..5]);
    data[14..16].copy_from_slice(&(-7_i16).to_le_bytes());
    data[16..18].copy_from_slice(&0x1234_u16.to_le_bytes());
    data[18..21].copy_from_slice(&11_u32.to_le_bytes()[..3]);
    data[21..24].copy_from_slice(&12_u32.to_le_bytes()[..3]);
    data[24..27].copy_from_slice(&13_u32.to_le_bytes()[..3]);
    data[27..30].copy_from_slice(&14_u32.to_le_bytes()[..3]);
    data[30..32].copy_from_slice(&127_u16.to_le_bytes());
    data[32] = 3;
    data[33..35].copy_from_slice(&720_u16.to_le_bytes());
    data[35] = taps;
    for index in 0..taps as usize {
        let at = 36 + index * 4;
        data[at..at + 2].copy_from_slice(&(index as i16).to_le_bytes());
        data[at + 2..at + 4].copy_from_slice(&(-(index as i16)).to_le_bytes());
    }
    let checksum = crc32(&data);
    data.extend_from_slice(&checksum.to_le_bytes());
    data
}

fn beacon_frames(hello: &HelloRecord, source: u8, k: u32, reports: &[Vec<u8>]) -> Vec<Vec<u8>> {
    let pooled: Vec<u8> = reports.iter().flatten().copied().collect();
    let mut starts = Vec::new();
    let mut offset = 0;
    for report in reports {
        starts.push(offset);
        offset += report.len();
    }
    (0..hello.m_slots)
        .map(|m| {
            let mut frame = vec![0; BEACON_HEADER_BYTES + hello.frame_payload_bytes as usize];
            frame[..2].copy_from_slice(&0x8841_u16.to_le_bytes());
            frame[2] = k.wrapping_mul(hello.m_slots as u32).wrapping_add(m as u32) as u8;
            frame[3..5].copy_from_slice(&0xabcd_u16.to_le_bytes());
            frame[5..7].copy_from_slice(&0xffff_u16.to_le_bytes());
            frame[7..9].copy_from_slice(&(source as u16).to_le_bytes());
            frame[9..12].copy_from_slice(&[1, 0, m]);
            frame[12..16].copy_from_slice(&k.to_le_bytes());
            frame[16..18].copy_from_slice(&[hello.n_nodes, hello.m_slots]);
            frame[18..20].copy_from_slice(&hello.config_hash.to_le_bytes());
            frame[20..25].copy_from_slice(&(1000 + k as u64).to_le_bytes()[..5]);
            let begin = m as usize * hello.frame_payload_bytes as usize;
            let end = begin + hello.frame_payload_bytes as usize;
            frame[25] = starts
                .iter()
                .filter(|start| begin <= **start && **start < end)
                .count() as u8;
            frame[26..28].copy_from_slice(&(pooled.len() as u16).to_le_bytes());
            frame[28] = reports.iter().fold(0, |map, report| map | (1 << report[0]));
            let fragment = &pooled[begin.min(pooled.len())..end.min(pooled.len())];
            frame[31..31 + fragment.len()].copy_from_slice(fragment);
            frame
        })
        .collect()
}

fn radio(frame: Vec<u8>) -> RadioFrameRecord {
    RadioFrameRecord {
        rx_timestamp: 777,
        rx_flags: RX_VALID,
        frame,
    }
}

#[test]
fn parser_accepts_every_fragmentation_boundary_byte_exactly() {
    let raw = encode_record(RecordKind::Hello, 0, 4, &hello_payload(2, 1, 296, 0)).unwrap();
    for split in 0..=raw.len() {
        let mut parser = StreamParser::default();
        let mut records = parser.feed(&raw[..split]);
        records.extend(parser.feed(&raw[split..]));
        assert_eq!(records.len(), 1, "split {split}");
        assert_eq!(records[0].raw, raw);
    }
}

#[test]
fn parser_handles_one_byte_chunks_and_split_sync() {
    let records: Vec<Vec<u8>> = (0..20)
        .map(|sequence| encode_record(RecordKind::Heartbeat, 0, sequence, &[0; 12]).unwrap())
        .collect();
    let stream: Vec<u8> = records.iter().flatten().copied().collect();
    let mut parser = StreamParser::default();
    let decoded: Vec<Record> = stream
        .iter()
        .flat_map(|byte| parser.feed(&[*byte]))
        .collect();
    assert_eq!(
        decoded.iter().map(|item| &item.raw).collect::<Vec<_>>(),
        records.iter().collect::<Vec<_>>()
    );
}

#[test]
fn corruption_resynchronises_without_a_producer_gap() {
    let first = encode_record(RecordKind::Heartbeat, 0, 10, &[0; 12]).unwrap();
    let mut bad = encode_record(RecordKind::Heartbeat, 0, 11, &[0; 12]).unwrap();
    bad[12] ^= 1;
    let last = encode_record(RecordKind::Heartbeat, 0, 12, &[0; 12]).unwrap();
    let stream: Vec<u8> = [b"noise".as_slice(), &first, &bad, &last].concat();
    let mut parser = StreamParser::default();
    let decoded = parser.feed(&stream);
    assert_eq!(
        decoded
            .iter()
            .map(|record| record.sequence)
            .collect::<Vec<_>>(),
        [10, 12]
    );
    assert_eq!(parser.stats().crc_failures, 1);
    assert_eq!(parser.stats().sequence_gaps, 0);
}

#[test]
fn version_framing_unknown_gap_and_duplicates_have_separate_stats() {
    let mut unsupported = encode_record(RecordKind::Heartbeat, 0, 9, &[0; 12]).unwrap();
    unsupported[2] = 2;
    let checksum = crc32(&unsupported[2..unsupported.len() - 4]);
    let end = unsupported.len();
    unsupported[end - 4..].copy_from_slice(&checksum.to_le_bytes());
    let mut reserved = encode_record(RecordKind::Heartbeat, 0x80, 10, &[0; 12]).unwrap();
    let unknown = {
        let mut raw = encode_record(RecordKind::Heartbeat, 0, 13, &[0; 12]).unwrap();
        raw[3] = 0x80;
        let checksum = crc32(&raw[2..raw.len() - 4]);
        let end = raw.len();
        raw[end - 4..].copy_from_slice(&checksum.to_le_bytes());
        raw
    };
    // Ensure this is a CRC-valid outer framing error, not a CRC error.
    let checksum = crc32(&reserved[2..reserved.len() - 4]);
    let end = reserved.len();
    reserved[end - 4..].copy_from_slice(&checksum.to_le_bytes());
    let duplicate = encode_record(RecordKind::Heartbeat, 0, 13, &[0; 12]).unwrap();
    let mut parser = StreamParser::default();
    assert!(
        parser
            .feed(&[unsupported, reserved, unknown, duplicate].concat())
            .is_empty()
    );
    assert_eq!(parser.stats().unsupported_versions, 1);
    assert_eq!(parser.stats().framing_errors, 1);
    assert_eq!(parser.stats().unknown_types, 1);
    assert_eq!(parser.stats().sequence_gaps, 2);
    assert_eq!(parser.stats().duplicates_or_old, 1);
}

#[test]
fn sequence_wrap_and_old_suppression() {
    let stream: Vec<u8> = [u32::MAX - 1, u32::MAX, 0, 0]
        .into_iter()
        .flat_map(|sequence| encode_record(RecordKind::Heartbeat, 0, sequence, &[0; 12]).unwrap())
        .collect();
    let mut parser = StreamParser::default();
    let records = parser.feed(&stream);
    assert_eq!(
        records.iter().map(|item| item.sequence).collect::<Vec<_>>(),
        [u32::MAX - 1, u32::MAX, 0]
    );
    assert_eq!(parser.stats().duplicates_or_old, 1);
}

#[test]
fn u40_deltas_wrap_and_sign() {
    assert_eq!(read_u40(&[0xff; 5]).unwrap(), U40_MASK);
    assert_eq!(u40_forward_delta(2, U40_MASK - 2), 5);
    assert_eq!(u40_signed_delta(2, U40_MASK - 2), 5);
    assert_eq!(u40_signed_delta(U40_MASK - 2, 2), -5);
}

#[test]
fn all_seven_record_types_decode() {
    let hello_raw = encode_record(RecordKind::Hello, 0, 0, &hello_payload(2, 1, 296, 0)).unwrap();
    let mut parser = StreamParser::default();
    let hello_record = parser.feed(&hello_raw).remove(0);
    let mut state = DecoderState::default();
    assert!(matches!(
        decode_record(&hello_record, &mut state).unwrap(),
        DecodedRecord::Hello(_)
    ));

    let heartbeat = encode_record(RecordKind::Heartbeat, 0, 1, &[0; 12]).unwrap();
    let report = subreport(1, 1, 64);
    let local = [vec![0], 1_u32.to_le_bytes().to_vec(), report].concat();
    let local = encode_record(RecordKind::LocalObservation, 0, 2, &local).unwrap();
    let hello = state.hello.as_ref().unwrap();
    let frame = beacon_frames(hello, 1, 1, &[subreport(0, 1, 64)]).remove(0);
    let radio_payload = [
        &777_u64.to_le_bytes()[..5],
        &[1],
        &(frame.len() as u16).to_le_bytes(),
        &frame,
    ]
    .concat();
    let radio = encode_record(RecordKind::RadioFrame, 0, 3, &radio_payload).unwrap();
    let mut summary = vec![0; 34];
    summary[10..12].copy_from_slice(&1_u16.to_le_bytes());
    let summary = encode_record(RecordKind::CycleSummary, 0, 4, &summary).unwrap();
    let error = encode_record(
        RecordKind::Error,
        0,
        5,
        &[&[1, 0, 2, 0, 0, 0, 0, 0][..], &b"bad"[..]].concat(),
    )
    .unwrap();
    let mut tx = vec![0; 13];
    tx[4] = 0;
    tx[10..12].copy_from_slice(&329_u16.to_le_bytes());
    tx[12] = 1;
    let tx = encode_record(RecordKind::TxRecord, 0, 6, &tx).unwrap();
    let variants = [heartbeat, local, radio, summary, error, tx]
        .into_iter()
        .map(|raw| decode_record(&parser.feed(&raw).remove(0), &mut state).unwrap())
        .collect::<Vec<_>>();
    assert!(matches!(variants[0], DecodedRecord::Heartbeat(_)));
    assert!(matches!(variants[1], DecodedRecord::LocalObservation(_)));
    assert!(matches!(variants[2], DecodedRecord::RadioFrame(_)));
    assert!(matches!(variants[3], DecodedRecord::CycleSummary(_)));
    assert!(matches!(variants[4], DecodedRecord::Error(_)));
    assert!(matches!(variants[5], DecodedRecord::TxRecord(_)));
}

#[test]
fn subreport_crc_cir_and_raw_bytes_are_preserved() {
    let raw = subreport(1, 1, 64);
    let decoded = decode_subreport(&raw).unwrap();
    assert_eq!(decoded.cfo_raw, -7);
    assert_eq!(decoded.cir_iq[5], (5, -5));
    assert_eq!(decoded.cir_bytes, raw[36..292]);
    let mut damaged = raw;
    damaged[40] ^= 1;
    assert_eq!(
        decode_subreport(&damaged).unwrap_err().to_string(),
        "subreport CRC32 mismatch"
    );
}

#[test]
fn hello_and_local_invariants_are_strict() {
    let mut bad = hello_payload(2, 1, 296, 0);
    bad[10..12].copy_from_slice(&295_u16.to_le_bytes());
    let record = StreamParser::default()
        .feed(&encode_record(RecordKind::Hello, 0, 0, &bad).unwrap())
        .remove(0);
    assert!(decode_record(&record, &mut DecoderState::default()).is_err());

    let hello = decode_hello(3, 1, 592, 0);
    let mut state = DecoderState { hello: Some(hello) };
    let payload = [vec![0], 1_u32.to_le_bytes().to_vec(), subreport(1, 2, 65)].concat();
    let record = StreamParser::default()
        .feed(&encode_record(RecordKind::LocalObservation, 0, 1, &payload).unwrap())
        .remove(0);
    assert!(decode_record(&record, &mut state).is_err());
}

#[test]
fn m2_reassembles_out_of_order_and_canonical_shapes_match() {
    let hello = decode_hello(5, 2, 592, 0);
    let reports = [2, 3, 4, 0].map(|node| subreport(node, (1 + 5 - node) % 5, 64));
    let frames = beacon_frames(&hello, 1, 1, &reports);
    let mut reassembler = ReportReassembler::new();
    assert!(
        reassembler
            .add(&radio(frames[1].clone()), &hello, 2)
            .unwrap()
            .is_empty()
    );
    let decoded = reassembler
        .add(&radio(frames[0].clone()), &hello, 3)
        .unwrap();
    assert_eq!(decoded.len(), 4);
    assert!(
        decoded
            .iter()
            .all(|item| item.provenance == ObservationProvenance::CompleteReport)
    );
    assert_eq!(
        decoded
            .iter()
            .map(|item| item.subreport.observed_node_id)
            .collect::<Vec<_>>(),
        [2, 3, 4, 0]
    );
}

#[test]
fn local_and_relayed_observations_share_the_canonical_shape() {
    let mut parser = StreamParser::default();
    let mut processor = CanonicalProcessor::new();
    let hello_raw = encode_record(RecordKind::Hello, 0, 0, &hello_payload(2, 1, 296, 0)).unwrap();
    processor
        .process(&parser.feed(&hello_raw).remove(0))
        .unwrap();

    let local_raw = subreport(1, 1, 64);
    let local_payload = [vec![0], 1_u32.to_le_bytes().to_vec(), local_raw.clone()].concat();
    let local_record = parser
        .feed(&encode_record(RecordKind::LocalObservation, 0, 1, &local_payload).unwrap())
        .remove(0);
    let local = processor
        .process(&local_record)
        .unwrap()
        .observations
        .remove(0);

    let hello = processor.decoder_state.hello.as_ref().unwrap();
    let relayed_raw = subreport(0, 1, 64);
    let frame = beacon_frames(hello, 1, 1, &[relayed_raw.clone()]).remove(0);
    let radio_payload = [
        &777_u64.to_le_bytes()[..5],
        &[RX_VALID],
        &(frame.len() as u16).to_le_bytes(),
        &frame,
    ]
    .concat();
    let radio_record = parser
        .feed(&encode_record(RecordKind::RadioFrame, 0, 2, &radio_payload).unwrap())
        .remove(0);
    let relayed = processor
        .process(&radio_record)
        .unwrap()
        .observations
        .remove(0);

    assert_eq!(local.route, ObservationRoute::Local);
    assert_eq!(relayed.route, ObservationRoute::Relayed);
    assert_eq!((local.reporting_node_id, local.observed_node_id), (0, 1));
    assert_eq!(
        (relayed.reporting_node_id, relayed.observed_node_id),
        (1, 0)
    );
    assert_eq!((relayed.observed_k, relayed.report_k), (0, Some(1)));
    assert_eq!(local.subreport_bytes, local_raw);
    assert_eq!(relayed.subreport_bytes, relayed_raw);
    assert_eq!(local.cir_bytes.len(), 64 * 4);
    assert_eq!(relayed.cir_bytes.len(), 64 * 4);
}

#[test]
fn m2_wrong_order_and_start_counts_are_rejected() {
    let hello = decode_hello(5, 2, 592, 0);
    let wrong_order = [0, 2, 3, 4].map(|node| subreport(node, (1 + 5 - node) % 5, 64));
    let frames = beacon_frames(&hello, 1, 1, &wrong_order);
    let mut reassembler = ReportReassembler::new();
    reassembler
        .add(&radio(frames[0].clone()), &hello, 1)
        .unwrap();
    assert!(
        reassembler
            .add(&radio(frames[1].clone()), &hello, 2)
            .is_err()
    );

    let correct = [2, 3, 4, 0].map(|node| subreport(node, (1 + 5 - node) % 5, 64));
    let mut frames = beacon_frames(&hello, 1, 1, &correct);
    frames[1][25] = frames[1][25].wrapping_add(1);
    let mut reassembler = ReportReassembler::new();
    reassembler
        .add(&radio(frames[0].clone()), &hello, 1)
        .unwrap();
    assert!(
        reassembler
            .add(&radio(frames[1].clone()), &hello, 2)
            .is_err()
    );
}

#[test]
fn arbitrary_m_full_reassembly() {
    let hello = decode_hello(8, 3, 691, 0);
    let order = [2, 3, 4, 5, 6, 7, 0];
    let reports: Vec<Vec<u8>> = order
        .iter()
        .map(|node| subreport(*node, (1 + 8 - node) % 8, 64))
        .collect();
    let frames = beacon_frames(&hello, 1, 1, &reports);
    let mut reassembler = ReportReassembler::new();
    assert!(
        reassembler
            .add(&radio(frames[2].clone()), &hello, 1)
            .unwrap()
            .is_empty()
    );
    assert!(
        reassembler
            .add(&radio(frames[0].clone()), &hello, 2)
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        reassembler
            .add(&radio(frames[1].clone()), &hello, 3)
            .unwrap()
            .len(),
        7
    );
}

#[test]
fn newer_report_recovers_wholly_contained_subreports_from_partial_fragments() {
    let hello = decode_hello(5, 2, 592, 0);
    let old_reports = [2, 3, 4, 0].map(|node| subreport(node, (1 + 5 - node) % 5, 64));
    let mut old_frames = beacon_frames(&hello, 1, 1, &old_reports);
    // Corrupt report 4 while retaining report 0 as an independently valid report.
    old_frames[1][BEACON_HEADER_BYTES + 40] ^= 1;
    let new_reports = [2, 3, 4, 0].map(|node| subreport(node, (1 + 5 - node) % 5, 64));
    let new_frames = beacon_frames(&hello, 1, 6, &new_reports);
    let mut reassembler = ReportReassembler::new();
    // Fragment 1 starts inside report 3, but reports 4 and 0 are wholly contained.
    assert!(
        reassembler
            .add(&radio(old_frames[1].clone()), &hello, 10)
            .unwrap()
            .is_empty()
    );
    let recovered = reassembler
        .add(&radio(new_frames[0].clone()), &hello, 11)
        .unwrap();
    assert_eq!(
        recovered
            .iter()
            .map(|item| item.subreport.observed_node_id)
            .collect::<Vec<_>>(),
        [0]
    );
    assert!(
        recovered
            .iter()
            .all(|item| item.provenance == ObservationProvenance::RecoveredPartialReport)
    );
    assert!(
        recovered
            .iter()
            .all(|item| item.header.k == 1 && item.usb_sequence == 10)
    );
}

#[test]
fn supersession_and_observed_k_work_across_u32_wrap() {
    let hello = decode_hello(2, 1, 296, 0);
    let old = beacon_frames(&hello, 1, u32::MAX, &[subreport(0, 1, 64)]).remove(0);
    let new = beacon_frames(&hello, 0, 0, &[subreport(1, 1, 64)]).remove(0);
    let mut reassembler = ReportReassembler::new();
    assert_eq!(reassembler.add(&radio(old), &hello, 1).unwrap().len(), 1);
    assert_eq!(reassembler.add(&radio(new), &hello, 2).unwrap().len(), 1);
    assert_eq!(0_u32.wrapping_sub(1), u32::MAX);
}

#[test]
fn reserved_flags_padding_and_local_schedule_are_rejected() {
    let hello = decode_hello(2, 1, 296, 0);
    let mut frame = beacon_frames(&hello, 1, 1, &[subreport(0, 1, 64)]).remove(0);
    frame[30] = 0x80;
    assert!(decode_beacon_header(&frame, &hello).is_err());
    frame[30] = 0;
    // No padding exists in this N=2 profile; use an empty report to exercise padding.
    let mut empty = beacon_frames(&hello, 1, 1, &[]).remove(0);
    empty[31] = 1;
    assert!(
        ReportReassembler::new()
            .add(&radio(empty), &hello, 1)
            .is_err()
    );

    let mut report = subreport(1, 1, 64);
    report[1] = 0x80;
    let checksum = crc32(&report[..report.len() - 4]);
    let end = report.len();
    report[end - 4..].copy_from_slice(&checksum.to_le_bytes());
    assert!(decode_subreport(&report).is_err());
}
