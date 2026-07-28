from pathlib import Path
import gc
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np


RADAR_MAP = Path(__file__).resolve().parents[1] / "host-tools" / "radar-map"
sys.path.insert(0, str(RADAR_MAP))

from radar_map.model import Geometry, GridSpec, LinkProfile, METRES_PER_TAP, QualityConfig  # noqa: E402
from radar_map.capture import decode_capture  # noqa: E402
from radar_map.processing import backproject, build_link_profiles  # noqa: E402
from radar_map.server import slice_payload  # noqa: E402
from radar_map.storage import export_volume, load_volume  # noqa: E402
from test_h3_ingest import hello_payload, sample_stream  # noqa: E402
from unoq.heimdall.protocol import HELLO, encode_record  # noqa: E402


def observation(source: int, receiver: int, sequence: int, cir: np.ndarray, false_path=False):
    fp = 668.0 if false_path else 740.0
    start = 652 if false_path else 724
    values = np.column_stack((np.real(cir), np.imag(cir))).astype("<i2").ravel()
    return SimpleNamespace(
        observed_node_id=source,
        reporting_node_id=receiver,
        observed_k=sequence,
        usb_sequence=sequence,
        obs_flags=0x05,
        accum_count=108,
        dgc_decision=3,
        fp_index_q10_6=round(fp * 64),
        cir_start_offset=start,
        cir_taps=len(cir),
        cir_blob=struct.pack(f"<{len(values)}h", *values),
    )


class QualityGateTests(unittest.TestCase):
    def test_false_first_path_is_rejected_as_distinct_condition(self):
        geometry = Geometry({0: np.array([0.0, 0.0, 0.0]), 1: np.array([2.0, 0.0, 0.0])})
        normal = np.zeros(64, dtype=np.complex128)
        normal[16] = 10_000 + 2_000j
        normal[24] = 2_000 - 500j
        anomaly = np.random.default_rng(7).normal(0, 1, 64) + 1j * np.random.default_rng(8).normal(0, 1, 64)
        anomaly *= np.linalg.norm(normal) / np.linalg.norm(anomaly)
        observations = [observation(0, 1, 0, anomaly, false_path=True)]
        observations.extend(observation(0, 1, index + 1, normal) for index in range(10))

        profiles, stats = build_link_profiles(
            observations, geometry, quality=QualityConfig(), clutter_frames=0
        )

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].accepted_frames, 10)
        self.assertEqual(stats["rejected"]["false_first_path"], 1)
        self.assertEqual(stats["links"][0]["false_first_path"], 1)


class CaptureTests(unittest.TestCase):
    def test_husb_decode_feeds_backprojection_and_records_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            capture = Path(temporary) / "fixture.husb"
            capture.write_bytes(sample_stream())
            observations, stats = decode_capture(capture, chunk_bytes=7)
        geometry = Geometry({0: np.array([0.0, 0.0, 0.0]), 1: np.array([2.0, 0.0, 0.0])})
        profiles, _ = build_link_profiles(observations, geometry, clutter_frames=0)
        result = backproject(profiles, geometry, GridSpec((0.0, 0.0, 0.0), (2.0, 1.0, 1.0), 0.5))
        self.assertEqual(stats["canonical_observations"], 2)
        self.assertEqual(stats["hello"]["config_hash"], 0x3C50)
        self.assertEqual(len(stats["capture_sha256"]), 64)
        self.assertEqual(len(profiles), 2)
        self.assertEqual(result.volume.shape, (3, 3, 5))

    def test_changed_hello_configuration_is_not_silently_combined(self):
        changed = bytearray(hello_payload())
        changed[2] = 3
        stream = sample_stream() + encode_record(HELLO, 0, 103, bytes(changed))
        with tempfile.TemporaryDirectory() as temporary:
            capture = Path(temporary) / "mixed.husb"
            capture.write_bytes(stream)
            with self.assertRaisesRegex(ValueError, "multiple HELLO configurations"):
                decode_capture(capture)

    def test_same_configuration_usb_restart_is_not_silently_combined(self):
        stream = sample_stream() + encode_record(HELLO, 0, 1, hello_payload())
        with tempfile.TemporaryDirectory() as temporary:
            capture = Path(temporary) / "restarted.husb"
            capture.write_bytes(stream)
            with self.assertRaisesRegex(ValueError, "USB sequence restart"):
                decode_capture(capture)


class BackprojectionTests(unittest.TestCase):
    def test_synthetic_reflector_localizes_in_three_dimensions(self):
        positions = {
            0: np.array([0.0, 0.0, 0.0]),
            1: np.array([2.0, 0.0, 0.0]),
            2: np.array([0.0, 2.0, 0.0]),
            3: np.array([2.0, 2.0, 0.0]),
        }
        target = np.array([0.8, 1.2, 0.8])
        tap_axis = np.arange(0.0, 30.0, 0.05)
        profiles = []
        for source in positions:
            for receiver in positions:
                if source == receiver:
                    continue
                direct = np.linalg.norm(positions[source] - positions[receiver])
                excess = (
                    np.linalg.norm(target - positions[source])
                    + np.linalg.norm(target - positions[receiver])
                    - direct
                )
                target_tap = excess / METRES_PER_TAP
                magnitude = np.exp(-0.5 * ((tap_axis - target_tap) / 0.12) ** 2)
                profiles.append(LinkProfile(source, receiver, tap_axis, magnitude, 20, 1.0))
        result = backproject(
            profiles,
            Geometry(positions, revision="synthetic"),
            GridSpec((0.0, 0.0, 0.0), (2.0, 2.0, 1.6), 0.1),
        )
        maximum = np.unravel_index(np.argmax(result.volume), result.volume.shape)
        estimate = np.array([result.x_m[maximum[2]], result.y_m[maximum[1]], result.z_m[maximum[0]]])
        np.testing.assert_allclose(estimate, target, atol=0.11)

    def test_export_round_trip_and_slice_axis_order(self):
        profile = LinkProfile(0, 1, np.arange(10.0), np.arange(10.0), 4, 1.0)
        geometry = Geometry({0: np.array([0.0, 0.0, 0.0]), 1: np.array([1.0, 0.0, 0.0])})
        result = backproject([profile], geometry, GridSpec((0.0, 0.0, 0.0), (1.0, 1.0, 1.0), 0.5))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            export_volume(result, output, {"fixture": "synthetic"})
            volume, confidence, metadata = load_volume(output)
            payload = slice_payload(volume, confidence, metadata, "xz", 1)
            self.assertEqual(payload["array_order"], ["z", "x"])
            self.assertEqual(payload["shape"], [3, 3])
            np.testing.assert_array_equal(volume[:, 1, :], payload["values"])
            volume._mmap.close()
            confidence._mmap.close()
            del volume, confidence
            gc.collect()


if __name__ == "__main__":
    unittest.main()
