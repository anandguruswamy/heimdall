from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
import zlib

from unoq.heimdall.archive import RawArchive, iter_archive_bytes
from unoq.heimdall.canonical import CanonicalProcessor
from unoq.heimdall.ingest import IngestSession, replay_archive
from unoq.heimdall.protocol import (
    HELLO,
    LOCAL_OBS,
    ProtocolError,
    RADIO_FRAME,
    StreamParser,
    encode_record,
)
from unoq.heimdall.storage import HeimdallStorage
from unoq.heimdall.verify_h3 import verify


def hello_payload(m_slots: int = 1, frame_payload_bytes: int = 296) -> bytes:
    return struct.pack(
        "<8B4H2IQI",
        1, 1, 2, m_slots, 0, 0, 64, 16,
        0x3C50, 296, frame_payload_bytes, 1023, 10000, 20000,
        0x7556160612A31510, 0,
    )


def subreport(observed_node_id: int, round_delta: int = 1, taps: int = 64) -> bytes:
    data = bytearray(36 + 4 * taps)
    data[0:4] = bytes((observed_node_id, 0x05, 0, round_delta))
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
    data[35] = taps
    for index in range(taps):
        struct.pack_into("<hh", data, 36 + 4 * index, index, -index)
    return bytes(data) + struct.pack("<I", zlib.crc32(data))


def beacon_frame(
    k: int = 19,
    m: int = 0,
    m_slots: int = 1,
    frame_payload_bytes: int = 296,
) -> bytes:
    report = subreport(0)
    header = bytearray(31)
    struct.pack_into("<H", header, 0, 0x8841)
    header[2] = k & 0xFF
    struct.pack_into("<H", header, 3, 0xBEEF)
    struct.pack_into("<H", header, 5, 0xFFFF)
    struct.pack_into("<H", header, 7, 1)
    header[9:12] = bytes((1, 0, m))
    struct.pack_into("<I", header, 12, k)
    header[16:18] = bytes((2, m_slots))
    struct.pack_into("<H", header, 18, 0x3C50)
    header[20:25] = (999).to_bytes(5, "little")
    header[25] = 1
    struct.pack_into("<H", header, 26, len(report))
    header[28] = 1
    start = m * frame_payload_bytes
    fragment = report[start:start + frame_payload_bytes]
    return bytes(header) + fragment.ljust(frame_payload_bytes, b"\x00")


def sample_stream() -> bytes:
    hello = encode_record(HELLO, 0, 100, hello_payload())
    local_payload = bytes((0,)) + struct.pack("<I", 18) + subreport(1)
    local = encode_record(LOCAL_OBS, 0, 101, local_payload)
    frame = beacon_frame()
    radio_payload = (777).to_bytes(5, "little") + bytes((1,))
    radio_payload += struct.pack("<H", len(frame)) + frame
    radio = encode_record(RADIO_FRAME, 0, 102, radio_payload)
    return hello + local + radio


def n3_hello_payload() -> bytes:
    return struct.pack(
        "<8B4H2IQI",
        1, 1, 3, 1, 0, 0, 64, 16,
        0xC8CF, 296, 592, 1023, 10000, 30000,
        0x7556160612A31510, 0,
    )


def n3_beacon_frame(source: int, k: int, reports: list[bytes]) -> bytes:
    pooled = b"".join(reports)
    header = bytearray(31)
    struct.pack_into("<H", header, 0, 0x8841)
    header[2] = k & 0xFF
    struct.pack_into("<H", header, 3, 0xABCD)
    struct.pack_into("<H", header, 5, 0xFFFF)
    struct.pack_into("<H", header, 7, source)
    header[9:12] = bytes((1, 0, 0))
    struct.pack_into("<I", header, 12, k)
    header[16:18] = bytes((3, 1))
    struct.pack_into("<H", header, 18, 0xC8CF)
    header[20:25] = (1000 + k).to_bytes(5, "little")
    header[25] = len(reports)
    struct.pack_into("<H", header, 26, len(pooled))
    header[28] = sum(1 << report[0] for report in reports)
    return bytes(header) + pooled.ljust(592, b"\x00")


class CanonicalTests(unittest.TestCase):
    def test_local_and_relayed_observations_have_one_shape(self):
        records = StreamParser().feed(sample_stream())
        processor = CanonicalProcessor()
        outputs = [processor.process(record) for record in records]
        local = outputs[1].observations[0]
        relayed = outputs[2].observations[0]
        self.assertEqual(local.route, "local")
        self.assertEqual((local.reporting_node_id, local.observed_node_id), (0, 1))
        self.assertEqual(relayed.route, "relayed")
        self.assertEqual((relayed.reporting_node_id, relayed.observed_node_id), (1, 0))
        self.assertEqual(relayed.observed_k, 18)
        self.assertEqual(relayed.report_k, 19)
        self.assertEqual(len(relayed.cir_blob), 64 * 4)

    def test_two_fragment_report_reassembles_out_of_order(self):
        parser = StreamParser()
        processor = CanonicalProcessor()
        hello = encode_record(HELLO, 0, 0, hello_payload(2, 148))
        processor.process(parser.feed(hello)[0])
        outputs = []
        for sequence, m in ((1, 1), (2, 0)):
            frame = beacon_frame(m=m, m_slots=2, frame_payload_bytes=148)
            payload = (777).to_bytes(5, "little") + bytes((1,))
            payload += struct.pack("<H", len(frame)) + frame
            record = parser.feed(encode_record(RADIO_FRAME, 0, sequence, payload))[0]
            outputs.append(processor.process(record))
        self.assertEqual(outputs[0].observations, ())
        self.assertEqual(len(outputs[1].observations), 1)
        self.assertEqual(outputs[1].observations[0].observed_k, 18)

    def test_n3_complete_cycle_yields_six_directed_observations(self):
        parser = StreamParser()
        processor = CanonicalProcessor()
        stream = [encode_record(HELLO, 0, 0, n3_hello_payload())]
        stream.append(encode_record(
            LOCAL_OBS, 0, 1, bytes((0,)) + struct.pack("<I", 1) + subreport(1, 2)
        ))
        stream.append(encode_record(
            LOCAL_OBS, 0, 2, bytes((0,)) + struct.pack("<I", 2) + subreport(2, 1)
        ))
        frames = (
            n3_beacon_frame(1, 1, [subreport(2, 2), subreport(0, 1)]),
            n3_beacon_frame(2, 2, [subreport(1, 1), subreport(0, 2)]),
        )
        for sequence, frame in enumerate(frames, 3):
            payload = (777).to_bytes(5, "little") + bytes((1,))
            payload += struct.pack("<H", len(frame)) + frame
            stream.append(encode_record(RADIO_FRAME, 0, sequence, payload))

        observations = []
        for raw in stream:
            record = parser.feed(raw)[0]
            observations.extend(processor.process(record).observations)

        self.assertEqual(len(frames[0]), 623)  # Hardware FCS makes 625 on air.
        self.assertEqual(len(observations), 6)
        self.assertEqual(
            {(item.reporting_node_id, item.observed_node_id) for item in observations},
            {(0, 1), (0, 2), (1, 0), (1, 2), (2, 0), (2, 1)},
        )
        self.assertEqual(
            {(item.reporting_node_id, item.observed_node_id, item.round_delta)
             for item in observations if item.route == "relayed"},
            {(1, 0, 1), (1, 2, 2), (2, 0, 2), (2, 1, 1)},
        )

    def test_boundary_truncated_subreport_remains_self_describing(self):
        parser = StreamParser()
        processor = CanonicalProcessor()
        processor.process(parser.feed(
            encode_record(HELLO, 0, 0, n3_hello_payload())
        )[0])
        reports = [subreport(2, 2, 61), subreport(0, 1)]
        frame = n3_beacon_frame(1, 1, reports)
        payload = (777).to_bytes(5, "little") + bytes((1,))
        payload += struct.pack("<H", len(frame)) + frame
        output = processor.process(parser.feed(
            encode_record(RADIO_FRAME, 0, 1, payload)
        )[0])
        self.assertEqual([item.cir_taps for item in output.observations], [61, 64])

    def test_local_subreport_cannot_exceed_hello_tap_ceiling(self):
        parser = StreamParser()
        processor = CanonicalProcessor()
        processor.process(parser.feed(
            encode_record(HELLO, 0, 0, n3_hello_payload())
        )[0])
        payload = bytes((0,)) + struct.pack("<I", 1) + subreport(1, 2, 65)
        record = parser.feed(encode_record(LOCAL_OBS, 0, 1, payload))[0]
        with self.assertRaisesRegex(ProtocolError, "dimensions exceed HELLO"):
            processor.process(record)


class ArchiveTests(unittest.TestCase):
    def test_rotation_preserves_every_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = RawArchive(directory, rotate_bytes=7, sync_interval_seconds=0)
            archive.write(b"abc")
            archive.write(b"defghijklmnop")
            segments = archive.close()
            self.assertEqual([item.byte_count for item in segments], [7, 7, 2])
            self.assertEqual(b"".join(iter_archive_bytes(directory, 3)), b"abcdefghijklmnop")
            self.assertTrue(all(len(item.sha256) == 64 for item in segments))


class PersistenceTests(unittest.TestCase):
    def test_archive_replay_produces_identical_observations(self):
        stream = b"noise" + sample_stream()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = HeimdallStorage(root / "live.sqlite3")
            live_run = live.start_run({"mode": "test-live"})
            session = IngestSession(live, live_run, "test-device", root / "raw", 113)
            for offset in range(0, len(stream), 17):
                session.feed(stream[offset:offset + 17])
            stats = session.close()
            live.close_run(live_run)
            fingerprints = live.observation_fingerprints(live_run)
            live.close()

            self.assertEqual(stats["records"], 3)
            self.assertEqual(len(fingerprints), 2)
            archive_directory = root / "raw" / "connection-000001"
            self.assertEqual(b"".join(iter_archive_bytes(archive_directory)), stream)

            replay = HeimdallStorage(root / "replay.sqlite3")
            replay_run = replay.start_run({"mode": "replay"})
            replay_stats = replay_archive(archive_directory, replay, replay_run, 23)
            replay.close_run(replay_run)
            self.assertEqual(replay_stats["records"], 3)
            self.assertEqual(replay.observation_fingerprints(replay_run), fingerprints)
            replay.close()
            verification = verify(
                root / "live.sqlite3", root / "raw", root / "replay.sqlite3"
            )
            self.assertTrue(verification["equivalent"])

    def test_rejected_record_is_durable_and_new_connection_recovers(self):
        valid_hello = encode_record(HELLO, 0, 0, hello_payload())
        bad_payload = bytes((0,)) + struct.pack("<I", 18) + subreport(1)[:-1] + b"\x00"
        rejected = encode_record(LOCAL_OBS, 0, 1, bad_payload)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = HeimdallStorage(root / "events.sqlite3")
            run_id = storage.start_run({"mode": "restart-test"})
            first = IngestSession(storage, run_id, "device", root / "raw")
            first.feed(valid_hello + rejected)
            first_stats = first.close("disconnected")
            second = IngestSession(storage, run_id, "device", root / "raw")
            second.feed(valid_hello)
            second.close()
            storage.close_run(run_id)

            self.assertEqual(first_stats["records_rejected"], 1)
            rows = storage.db.execute(
                "SELECT decode_status,decode_error FROM usb_records ORDER BY id"
            ).fetchall()
            self.assertEqual([row[0] for row in rows], ["valid", "rejected", "valid"])
            self.assertIn("subreport CRC32", rows[1][1])
            connections = storage.db.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
            self.assertEqual(connections, 2)
            storage.close()

    def test_unclean_exit_recovers_epoch_and_segment_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "events.sqlite3"
            storage = HeimdallStorage(database)
            run_id = storage.start_run({"mode": "crash-test"})
            session = IngestSession(storage, run_id, "device", root / "raw")
            session.feed(encode_record(HELLO, 0, 0, hello_payload()))
            assert session.archive is not None
            session.archive.close()
            storage.close()

            recovered = HeimdallStorage(database)
            result = recovered.recover_interrupted(root / "raw")
            self.assertEqual(result, {"connections": 1, "segments": 1})
            self.assertEqual(
                recovered.db.execute("SELECT status FROM runs").fetchone()[0],
                "interrupted",
            )
            self.assertEqual(
                recovered.db.execute("SELECT status FROM connections").fetchone()[0],
                "interrupted",
            )
            segment = recovered.db.execute(
                "SELECT byte_count,length(sha256) FROM raw_segments"
            ).fetchone()
            self.assertEqual(segment, (len(encode_record(HELLO, 0, 0, hello_payload())), 64))
            recovered.close()


if __name__ == "__main__":
    unittest.main()
