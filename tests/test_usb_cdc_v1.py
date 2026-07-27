import struct
import unittest
import zlib

from unoq.heimdall.protocol import (
    CYCLE_SUMMARY,
    DecoderState,
    HEARTBEAT,
    HELLO,
    LOCAL_OBS,
    ProtocolError,
    RADIO_FRAME,
    StreamParser,
    TX_RECORD,
    decode_record,
    encode_record,
)
from unoq.heimdall.replay import replay_bytes
from unoq.heimdall.inspect_capture import inspect


def hello_payload() -> bytes:
    return struct.pack(
        "<8B4H2IQI", 1, 1, 2, 1, 0, 0, 64, 16,
        0x3C50, 296, 296, 1023, 10000, 20000,
        0x7556160612A31510, 0,
    )


def subreport() -> bytes:
    data = bytearray(292)
    data[0:4] = bytes((1, 0x05, 0, 1))
    data[4:9] = (123).to_bytes(5, "little")
    data[9:14] = (456).to_bytes(5, "little")
    struct.pack_into("<hH", data, 14, -7, 0x1234)
    data[18:21] = (11).to_bytes(3, "little")
    data[21:24] = (12).to_bytes(3, "little")
    data[24:27] = (13).to_bytes(3, "little")
    data[27:30] = (14).to_bytes(3, "little")
    struct.pack_into("<H", data, 30, 127)
    data[32] = 3
    struct.pack_into("<H", data, 33, 720)
    data[35] = 64
    for index in range(64):
        struct.pack_into("<hh", data, 36 + 4 * index, index, -index)
    return bytes(data) + struct.pack("<I", zlib.crc32(data))


def n3_hello_payload() -> bytes:
    return struct.pack(
        "<8B4H2IQI", 1, 1, 3, 1, 0, 0, 64, 16,
        0xC8CF, 296, 592, 1023, 10000, 30000,
        0x7556160612A31510, 0,
    )


def n3_radio_payload(source: int, k: int) -> bytes:
    frame = bytearray(623)
    struct.pack_into("<H", frame, 7, source)
    struct.pack_into("<I", frame, 12, k)
    return (777).to_bytes(5, "little") + bytes((1,)) + struct.pack("<H", len(frame)) + frame


def n5_hello_payload() -> bytes:
    return struct.pack(
        "<8B4H2IQI", 1, 1, 5, 2, 0, 0, 64, 16,
        0x8885, 296, 592, 1023, 3500, 35000,
        0x7556160612A31510, 0,
    )


def n5_radio_payload(source: int, k: int, m: int) -> bytes:
    frame = bytearray(623)
    struct.pack_into("<H", frame, 7, source)
    frame[9:12] = bytes((1, 0, m))
    struct.pack_into("<I", frame, 12, k)
    frame[16:18] = bytes((5, 2))
    struct.pack_into("<H", frame, 18, 0x8885)
    return (777).to_bytes(5, "little") + bytes((1,)) + struct.pack("<H", len(frame)) + frame


class UsbCdcV1Tests(unittest.TestCase):
    def test_incremental_parser_accepts_every_boundary(self):
        raw = encode_record(HELLO, 0, 4, hello_payload())
        for split in range(len(raw) + 1):
            parser = StreamParser()
            records = parser.feed(raw[:split]) + parser.feed(raw[split:])
            self.assertEqual([record.raw for record in records], [raw])

    def test_crc_failure_resynchronizes(self):
        bad = bytearray(encode_record(HEARTBEAT, 0, 0, bytes(12)))
        bad[12] ^= 0x80
        good = encode_record(HEARTBEAT, 0, 1, bytes(12))
        parser = StreamParser()
        records = parser.feed(b"noise" + bad + good)
        self.assertEqual([record.sequence for record in records], [1])
        self.assertEqual(parser.stats.crc_failures, 1)
        self.assertEqual(parser.stats.sequence_gaps, 0)

    def test_corrupt_middle_record_is_not_a_producer_gap(self):
        first = encode_record(HEARTBEAT, 0, 10, bytes(12))
        corrupt = bytearray(encode_record(HEARTBEAT, 0, 11, bytes(12)))
        corrupt[12] ^= 1
        last = encode_record(HEARTBEAT, 0, 12, bytes(12))
        parser = StreamParser()
        records = parser.feed(first + corrupt + last)
        self.assertEqual([record.sequence for record in records], [10, 12])
        self.assertEqual(parser.stats.crc_failures, 1)
        self.assertEqual(parser.stats.sequence_gaps, 0)

    def test_corrupt_sequence_field_cannot_poison_accounting(self):
        first = encode_record(HEARTBEAT, 0, 10, bytes(12))
        corrupt = bytearray(encode_record(HEARTBEAT, 0, 11, bytes(12)))
        corrupt[8] ^= 0x40
        last = encode_record(HEARTBEAT, 0, 12, bytes(12))
        parser = StreamParser()
        records = parser.feed(first + corrupt + last)
        self.assertEqual([record.sequence for record in records], [10, 12])
        self.assertEqual(parser.stats.crc_failures, 1)
        self.assertEqual(parser.stats.sequence_gaps, 0)
        self.assertEqual(parser.stats.duplicates_or_old, 0)

    def test_unknown_type_is_counted_and_ignored(self):
        parser = StreamParser()
        records = parser.feed(encode_record(0x80, 0, 0, b"reserved"))
        self.assertEqual(records, [])
        self.assertEqual(parser.stats.unknown_types, 1)

    def test_sequence_gap_and_duplicate_are_distinct(self):
        parser = StreamParser()
        stream = b"".join(
            encode_record(HEARTBEAT, 0, sequence, bytes(12))
            for sequence in (10, 13, 13)
        )
        self.assertEqual(len(parser.feed(stream)), 3)
        self.assertEqual(parser.stats.sequence_gaps, 2)
        self.assertEqual(parser.stats.duplicates_or_old, 1)

    def test_hello_gates_radio_frame_decode(self):
        frame_record = StreamParser().feed(
            encode_record(RADIO_FRAME, 0, 0, bytes(8))
        )[0]
        with self.assertRaises(ProtocolError):
            decode_record(frame_record, DecoderState())

    def test_local_observation_decodes_crc_and_cir(self):
        state = DecoderState()
        hello = StreamParser().feed(encode_record(HELLO, 0, 0, hello_payload()))[0]
        decode_record(hello, state)
        payload = bytes((0,)) + struct.pack("<I", 19) + subreport()
        record = StreamParser().feed(encode_record(LOCAL_OBS, 0, 1, payload))[0]
        decoded = decode_record(record, state)
        self.assertEqual(decoded.k, 19)
        self.assertEqual(decoded.subreport.cfo_raw, -7)
        self.assertEqual(decoded.subreport.cir_iq[5], (5, -5))

    def test_subreport_crc_is_independent_of_outer_crc(self):
        state = DecoderState()
        hello = StreamParser().feed(encode_record(HELLO, 0, 0, hello_payload()))[0]
        decode_record(hello, state)
        damaged = bytearray(subreport())
        damaged[40] ^= 1
        payload = bytes((0,)) + struct.pack("<I", 20) + damaged
        record = StreamParser().feed(encode_record(LOCAL_OBS, 0, 1, payload))[0]
        with self.assertRaisesRegex(ProtocolError, "subreport CRC32"):
            decode_record(record, state)

    def test_replay_is_byte_boundary_independent(self):
        stream = b"".join(
            encode_record(HEARTBEAT, 0, sequence, bytes(12))
            for sequence in range(20)
        )
        whole = replay_bytes(stream)
        fragmented = replay_bytes(stream, (1, 2, 7, 31))
        self.assertEqual([record.raw for record in fragmented],
                         [record.raw for record in whole])

    def test_capture_inspection_waits_for_hello(self):
        radio = encode_record(RADIO_FRAME, 0, 0, bytes(8))
        hello = encode_record(HELLO, 0, 1, hello_payload())
        result = inspect(radio + hello)
        self.assertEqual(result["prehello_data_skipped"], 1)
        self.assertEqual(result["hello"]["config_hash"], 0x3C50)

    def test_n3_hello_frame_size_and_modulo_ownership(self):
        stream = encode_record(HELLO, 0, 0, n3_hello_payload())
        stream += encode_record(RADIO_FRAME, 0, 1, n3_radio_payload(1, 1))
        stream += encode_record(RADIO_FRAME, 0, 2, n3_radio_payload(2, 2))
        tx_payload = struct.pack("<IB", 3, 0) + (999).to_bytes(5, "little")
        tx_payload += struct.pack("<HB", 625, 1)
        stream += encode_record(TX_RECORD, 0, 3, tx_payload)

        result = inspect(stream)
        self.assertEqual(result["hello"]["n_nodes"], 3)
        self.assertEqual(result["hello"]["m_slots"], 1)
        self.assertEqual(result["hello"]["frame_payload_bytes"], 592)
        self.assertTrue(result["radio_ownership_valid"])
        self.assertTrue(result["tx_ownership_valid"])

    def test_n3_cycle_summary_has_independent_peer_misses(self):
        payload = struct.pack("<II8H", 30, 10, 1, 2, 0, 0, 0, 0, 0, 2500)
        payload += bytes((0, 0, 1, 0, 0, 0, 0, 0, 0, 0))
        record = StreamParser().feed(encode_record(CYCLE_SUMMARY, 0, 0, payload))[0]
        summary = decode_record(record, DecoderState())
        self.assertEqual(summary.frames_expected, 2)
        self.assertEqual(summary.peer_m0_miss, (0, 0, 1, 0, 0, 0, 0, 0))

    def test_n5_m2_ownership_and_missing_node_summary(self):
        stream = encode_record(HELLO, 0, 0, n5_hello_payload())
        sequence = 1
        for source in (1, 2):
            for m in (0, 1):
                stream += encode_record(
                    RADIO_FRAME, 0, sequence, n5_radio_payload(source, source, m)
                )
                sequence += 1
        for m in (0, 1):
            tx_payload = struct.pack("<IB", 5, m) + (1000 + m).to_bytes(5, "little")
            tx_payload += struct.pack("<HB", 625, 1)
            stream += encode_record(TX_RECORD, 0, sequence, tx_payload)
            sequence += 1

        result = inspect(stream)
        self.assertEqual(result["hello"]["n_nodes"], 5)
        self.assertEqual(result["hello"]["m_slots"], 2)
        self.assertTrue(result["radio_ownership_valid"])
        self.assertTrue(result["tx_ownership_valid"])

        payload = struct.pack("<II8H", 5, 1, 4, 8, 0, 0, 0, 0, 0, 2500)
        payload += bytes((0, 0, 0, 1, 1, 0, 0, 0, 0, 0))
        record = StreamParser().feed(
            encode_record(CYCLE_SUMMARY, 0, sequence, payload)
        )[0]
        summary = decode_record(record, DecoderState())
        self.assertEqual((summary.frames_received, summary.frames_expected), (4, 8))
        self.assertEqual(summary.peer_m0_miss, (0, 0, 0, 1, 1, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
