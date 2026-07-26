#!/usr/bin/env python3
"""Heimdall beacon configuration derivation, verification, and header emission.

This is the reference implementation of the sizing model defined in
``contracts/beacon-v1.md``.  The browser configuration tool reimplements the
same formulas; this script is what the firmware build uses to prove the two
agree.

Decision log reference: ``docs/protocol-decisions.md`` item 27.  The
configuration tool is authoritative for the values that get flashed, but the
build re-derives them independently and fails if any disagrees, so formula
drift between the JavaScript tool and this model becomes a build error rather
than a silent lie.

Subcommands
-----------
derive   Read an input-only configuration and emit a complete one with the
         ``derived`` block filled in.
verify   Re-derive every value, compare against the declared ``derived``
         block, check all invariants, and optionally emit the C header.

Exit status is non-zero on any mismatch, invariant violation, or budget
overrun.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path
from typing import Any

SCHEMA = "heimdall-beacon-config/1"

# ---------------------------------------------------------------------------
# Wire-format constants.  These are normative and mirror contracts/beacon-v1.md.
# Changing any of them is a protocol_version bump.
# ---------------------------------------------------------------------------

MAC_HEADER_BYTES = 9
HEIMDALL_HEADER_BYTES = 22
FRAME_HEADER_BYTES = MAC_HEADER_BYTES + HEIMDALL_HEADER_BYTES  # 31
FCS_BYTES = 2
SUBREPORT_METADATA_BYTES = 40
SUBREPORT_CRC_BYTES = 4
BYTES_PER_TAP = 4  # int16 I + int16 Q

MAX_NODES = 8
MAX_CIR_TAPS = 128
MAX_FRAME_BYTES_LIMIT = 1023  # DWT_PHRMODE_EXT ceiling
CIR_CHUNK_SAMPLES = 16  # CHUNK_CIR_NB_SAMP in the Qorvo driver

# ---------------------------------------------------------------------------
# USB record overheads.  Normative in contracts/usb-cdc-v1.md.  These replace
# the former ``usb_wrapper_bytes`` budget parameter, which was a placeholder
# controlling a hard export limit without a defined layout behind it.
# ---------------------------------------------------------------------------

USB_OUTER_BYTES = 16  # 12 B framing header + 4 B CRC32
USB_RADIO_FRAME_WRAPPER_BYTES = 8  # rx_timestamp u40 + rx_flags u8 + frame_len u16
USB_LOCAL_OBS_WRAPPER_BYTES = 5  # reporting_node_id u8 + k u32
USB_TX_RECORD_PAYLOAD_BYTES = 13
USB_CYCLE_SUMMARY_PAYLOAD_BYTES = 34

# ---------------------------------------------------------------------------
# PHY timing constants.  IEEE 802.15.4 HRP UWB.
# ---------------------------------------------------------------------------

# Preamble symbol duration by mean PRF.
PREAMBLE_SYMBOL_NS = {16: 993.59, 64: 1017.63}

# Data symbol duration by data rate.  The DW3000 supports 850k and 6.8M only.
DATA_SYMBOL_NS = {850: 1025.64, 6800: 128.21}

# PHR is transmitted at the 850 kb/s base rate unless phr_rate is "dta".
PHR_BASE_SYMBOL_NS = 1025.64
PHR_SYMBOLS = 21

# SFD length in symbols by sfdType.
SFD_SYMBOLS = {0: 8, 1: 8, 2: 16, 3: 8}

# Reed-Solomon RS(63,55) over GF(2^6): 55 data symbols = 330 bits produce
# 63 coded symbols = 378 bits.
RS_DATA_BITS = 330
RS_CODED_BITS = 378

# Device time units.  1 ms = 63_897_600 DTU exactly; 1 us is NOT an integer.
DTU_PER_MS = 63_897_600
SLOT_QUANTUM_US = 100  # 0.1 ms, decision log item 6

PHR_MODES = {"std": 0, "ext": 1}
PHR_RATES = {"std": 0, "dta": 1}


class ConfigError(Exception):
    """Raised for any invariant violation or declared/derived mismatch."""


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xorout."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# ---------------------------------------------------------------------------
# Airtime model
# ---------------------------------------------------------------------------


def frame_airtime_us(
    psdu_bytes: int,
    preamble_length: int,
    prf_mhz: int,
    sfd_type: int,
    data_rate_kbps: int,
    phr_rate: str,
) -> float:
    """Airtime of one frame in microseconds.

    ``psdu_bytes`` is the full PHY payload: MAC header + Heimdall header +
    pooled-report bytes + FCS.

    Note on the data rate.  The 6.8 Mb/s figure is the *net* rate after
    Reed-Solomon coding: the coded symbol rate is 1/128.21 ns = 7.8 Mb/s, and
    7.8 * (330/378) = 6.81 Mb/s.  So ``psdu_bytes * 8 / 6.8e6`` already
    approximates the data field correctly, differing from the block model below
    only by padding of the final RS block -- 0.67 percent at 1023 B.

    The large error in a naive airtime estimate is omitting SHR and PHR, which
    together are about 160 us.  That is 12 percent of a maximum-size frame and
    29 percent of a 329 B one.
    """
    if prf_mhz not in PREAMBLE_SYMBOL_NS:
        raise ConfigError(f"unsupported prf_mhz {prf_mhz}, expected one of {sorted(PREAMBLE_SYMBOL_NS)}")
    if data_rate_kbps not in DATA_SYMBOL_NS:
        raise ConfigError(
            f"unsupported data_rate_kbps {data_rate_kbps}, expected one of {sorted(DATA_SYMBOL_NS)}"
        )
    if sfd_type not in SFD_SYMBOLS:
        raise ConfigError(f"unsupported sfd_type {sfd_type}, expected one of {sorted(SFD_SYMBOLS)}")
    if phr_rate not in PHR_RATES:
        raise ConfigError(f"unsupported phr_rate {phr_rate!r}, expected one of {sorted(PHR_RATES)}")

    t_psym = PREAMBLE_SYMBOL_NS[prf_mhz]
    t_dsym = DATA_SYMBOL_NS[data_rate_kbps]
    t_phrsym = t_dsym if phr_rate == "dta" else PHR_BASE_SYMBOL_NS

    shr_ns = (preamble_length + SFD_SYMBOLS[sfd_type]) * t_psym
    phr_ns = PHR_SYMBOLS * t_phrsym

    data_bits = psdu_bytes * 8
    blocks = math.ceil(data_bits / RS_DATA_BITS)
    data_ns = blocks * RS_CODED_BITS * t_dsym

    return (shr_ns + phr_ns + data_ns) / 1000.0


# ---------------------------------------------------------------------------
# RX processing model
# ---------------------------------------------------------------------------


def spi_byte_us(budget: dict[str, Any]) -> float:
    return 8.0 / budget["spi_hz"] * 1e6


def rx_processing_us(frame_bytes: int, cir_taps: int, subreport_bytes: int, budget: dict[str, Any]) -> float:
    """Worst-case RX callback duration in microseconds.

    SPI components are derived from byte counts; CRC throughput is measured.
    See the calibration record in ``firmware/radio/BRINGUP-NOTES.md``.

    Accounts for the reordered callback of decision item 30: read RX data,
    then diagnostics, then CIR, and only then re-arm RX.
    """
    overhead_us = budget["spi_transaction_overhead_us"]
    byte_us = spi_byte_us(budget)

    rxdata_us = frame_bytes * byte_us

    # Each CIR chunk is one dummy byte plus 6 bytes per complex sample, and is
    # preceded by two 32-bit indirect-pointer register writes.
    chunks = math.ceil(cir_taps / CIR_CHUNK_SAMPLES)
    chunk_bytes = 1 + 6 * CIR_CHUNK_SAMPLES + 8
    cir_us = chunks * (chunk_bytes * byte_us + overhead_us)

    diag_us = budget["diagnostics_bytes"] * byte_us + overhead_us

    # contracts/beacon-v1.md section 9: the subreport CRC32 is computed by the
    # observing node at observation time, so it lands inside this callback and
    # is not negligible. The stored CRC itself is not part of its input.
    crc_us = (subreport_bytes - SUBREPORT_CRC_BYTES) / budget["crc32_bytes_per_us"]

    return rxdata_us + cir_us + diag_us + crc_us + budget["rx_fixed_overhead_us"]


def tx_write_us(frame_bytes: int, budget: dict[str, Any]) -> float:
    """SPI time to push one assembled frame into the DW3000 transmit buffer."""
    return frame_bytes * spi_byte_us(budget) + budget["spi_transaction_overhead_us"]


# ---------------------------------------------------------------------------
# config_hash
# ---------------------------------------------------------------------------


def hashed_parameter_struct(cfg: dict[str, Any]) -> bytes:
    """Pack the parameters covered by config_hash.

    Hashing this packed binary form rather than the JSON text makes the hash
    immune to whitespace, key ordering, and number formatting.  Field order is
    normative; see contracts/beacon-v1.md.
    """
    net = cfg["network"]
    phy = cfg["phy"]
    cir = cfg["cir"]
    fr = cfg["framing"]
    tim = cfg["timing"]
    der = cfg["derived"]

    return struct.pack(
        "<BBBBHHHIBBBBBHBBHIBBBBIBB",
        cfg["protocol_version"],
        net["n_nodes"],
        der["m_slots_per_superslot"],
        net["master_node_id"],
        net["network_id"],
        fr["max_frame_bytes"],
        der["frame_payload_bytes"],
        tim["slot_duration_us"],
        cir["taps"],
        cir["left_taps"],
        phy["channel"],
        phy["tx_preamble_code"],
        phy["rx_preamble_code"],
        phy["preamble_length"],
        phy["pac"],
        phy["sfd_type"],
        phy["sfd_timeout"],
        phy["data_rate_kbps"],
        PHR_MODES[phy["phr_mode"]],
        PHR_RATES[phy["phr_rate"]],
        phy["pdoa_mode"],
        phy["tx_pg_delay"],
        phy["tx_power"],
        net["evidence_age_threshold"],
        1 if fr["enable_frame_filter"] else 0,
    )


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def derive(cfg: dict[str, Any]) -> dict[str, Any]:
    """Compute every derived value from the input parameters."""
    net = cfg["network"]
    phy = cfg["phy"]
    cir = cfg["cir"]
    fr = cfg["framing"]
    budget = cfg["budget"]

    n = net["n_nodes"]
    taps = cir["taps"]

    subreport_bytes = SUBREPORT_METADATA_BYTES + BYTES_PER_TAP * taps
    pooled_max = (n - 1) * subreport_bytes

    capacity_max = fr["max_frame_bytes"] - FRAME_HEADER_BYTES - FCS_BYTES
    if capacity_max <= 0:
        raise ConfigError(
            f"max_frame_bytes {fr['max_frame_bytes']} leaves no payload after "
            f"{FRAME_HEADER_BYTES} B header and {FCS_BYTES} B FCS"
        )

    m = math.ceil(pooled_max / capacity_max)
    frame_payload = math.ceil(pooled_max / m)  # balanced packing, item 18
    frame_bytes = frame_payload + FRAME_HEADER_BYTES + FCS_BYTES

    airtime = frame_airtime_us(
        frame_bytes,
        phy["preamble_length"],
        phy["prf_mhz"],
        phy["sfd_type"],
        phy["data_rate_kbps"],
        phy["phr_rate"],
    )
    rx_us = rx_processing_us(frame_bytes, taps, subreport_bytes, budget)
    tx_us = tx_write_us(frame_bytes, budget)
    assembly_us = budget["report_assembly_us"]
    margin = budget["processing_margin_factor"]

    # Two independent constraints bound the slot; the floor is the larger.
    #
    # 1. Reception. Every slot carries a frame, so every slot must fit that
    #    frame's airtime plus the RX callback that follows it.
    #
    # 2. Report assembly. Node i's report must contain U(i, i-1), observed from
    #    the peer whose superslot immediately precedes its own. The whole chain
    #    -- read out, build subreport, assemble report, write TX buffer -- must
    #    complete between the end of that peer's m=0 frame and the start of node
    #    i's own m=0 frame, a window of M * T_slot - airtime.
    #
    #    TX preparation cannot be pipelined into the preceding frame's airtime,
    #    because the bytes being written include the measurement of that very
    #    frame. Only M = 1 configurations are bound by this; M >= 2 gives a full
    #    extra slot of slack.
    floor_rx_raw = airtime + margin * rx_us
    floor_assembly_raw = (airtime + margin * (rx_us + assembly_us + tx_us)) / m
    slot_floor_raw = max(floor_rx_raw, floor_assembly_raw)
    slot_floor = int(math.ceil(slot_floor_raw / SLOT_QUANTUM_US) * SLOT_QUANTUM_US)

    slot = cfg["timing"]["slot_duration_us"]
    superslot = m * slot
    cycle = n * superslot

    # Gateway USB load. Record overheads are normative in
    # contracts/usb-cdc-v1.md. Per cycle the gateway emits:
    #   - one RADIO_FRAME per frame received from each of its N-1 peers
    #   - one LOCAL_OBS per peer it observed itself
    #   - one TX_RECORD per frame it transmitted
    #   - one CYCLE_SUMMARY
    usb_per_cycle = (
        (n - 1) * m * (USB_OUTER_BYTES + USB_RADIO_FRAME_WRAPPER_BYTES + frame_bytes)
        + (n - 1) * (USB_OUTER_BYTES + USB_LOCAL_OBS_WRAPPER_BYTES + subreport_bytes)
        + m * (USB_OUTER_BYTES + USB_TX_RECORD_PAYLOAD_BYTES)
        + (USB_OUTER_BYTES + USB_CYCLE_SUMMARY_PAYLOAD_BYTES)
    )
    usb_bps = usb_per_cycle / (cycle / 1e6)

    derived = {
        "subreport_bytes": subreport_bytes,
        "pooled_report_max_bytes": pooled_max,
        "frame_capacity_max_bytes": capacity_max,
        "m_slots_per_superslot": m,
        "frame_payload_bytes": frame_payload,
        "frame_bytes": frame_bytes,
        "frame_airtime_us": round(airtime, 2),
        "rx_processing_us": round(rx_us, 2),
        "tx_write_us": round(tx_us, 2),
        "slot_floor_rx_us": int(math.ceil(floor_rx_raw / SLOT_QUANTUM_US) * SLOT_QUANTUM_US),
        "slot_floor_assembly_us": int(math.ceil(floor_assembly_raw / SLOT_QUANTUM_US) * SLOT_QUANTUM_US),
        "slot_floor_us": slot_floor,
        "slot_floor_binding_constraint": "assembly" if floor_assembly_raw > floor_rx_raw else "reception",
        "superslot_us": superslot,
        "cycle_us": cycle,
        "per_link_rate_hz": round(1e6 / cycle, 3),
        "gateway_usb_bytes_per_s": int(round(usb_bps)),
        "gateway_usb_bytes_per_cycle": usb_per_cycle,
        "straddling_subreport_index": _straddle_index(subreport_bytes, frame_payload, n),
    }

    out = dict(cfg)
    out["derived"] = derived
    derived["config_hash"] = crc16_ccitt_false(hashed_parameter_struct(out))
    return out


def _straddle_index(subreport_bytes: int, frame_payload: int, n: int) -> int:
    """Index of the subreport crossing the first frame boundary, or -1.

    Informational.  Decision item 19 rotates subreport order per cycle so this
    position does not penalise the same link permanently.
    """
    if frame_payload <= 0:
        return -1
    for idx in range(n - 1):
        start = idx * subreport_bytes
        end = start + subreport_bytes
        if start < frame_payload < end:
            return idx
    return -1


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def check_invariants(cfg: dict[str, Any]) -> list[str]:
    """Return a list of invariant violations.  Empty means the config is sane.

    These are the same assertions the firmware performs at boot
    (decision item 27), duplicated here so a bad config never reaches a board.
    """
    errors: list[str] = []
    net = cfg["network"]
    phy = cfg["phy"]
    cir = cfg["cir"]
    fr = cfg["framing"]
    tim = cfg["timing"]
    der = cfg["derived"]
    budget = cfg["budget"]

    n = net["n_nodes"]

    def bad(msg: str) -> None:
        errors.append(msg)

    if not 2 <= n <= MAX_NODES:
        bad(f"n_nodes {n} outside 2..{MAX_NODES}")
    if not 0 <= net["master_node_id"] < n:
        bad(f"master_node_id {net['master_node_id']} not in 0..{n - 1}")
    if not 1 <= net["evidence_age_threshold"] <= 255:
        bad(f"evidence_age_threshold {net['evidence_age_threshold']} outside 1..255")

    if not 1 <= cir["taps"] <= MAX_CIR_TAPS:
        bad(f"cir.taps {cir['taps']} outside 1..{MAX_CIR_TAPS}")
    if not 0 <= cir["left_taps"] < cir["taps"]:
        bad(f"cir.left_taps {cir['left_taps']} must be in 0..taps-1")

    if fr["max_frame_bytes"] > MAX_FRAME_BYTES_LIMIT:
        bad(f"max_frame_bytes {fr['max_frame_bytes']} exceeds EXT PHR limit {MAX_FRAME_BYTES_LIMIT}")
    if fr["max_frame_bytes"] > 127 and phy["phr_mode"] != "ext":
        bad(f"max_frame_bytes {fr['max_frame_bytes']} requires phr_mode 'ext', got {phy['phr_mode']!r}")

    if der["frame_bytes"] > fr["max_frame_bytes"]:
        bad(f"frame_bytes {der['frame_bytes']} exceeds max_frame_bytes {fr['max_frame_bytes']}")
    if der["m_slots_per_superslot"] * der["frame_payload_bytes"] < der["pooled_report_max_bytes"]:
        bad(
            f"M * frame_payload ({der['m_slots_per_superslot']} * {der['frame_payload_bytes']}) "
            f"cannot hold pooled_report_max_bytes {der['pooled_report_max_bytes']}"
        )

    slot = tim["slot_duration_us"]
    if slot % SLOT_QUANTUM_US != 0:
        bad(f"slot_duration_us {slot} is not a multiple of {SLOT_QUANTUM_US} us")
    if slot < der["slot_floor_us"]:
        which = der["slot_floor_binding_constraint"]
        if which == "assembly":
            detail = (
                f"report-assembly path binds: airtime {der['frame_airtime_us']:.1f} + "
                f"{budget['processing_margin_factor']}x (RX {der['rx_processing_us']:.1f} + "
                f"assembly {budget['report_assembly_us']:.1f} + TX write {der['tx_write_us']:.1f}) "
                f"over M={der['m_slots_per_superslot']}"
            )
        else:
            detail = (
                f"reception path binds: airtime {der['frame_airtime_us']:.1f} + "
                f"{budget['processing_margin_factor']}x RX processing {der['rx_processing_us']:.1f}"
            )
        bad(f"slot_duration_us {slot} below feasibility floor {der['slot_floor_us']} us ({detail})")

    # Timestamp differencing span, decision item 13.  40-bit timestamps hold
    # 17.2 s so this cannot fail today, but the check documents the limit that
    # would bite if the width were ever reduced.
    span_us = (n - 1) * der["superslot_us"]
    span_dtu = span_us * DTU_PER_MS / 1000
    if span_dtu >= 2**40:
        bad(f"differencing span {span_us} us exceeds 40-bit timestamp range")

    if der["gateway_usb_bytes_per_s"] > budget["usb_budget_bytes_per_s"]:
        bad(
            f"gateway USB load {der['gateway_usb_bytes_per_s']} B/s exceeds budget "
            f"{budget['usb_budget_bytes_per_s']} B/s "
            f"(reduce cir.taps, raise slot_duration_us, or raise the budget once measured)"
        )

    if phy["pdoa_mode"] != 0:
        bad(f"pdoa_mode {phy['pdoa_mode']} requires dual-antenna hardware; current boards need 0")

    return errors


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify(cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Re-derive and compare against the declared derived block."""
    problems: list[str] = []

    if cfg.get("schema") != SCHEMA:
        problems.append(f"schema is {cfg.get('schema')!r}, expected {SCHEMA!r}")

    declared = cfg.get("derived")
    if declared is None:
        problems.append("configuration has no 'derived' block to verify")
        return cfg, problems

    recomputed = derive(cfg)["derived"]

    for key in sorted(set(declared) | set(recomputed)):
        if key not in declared:
            problems.append(f"derived.{key} missing from configuration (expected {recomputed[key]})")
            continue
        if key not in recomputed:
            problems.append(f"derived.{key} present in configuration but not produced by the model")
            continue
        want, got = recomputed[key], declared[key]
        if isinstance(want, float) or isinstance(got, float):
            if abs(float(want) - float(got)) > 0.011:
                problems.append(f"derived.{key} declared {got}, model says {want}")
        elif want != got:
            problems.append(f"derived.{key} declared {got}, model says {want}")

    problems.extend(check_invariants({**cfg, "derived": recomputed}))
    return {**cfg, "derived": recomputed}, problems


# ---------------------------------------------------------------------------
# Header emission
# ---------------------------------------------------------------------------


def emit_header(cfg: dict[str, Any]) -> str:
    net, phy, cir = cfg["network"], cfg["phy"], cfg["cir"]
    fr, tim, der = cfg["framing"], cfg["timing"], cfg["derived"]

    defines: list[tuple[str, Any, str]] = [
        ("HEIMDALL_PROTOCOL_VERSION", cfg["protocol_version"], "beacon contract version"),
        ("HEIMDALL_N_NODES", net["n_nodes"], "deployed node count, cycle is exactly this many superslots"),
        ("HEIMDALL_MAX_NODES", MAX_NODES, "encoding ceiling, never appears in the schedule"),
        ("HEIMDALL_MASTER_NODE_ID", net["master_node_id"], "bootstrap transmitter and liveness anchor"),
        ("HEIMDALL_NETWORK_ID", net["network_id"], "802.15.4 PAN ID, matched by hardware frame filter"),
        ("HEIMDALL_EVIDENCE_AGE_THRESHOLD", net["evidence_age_threshold"], "transmit iff evidence_age <= this"),
        ("HEIMDALL_CONFIG_HASH", der["config_hash"], "CRC-16/CCITT-FALSE over the packed parameter struct"),
        ("HEIMDALL_M", der["m_slots_per_superslot"], "slots per superslot"),
        ("HEIMDALL_SLOT_DURATION_US", tim["slot_duration_us"], "uniform across all slots"),
        ("HEIMDALL_SLOT_FLOOR_US", der["slot_floor_us"], "feasibility floor, slot must not be below this"),
        ("HEIMDALL_SLOT_FLOOR_RX_US", der["slot_floor_rx_us"], "reception constraint"),
        ("HEIMDALL_SLOT_FLOOR_ASSEMBLY_US", der["slot_floor_assembly_us"], "report-assembly constraint"),
        ("HEIMDALL_RX_PROCESSING_BUDGET_US", int(der["rx_processing_us"]), "modelled, must be measured"),
        ("HEIMDALL_TX_WRITE_BUDGET_US", int(der["tx_write_us"]), "modelled, must be measured"),
        ("HEIMDALL_SUPERSLOT_US", der["superslot_us"], ""),
        ("HEIMDALL_CYCLE_US", der["cycle_us"], "full cycle, all N superslots"),
        ("HEIMDALL_FRAME_HEADER_BYTES", FRAME_HEADER_BYTES, "9 B MAC + 22 B Heimdall"),
        ("HEIMDALL_FRAME_PAYLOAD_BYTES", der["frame_payload_bytes"], "balanced pooled-report bytes per frame"),
        ("HEIMDALL_FRAME_BYTES", der["frame_bytes"], "full PSDU including header and FCS"),
        ("HEIMDALL_MAX_FRAME_BYTES", fr["max_frame_bytes"], ""),
        ("HEIMDALL_SUBREPORT_BYTES", der["subreport_bytes"], "40 B metadata + 4 B per tap"),
        ("HEIMDALL_POOLED_REPORT_MAX_BYTES", der["pooled_report_max_bytes"], "(N-1) subreports"),
        ("HEIMDALL_CIR_TAPS", cir["taps"], ""),
        ("HEIMDALL_CIR_LEFT_TAPS", cir["left_taps"], "taps before the CIA first-path index"),
        ("HEIMDALL_CIR_CHUNKS", math.ceil(cir["taps"] / CIR_CHUNK_SAMPLES), "16-sample SPI reads required"),
        ("HEIMDALL_ENABLE_FRAME_FILTER", 1 if fr["enable_frame_filter"] else 0, "escape hatch for bring-up"),
        ("HEIMDALL_PHY_CHANNEL", phy["channel"], ""),
        ("HEIMDALL_PHY_TX_PREAMBLE_CODE", phy["tx_preamble_code"], ""),
        ("HEIMDALL_PHY_RX_PREAMBLE_CODE", phy["rx_preamble_code"], ""),
        ("HEIMDALL_PHY_PREAMBLE_LENGTH", phy["preamble_length"], ""),
        ("HEIMDALL_PHY_PAC", phy["pac"], ""),
        ("HEIMDALL_PHY_SFD_TYPE", phy["sfd_type"], ""),
        ("HEIMDALL_PHY_SFD_TIMEOUT", phy["sfd_timeout"], ""),
        ("HEIMDALL_PHY_DATA_RATE_KBPS", phy["data_rate_kbps"], ""),
        ("HEIMDALL_PHY_PHR_MODE_EXT", PHR_MODES[phy["phr_mode"]], "1 selects DWT_PHRMODE_EXT"),
        ("HEIMDALL_PHY_PHR_RATE_DTA", PHR_RATES[phy["phr_rate"]], ""),
        ("HEIMDALL_PHY_PDOA_MODE", phy["pdoa_mode"], ""),
        ("HEIMDALL_PHY_TX_PG_DELAY", f"0x{phy['tx_pg_delay']:02x}", ""),
        ("HEIMDALL_PHY_TX_POWER", f"0x{phy['tx_power']:08x}", ""),
    ]

    width = max(len(name) for name, _, _ in defines)
    lines = [
        "/* Generated by tools/config/heimdall_config.py.  Do not edit.",
        " *",
        f" * Source configuration : {cfg.get('_source_path', '<stdin>')}",
        f" * Generated by tool    : {cfg.get('generated_by', 'unknown')}",
        " *",
        " * Every value below was declared by the configuration tool and then",
        " * independently re-derived and verified by this script.  See",
        " * contracts/beacon-v1.md and docs/protocol-decisions.md item 27.",
        " */",
        "",
        "#ifndef HEIMDALL_BEACON_CONFIG_H",
        "#define HEIMDALL_BEACON_CONFIG_H",
        "",
    ]
    for name, value, comment in defines:
        line = f"#define {name.ljust(width)} {value}"
        if comment:
            line += f" /* {comment} */"
        lines.append(line)
    lines += [
        "",
        "/* Device time units.  1 ms is exactly 63897600 DTU; 1 us is not an",
        " * integer number of DTU, which is why slot durations are constrained",
        " * to 0.1 ms steps. */",
        f"#define HEIMDALL_DTU_PER_MS {DTU_PER_MS}UL",
        "",
        "#endif /* HEIMDALL_BEACON_CONFIG_H */",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    cfg = json.loads(text)
    cfg["_source_path"] = str(Path(path).as_posix())
    return cfg


def _strip_private(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_derive = sub.add_parser("derive", help="fill in the derived block")
    p_derive.add_argument("config")
    p_derive.add_argument("-o", "--output", help="write here instead of stdout")

    p_verify = sub.add_parser("verify", help="re-derive and assert against declared values")
    p_verify.add_argument("config")
    p_verify.add_argument("--emit-header", help="write the C parameter header here")

    args = parser.parse_args(argv)

    try:
        cfg = _load(args.config)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {args.config}: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "derive":
        try:
            full = derive(cfg)
        except (ConfigError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        text = json.dumps(_strip_private(full), indent=2) + "\n"
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            sys.stdout.write(text)
        for problem in check_invariants(full):
            print(f"warning: {problem}", file=sys.stderr)
        return 0

    try:
        full, problems = verify(cfg)
    except (ConfigError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if problems:
        print(f"error: {args.config} failed verification", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    der = full["derived"]
    print(
        f"ok: N={full['network']['n_nodes']} taps={full['cir']['taps']} "
        f"M={der['m_slots_per_superslot']} frame={der['frame_bytes']}B "
        f"airtime={der['frame_airtime_us']:.0f}us slot={full['timing']['slot_duration_us']}us "
        f"(floor {der['slot_floor_us']}us) cycle={der['cycle_us']}us "
        f"rate={der['per_link_rate_hz']:.1f}Hz usb={der['gateway_usb_bytes_per_s']}B/s "
        f"hash=0x{der['config_hash']:04x}"
    )

    if args.emit_header:
        out = Path(args.emit_header)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(emit_header(full), encoding="utf-8")
        print(f"ok: wrote {out.as_posix()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
