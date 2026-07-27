"""Regression tests for the beacon configuration model.

Run from the repository root:

    python -m unittest discover -s tests -v

These tests guard the sizing model in ``tools/config/heimdall_config.py``, which
is the reference implementation the firmware build uses to verify the browser
configuration tool's arithmetic. See ``contracts/beacon-v1.md`` sections 7, 10,
and 13.
"""

from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools" / "config"))

import heimdall_config as hc  # noqa: E402

EXAMPLE = REPO_ROOT / "deployment" / "beacon-config.example.json"
N3_CONFIG = REPO_ROOT / "deployment" / "beacon-config.n3.json"
N5_CONFIG = REPO_ROOT / "deployment" / "beacon-config.n5.json"


def load_example() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


class WireFormatConstants(unittest.TestCase):
    """These constants are normative in contracts/beacon-v1.md."""

    def test_header_is_31_bytes(self):
        self.assertEqual(hc.MAC_HEADER_BYTES, 9)
        self.assertEqual(hc.HEIMDALL_HEADER_BYTES, 22)
        self.assertEqual(hc.FRAME_HEADER_BYTES, 31)

    def test_subreport_metadata_is_40_bytes(self):
        self.assertEqual(hc.SUBREPORT_METADATA_BYTES, 40)
        self.assertEqual(hc.BYTES_PER_TAP, 4)

    def test_encoding_ceilings(self):
        self.assertEqual(hc.MAX_NODES, 8)
        self.assertEqual(hc.MAX_CIR_TAPS, 128)
        self.assertEqual(hc.MAX_FRAME_BYTES_LIMIT, 1023)

    def test_subreport_size_matches_contract_formula(self):
        for taps in (1, 32, 64, 128):
            expected = 40 + 4 * taps
            cfg = load_example()
            cfg["cir"]["taps"] = taps
            cfg["cir"]["left_taps"] = min(cfg["cir"]["left_taps"], taps - 1)
            self.assertEqual(hc.derive(cfg)["derived"]["subreport_bytes"], expected)

    def test_max_taps_subreport_still_fits_one_frame(self):
        """A subreport must never be forced to span frames by its own size."""
        biggest = hc.SUBREPORT_METADATA_BYTES + hc.BYTES_PER_TAP * hc.MAX_CIR_TAPS
        capacity = hc.MAX_FRAME_BYTES_LIMIT - hc.FRAME_HEADER_BYTES - hc.FCS_BYTES
        self.assertLessEqual(biggest, capacity, "552 B subreport must fit 990 B capacity")


class AirtimeModel(unittest.TestCase):
    def test_reed_solomon_block_expansion(self):
        self.assertEqual(hc.RS_DATA_BITS, 330)
        self.assertEqual(hc.RS_CODED_BITS, 378)

    def test_nominal_rate_is_already_net_of_rs_coding(self):
        """6.8 Mb/s is the post-RS net rate, not the coded symbol rate.

        The coded symbol rate is 1/128.21 ns = 7.8 Mb/s, and
        7.8 * (330/378) = 6.81 Mb/s. So dividing bytes by 6.8 Mb/s approximates
        the data field well; it does not double-count parity. This test exists
        because the opposite was asserted during design review and propagated
        into three documents before it was caught.
        """
        coded_rate_mbps = 1000.0 / hc.DATA_SYMBOL_NS[6800]
        self.assertAlmostEqual(coded_rate_mbps, 7.8, places=1)
        self.assertAlmostEqual(coded_rate_mbps * hc.RS_DATA_BITS / hc.RS_CODED_BITS, 6.81, places=2)

    def test_block_model_differs_from_naive_only_by_final_block_padding(self):
        psdu = 1023
        block_data_us = math.ceil(psdu * 8 / hc.RS_DATA_BITS) * hc.RS_CODED_BITS * hc.DATA_SYMBOL_NS[6800] / 1000.0
        naive_data_us = psdu * 8 / 6800.0 * 1000.0
        self.assertLess(abs(block_data_us / naive_data_us - 1.0), 0.01)

    def test_omitting_shr_and_phr_is_the_dominant_naive_error(self):
        """SHR + PHR is ~160 us regardless of frame size, so it dominates."""
        shr_us = (128 + 8) * hc.PREAMBLE_SYMBOL_NS[64] / 1000.0
        phr_us = hc.PHR_SYMBOLS * hc.PHR_BASE_SYMBOL_NS / 1000.0
        self.assertAlmostEqual(shr_us, 138.4, places=1)
        self.assertAlmostEqual(phr_us, 21.5, places=1)

        overhead = shr_us + phr_us
        big = hc.frame_airtime_us(1023, 128, 64, 1, 6800, "std")
        small = hc.frame_airtime_us(329, 128, 64, 1, 6800, "std")
        self.assertGreater(overhead / big, 0.10)
        self.assertLess(overhead / big, 0.14)
        self.assertGreater(overhead / small, 0.25)
        self.assertLess(overhead / small, 0.32)

    def test_lower_preamble_saves_expected_airtime(self):
        long_p = hc.frame_airtime_us(773, 128, 64, 1, 6800, "std")
        short_p = hc.frame_airtime_us(773, 64, 64, 1, 6800, "std")
        self.assertAlmostEqual(long_p - short_p, 64 * hc.PREAMBLE_SYMBOL_NS[64] / 1000.0, places=2)

    def test_850kbps_is_eight_times_slower_in_data(self):
        fast = hc.frame_airtime_us(1023, 128, 64, 1, 6800, "std")
        slow = hc.frame_airtime_us(1023, 128, 64, 1, 850, "std")
        self.assertGreater(slow, fast * 6)

    def test_rejects_unsupported_phy_values(self):
        with self.assertRaises(hc.ConfigError):
            hc.frame_airtime_us(100, 128, 32, 1, 6800, "std")
        with self.assertRaises(hc.ConfigError):
            hc.frame_airtime_us(100, 128, 64, 1, 110, "std")
        with self.assertRaises(hc.ConfigError):
            hc.frame_airtime_us(100, 128, 64, 9, 6800, "std")


class BalancedPacking(unittest.TestCase):
    def test_balanced_beats_greedy_on_max_frame_size(self):
        """Item 18: balancing shrinks the largest frame, hence the slot."""
        cfg = load_example()
        cfg["network"]["n_nodes"] = 6
        der = hc.derive(cfg)["derived"]

        greedy_max_payload = min(der["pooled_report_max_bytes"], der["frame_capacity_max_bytes"])
        self.assertLess(der["frame_payload_bytes"], greedy_max_payload)
        self.assertEqual(der["frame_payload_bytes"], 740)
        self.assertEqual(greedy_max_payload, 990)

    def test_all_frames_can_hold_the_report(self):
        for n in range(2, hc.MAX_NODES + 1):
            for taps in (32, 64, 128):
                cfg = load_example()
                cfg["network"]["n_nodes"] = n
                cfg["cir"]["taps"] = taps
                der = hc.derive(cfg)["derived"]
                self.assertGreaterEqual(
                    der["m_slots_per_superslot"] * der["frame_payload_bytes"],
                    der["pooled_report_max_bytes"],
                    f"N={n} taps={taps}",
                )
                self.assertLessEqual(der["frame_bytes"], cfg["framing"]["max_frame_bytes"])

    def test_slot_floor_quantised_to_tenth_millisecond(self):
        for n in range(2, hc.MAX_NODES + 1):
            cfg = load_example()
            cfg["network"]["n_nodes"] = n
            der = hc.derive(cfg)["derived"]
            self.assertEqual(der["slot_floor_us"] % hc.SLOT_QUANTUM_US, 0, f"N={n}")
            self.assertGreaterEqual(
                der["slot_floor_us"],
                der["frame_airtime_us"] + der["rx_processing_us"],
                f"N={n}",
            )

    def test_slot_floor_is_not_monotonic_in_n(self):
        """N=5 has a shorter slot than N=4 because M increments and rebalances."""
        floors = {}
        for n in (4, 5):
            cfg = load_example()
            cfg["network"]["n_nodes"] = n
            floors[n] = hc.derive(cfg)["derived"]["slot_floor_us"]
        self.assertLess(floors[5], floors[4])


class ReportAssemblyConstraint(unittest.TestCase):
    """contracts/beacon-v1.md section 13.2.

    Node i's report must contain U(i, i-1), measured from the peer whose
    superslot immediately precedes its own. The read-out, assembly, and TX
    buffer write must all fit in M * T_slot - airtime. TX preparation cannot be
    pipelined into the preceding frame's airtime, because the bytes being
    written include the measurement of that very frame.

    This constraint was missing from the original model and made M=1
    configurations optimistic. These tests exist so it cannot regress.
    """

    def test_floor_is_the_max_of_both_constraints(self):
        for n in range(2, hc.MAX_NODES + 1):
            cfg = load_example()
            cfg["network"]["n_nodes"] = n
            der = hc.derive(cfg)["derived"]
            self.assertEqual(
                der["slot_floor_us"],
                max(der["slot_floor_rx_us"], der["slot_floor_assembly_us"]),
                f"N={n}",
            )

    def test_assembly_binds_exactly_when_m_is_one(self):
        for n in range(2, hc.MAX_NODES + 1):
            cfg = load_example()
            cfg["network"]["n_nodes"] = n
            der = hc.derive(cfg)["derived"]
            expected = "assembly" if der["m_slots_per_superslot"] == 1 else "reception"
            self.assertEqual(der["slot_floor_binding_constraint"], expected, f"N={n}")

    def test_assembly_window_actually_fits_at_the_floor(self):
        """The whole chain must fit in M * T_slot - airtime, with margin."""
        for n in range(2, hc.MAX_NODES + 1):
            for taps in (32, 64, 128):
                cfg = load_example()
                cfg["network"]["n_nodes"] = n
                cfg["cir"]["taps"] = taps
                der = hc.derive(cfg)["derived"]
                cfg["timing"]["slot_duration_us"] = der["slot_floor_us"]
                der = hc.derive(cfg)["derived"]

                window = der["m_slots_per_superslot"] * der["slot_floor_us"] - der["frame_airtime_us"]
                work = cfg["budget"]["processing_margin_factor"] * (
                    der["rx_processing_us"] + cfg["budget"]["report_assembly_us"] + der["tx_write_us"]
                )
                self.assertGreaterEqual(window, work, f"N={n} taps={taps}")

    def test_tx_write_scales_with_frame_size(self):
        cfg = load_example()
        cfg["network"]["n_nodes"] = 4
        der = hc.derive(cfg)["derived"]
        expected = der["frame_bytes"] * 8.0 / cfg["budget"]["spi_hz"] * 1e6
        expected += cfg["budget"]["spi_transaction_overhead_us"]
        self.assertAlmostEqual(der["tx_write_us"], expected, places=2)

    def test_ignoring_tx_path_would_have_been_optimistic(self):
        """Quantifies the bug: N=4 was short by 400 us before the fix."""
        cfg = load_example()
        cfg["network"]["n_nodes"] = 4
        cfg["budget"]["report_assembly_us"] = 20.0
        der = hc.derive(cfg)["derived"]
        self.assertGreater(der["slot_floor_assembly_us"], der["slot_floor_rx_us"])
        self.assertEqual(der["slot_floor_assembly_us"] - der["slot_floor_rx_us"], 400)

    def test_crc32_is_included_in_rx_processing(self):
        """Section 9 puts the subreport CRC32 in the observing node's callback."""
        cfg = load_example()
        slow = copy.deepcopy(cfg)
        slow["budget"]["crc32_bytes_per_us"] = 1.0
        delta = hc.derive(slow)["derived"]["rx_processing_us"] - hc.derive(cfg)["derived"]["rx_processing_us"]
        protected = hc.derive(cfg)["derived"]["subreport_bytes"] - hc.SUBREPORT_CRC_BYTES
        baseline = cfg["budget"]["crc32_bytes_per_us"]
        self.assertAlmostEqual(delta, protected / 1.0 - protected / baseline, places=2)


class ConfigHash(unittest.TestCase):
    def test_packed_struct_is_39_bytes(self):
        cfg = hc.derive(load_example())
        self.assertEqual(len(hc.hashed_parameter_struct(cfg)), 39)

    def test_crc16_ccitt_false_known_vector(self):
        self.assertEqual(hc.crc16_ccitt_false(b"123456789"), 0x29B1)

    def test_hash_is_stable(self):
        first = hc.derive(load_example())["derived"]["config_hash"]
        second = hc.derive(load_example())["derived"]["config_hash"]
        self.assertEqual(first, second)

    def test_hash_changes_for_every_hashed_parameter(self):
        base = hc.derive(load_example())["derived"]["config_hash"]
        mutations = [
            ("network", "n_nodes", 4),
            ("network", "network_id", 1234),
            ("network", "master_node_id", 1),
            ("network", "evidence_age_threshold", 5),
            ("phy", "channel", 5),
            ("phy", "tx_preamble_code", 10),
            ("phy", "rx_preamble_code", 11),
            ("phy", "preamble_length", 64),
            ("phy", "pac", 16),
            ("phy", "sfd_type", 2),
            ("phy", "sfd_timeout", 65),
            ("phy", "phr_rate", "dta"),
            ("phy", "tx_pg_delay", 51),
            ("phy", "tx_power", 1),
            ("cir", "taps", 32),
            ("cir", "left_taps", 8),
            ("framing", "max_frame_bytes", 512),
            ("framing", "enable_frame_filter", False),
            ("timing", "slot_duration_us", 10100),
        ]
        for section, key, value in mutations:
            cfg = load_example()
            cfg[section][key] = value
            mutated = hc.derive(cfg)["derived"]["config_hash"]
            self.assertNotEqual(base, mutated, f"{section}.{key} must affect config_hash")

    def test_hash_ignores_non_interoperability_parameters(self):
        """Budget and model parameters must not change the on-air hash."""
        base = hc.derive(load_example())["derived"]["config_hash"]
        for key, value in [
            ("usb_budget_bytes_per_s", 999_999),
            ("processing_margin_factor", 2.0),
            ("spi_hz", 8_000_000),
            ("rx_fixed_overhead_us", 200.0),
            ("crc32_bytes_per_us", 2.0),
            ("report_assembly_us", 90.0),
        ]:
            cfg = load_example()
            cfg["budget"][key] = value
            self.assertEqual(base, hc.derive(cfg)["derived"]["config_hash"], f"budget.{key}")

    def test_hash_ignores_text_formatting(self):
        cfg = load_example()
        reordered = {k: cfg[k] for k in reversed(list(cfg))}
        self.assertEqual(
            hc.derive(cfg)["derived"]["config_hash"],
            hc.derive(reordered)["derived"]["config_hash"],
        )


class ExampleConfiguration(unittest.TestCase):
    def test_example_verifies_clean(self):
        cfg = load_example()
        _, problems = hc.verify(cfg)
        self.assertEqual(problems, [], f"shipped example must verify: {problems}")

    def test_example_matches_current_hardware(self):
        cfg = load_example()
        self.assertEqual(cfg["network"]["n_nodes"], 2, "two verified boards")
        self.assertEqual(cfg["phy"]["channel"], 9)
        self.assertEqual(cfg["phy"]["preamble_length"], 128)
        self.assertEqual(cfg["phy"]["data_rate_kbps"], 6800)
        self.assertEqual(cfg["phy"]["phr_mode"], "ext", "frames above 127 B need EXT")

    def test_example_uses_measured_processing_rates(self):
        cfg = load_example()
        self.assertEqual(cfg["budget"]["crc32_bytes_per_us"], 1.92)
        self.assertEqual(cfg["budget"]["report_assembly_us"], 122.0)

    def test_gate_h4_n3_profile_verifies_clean(self):
        cfg = json.loads(N3_CONFIG.read_text(encoding="utf-8"))
        _, problems = hc.verify(cfg)
        self.assertEqual(problems, [], f"Gate H4 profile must verify: {problems}")
        self.assertEqual(cfg["network"]["n_nodes"], 3)
        self.assertEqual(cfg["timing"]["slot_duration_us"], 10_000)
        self.assertEqual(cfg["derived"]["m_slots_per_superslot"], 1)
        self.assertEqual(cfg["derived"]["superslot_us"], 10_000)
        self.assertEqual(cfg["derived"]["cycle_us"], 30_000)
        self.assertEqual(cfg["derived"]["pooled_report_max_bytes"], 592)
        self.assertEqual(cfg["derived"]["frame_bytes"], 625)
        self.assertEqual(cfg["derived"]["config_hash"], 0xC8CF)

    def test_n5_m2_profile_verifies_clean(self):
        cfg = json.loads(N5_CONFIG.read_text(encoding="utf-8"))
        _, problems = hc.verify(cfg)
        self.assertEqual(problems, [], f"N=5 profile must verify: {problems}")
        self.assertEqual(cfg["network"]["n_nodes"], 5)
        self.assertEqual(cfg["timing"]["slot_duration_us"], 3_500)
        self.assertEqual(cfg["derived"]["m_slots_per_superslot"], 2)
        self.assertEqual(cfg["derived"]["pooled_report_max_bytes"], 1184)
        self.assertEqual(cfg["derived"]["frame_payload_bytes"], 592)
        self.assertEqual(cfg["derived"]["frame_bytes"], 625)
        self.assertEqual(cfg["derived"]["cycle_us"], 35_000)
        self.assertEqual(cfg["derived"]["per_link_rate_hz"], 28.571)
        self.assertEqual(cfg["derived"]["gateway_usb_bytes_per_s"], 187_200)
        self.assertEqual(cfg["derived"]["config_hash"], 0x8885)

    def test_header_emission_contains_every_flashed_constant(self):
        header = hc.emit_header(hc.derive(load_example()))
        for name in (
            "HEIMDALL_PROTOCOL_VERSION",
            "HEIMDALL_N_NODES",
            "HEIMDALL_MASTER_NODE_ID",
            "HEIMDALL_CONFIG_HASH",
            "HEIMDALL_M",
            "HEIMDALL_SLOT_DURATION_US",
            "HEIMDALL_SLOT_FLOOR_US",
            "HEIMDALL_FRAME_PAYLOAD_BYTES",
            "HEIMDALL_FRAME_BYTES",
            "HEIMDALL_SUBREPORT_BYTES",
            "HEIMDALL_CIR_TAPS",
            "HEIMDALL_CIR_CHUNKS",
        ):
            self.assertIn(name, header)
        self.assertIn("#ifndef HEIMDALL_BEACON_CONFIG_H", header)


class VerifierRejectsBadConfigurations(unittest.TestCase):
    """The build depends on these failing. See docs/protocol-decisions.md item 27."""

    def _problems(self, mutate) -> list[str]:
        cfg = hc.derive(load_example())
        cfg = copy.deepcopy(cfg)
        mutate(cfg)
        _, problems = hc.verify(cfg)
        return problems

    def test_tampered_derived_value_is_caught(self):
        problems = self._problems(lambda c: c["derived"].__setitem__("frame_payload_bytes", 300))
        self.assertTrue(any("frame_payload_bytes" in p for p in problems), problems)

    def test_tampered_hash_is_caught(self):
        problems = self._problems(lambda c: c["derived"].__setitem__("config_hash", 0))
        self.assertTrue(any("config_hash" in p for p in problems), problems)

    def test_slot_below_floor_is_caught(self):
        problems = self._problems(lambda c: c["timing"].__setitem__("slot_duration_us", 500))
        self.assertTrue(any("feasibility floor" in p for p in problems), problems)

    def test_unquantised_slot_is_caught(self):
        problems = self._problems(lambda c: c["timing"].__setitem__("slot_duration_us", 10050))
        self.assertTrue(any("multiple of" in p for p in problems), problems)

    def test_too_many_taps_is_caught(self):
        problems = self._problems(lambda c: c["cir"].__setitem__("taps", 192))
        self.assertTrue(any("cir.taps" in p for p in problems), problems)

    def test_too_many_nodes_is_caught(self):
        problems = self._problems(lambda c: c["network"].__setitem__("n_nodes", 9))
        self.assertTrue(any("n_nodes" in p for p in problems), problems)

    def test_master_outside_range_is_caught(self):
        problems = self._problems(lambda c: c["network"].__setitem__("master_node_id", 7))
        self.assertTrue(any("master_node_id" in p for p in problems), problems)

    def test_ext_phr_required_above_127_bytes(self):
        problems = self._problems(lambda c: c["phy"].__setitem__("phr_mode", "std"))
        self.assertTrue(any("phr_mode" in p for p in problems), problems)

    def test_frame_above_ext_limit_is_caught(self):
        problems = self._problems(lambda c: c["framing"].__setitem__("max_frame_bytes", 2048))
        self.assertTrue(any("max_frame_bytes" in p for p in problems), problems)

    def test_usb_budget_overrun_is_caught(self):
        problems = self._problems(lambda c: c["budget"].__setitem__("usb_budget_bytes_per_s", 1000))
        self.assertTrue(any("USB load" in p for p in problems), problems)

    def test_pdoa_requires_dual_antenna(self):
        problems = self._problems(lambda c: c["phy"].__setitem__("pdoa_mode", 1))
        self.assertTrue(any("pdoa_mode" in p for p in problems), problems)

    def test_left_taps_must_be_inside_window(self):
        problems = self._problems(lambda c: c["cir"].__setitem__("left_taps", 64))
        self.assertTrue(any("left_taps" in p for p in problems), problems)

    def test_missing_derived_block_is_caught(self):
        cfg = load_example()
        del cfg["derived"]
        _, problems = hc.verify(cfg)
        self.assertTrue(any("derived" in p for p in problems), problems)

    def test_wrong_schema_is_caught(self):
        problems = self._problems(lambda c: c.__setitem__("schema", "something-else"))
        self.assertTrue(any("schema" in p for p in problems), problems)


class UsbRecordOverheads(unittest.TestCase):
    """contracts/usb-cdc-v1.md. These feed the export-blocking USB budget, so
    they are normative rather than descriptive."""

    def test_outer_framing_is_16_bytes(self):
        # 12 B header (sync, version, type, flags, reserved, length, sequence)
        # plus 4 B CRC32.
        self.assertEqual(hc.USB_OUTER_BYTES, 16)

    def test_record_wrapper_sizes(self):
        self.assertEqual(hc.USB_RADIO_FRAME_WRAPPER_BYTES, 8)  # u40 + u8 + u16
        self.assertEqual(hc.USB_LOCAL_OBS_WRAPPER_BYTES, 5)  # u8 + u32
        self.assertEqual(hc.USB_TX_RECORD_PAYLOAD_BYTES, 13)  # u32+u8+u40+u16+u8
        self.assertEqual(hc.USB_CYCLE_SUMMARY_PAYLOAD_BYTES, 34)

    def test_usb_load_matches_the_record_inventory(self):
        """Per cycle: (N-1)*M RADIO_FRAME, (N-1) LOCAL_OBS, M TX_RECORD, 1 SUMMARY."""
        for n in (2, 4, 6, 8):
            cfg = load_example()
            cfg["network"]["n_nodes"] = n
            cfg["budget"]["usb_budget_bytes_per_s"] = 10_000_000
            der = hc.derive(cfg)["derived"]
            m = der["m_slots_per_superslot"]
            expected = (
                (n - 1) * m * (
                    hc.USB_OUTER_BYTES
                    + hc.USB_RADIO_FRAME_WRAPPER_BYTES
                    + der["frame_bytes"]
                    - hc.FCS_BYTES
                )
                + (n - 1) * (hc.USB_OUTER_BYTES + hc.USB_LOCAL_OBS_WRAPPER_BYTES + der["subreport_bytes"])
                + m * (hc.USB_OUTER_BYTES + hc.USB_TX_RECORD_PAYLOAD_BYTES)
                + (hc.USB_OUTER_BYTES + hc.USB_CYCLE_SUMMARY_PAYLOAD_BYTES)
            )
            self.assertEqual(der["gateway_usb_bytes_per_cycle"], expected, f"N={n}")

    def test_usb_load_stays_in_the_documented_band_at_the_floor(self):
        low, high = 10**9, 0
        for n in range(2, hc.MAX_NODES + 1):
            cfg = load_example()
            cfg["network"]["n_nodes"] = n
            cfg["budget"]["usb_budget_bytes_per_s"] = 10_000_000
            cfg["timing"]["slot_duration_us"] = hc.derive(cfg)["derived"]["slot_floor_us"]
            bps = hc.derive(cfg)["derived"]["gateway_usb_bytes_per_s"]
            low, high = min(low, bps), max(high, bps)
        self.assertGreater(low, 225_000)
        self.assertLess(high, 440_000)


class TimestampSpan(unittest.TestCase):
    def test_40_bit_timestamps_cover_every_reachable_configuration(self):
        """Item 13: 32-bit would fail above 67.2 ms; 40-bit must never fail."""
        worst_us = 0
        for n in range(2, hc.MAX_NODES + 1):
            for taps in (32, 64, 128):
                cfg = load_example()
                cfg["network"]["n_nodes"] = n
                cfg["cir"]["taps"] = taps
                der = hc.derive(cfg)["derived"]
                cfg["timing"]["slot_duration_us"] = der["slot_floor_us"]
                der = hc.derive(cfg)["derived"]
                worst_us = max(worst_us, (n - 1) * der["superslot_us"])
        self.assertLess(worst_us * hc.DTU_PER_MS / 1000, 2**40)

    def test_32_bit_would_have_failed_somewhere(self):
        """Justifies the item 13 decision rather than asserting it."""
        span_32bit_us = 2**32 / hc.DTU_PER_MS * 1000
        self.assertAlmostEqual(span_32bit_us / 1000, 67.2, places=1)


if __name__ == "__main__":
    unittest.main()
