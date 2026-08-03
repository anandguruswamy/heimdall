#!/usr/bin/env python3
"""Estimate the aligned direct-path response in a Heimdall .husb capture."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import numpy as np

from radar_map.capture import decode_capture


def scaled_cir(observation) -> np.ndarray:
    values = np.frombuffer(observation.cir_blob, dtype="<i2").reshape(-1, 2)
    cir = values[:, 0].astype(np.float64) + 1j * values[:, 1].astype(np.float64)
    correction_db = (observation.dgc_decision - 3.0) * 2.65
    return cir * 10.0 ** (correction_db / 20.0) / observation.accum_count


def normalized_correlation(reference: np.ndarray, signal: np.ndarray) -> float:
    denominator = np.linalg.norm(reference) * np.linalg.norm(signal)
    return float(abs(np.vdot(reference, signal)) / denominator) if denominator else 0.0


def representative_reference(rows: np.ndarray, energies: np.ndarray) -> np.ndarray:
    count = min(9, len(rows))
    candidates = np.unique(np.linspace(0, len(rows) - 1, count, dtype=int))
    candidates = np.unique(
        np.append(candidates, int(np.argmin(np.abs(energies - np.median(energies)))))
    )
    best_index = int(candidates[0])
    best_score = -1.0
    for index in candidates:
        score = float(
            np.median([normalized_correlation(rows[index], row) for row in rows])
        )
        if score > best_score:
            best_index, best_score = int(index), score
    return rows[best_index]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Align hardware CIRs to the fractional first-path marker and compare "
            "the pre-first-path response with the measured noise floor."
        )
    )
    parser.add_argument("capture", type=Path)
    parser.add_argument("--oversample", type=int, default=16)
    parser.add_argument("--profile-min-tap", type=float, default=-12.0)
    parser.add_argument("--profile-max-tap", type=float, default=12.0)
    args = parser.parse_args()
    if args.oversample < 1:
        parser.error("--oversample must be positive")

    observations, decode_stats = decode_capture(args.capture)
    grouped = defaultdict(list)
    for item in observations:
        if (
            item.obs_flags & 0x05 == 0x05
            and item.accum_count > 0
            and item.cir_taps == 64
        ):
            grouped[(item.observed_node_id, item.reporting_node_id)].append(item)

    relative_taps = np.arange(
        args.profile_min_tap,
        args.profile_max_tap + 0.5 / args.oversample,
        1.0 / args.oversample,
    )
    direct = (relative_taps >= -2.0) & (relative_taps <= 6.0)
    link_profiles = []
    link_rows = []
    link_peaks = []

    for link, items in sorted(grouped.items()):
        markers = np.asarray(
            [item.fp_index_q10_6 / 64.0 - item.cir_start_offset for item in items]
        )
        first_paths = np.asarray([item.fp_index_q10_6 / 64.0 for item in items])
        starts = np.asarray([item.cir_start_offset for item in items])
        raw_rows = np.stack([scaled_cir(item) for item in items])
        energies = np.sum(np.abs(raw_rows) ** 2, axis=1)
        median_energy = max(float(np.median(energies)), np.finfo(np.float64).tiny)

        reference = representative_reference(raw_rows, energies)
        correlations = np.asarray(
            [normalized_correlation(reference, row) for row in raw_rows]
        )
        accepted = (
            (np.abs(first_paths - np.median(first_paths)) <= 8.0)
            & (np.abs(starts - np.median(starts)) <= 8.0)
            & (energies / median_energy >= 0.10)
            & (correlations >= 0.25)
        )

        power_rows = []
        coherent_rows = []
        coherent_reference = None
        for item, marker, cir, keep in zip(items, markers, raw_rows, accepted):
            if not keep:
                continue
            sample_positions = np.arange(item.cir_taps, dtype=np.float64) - marker
            aligned = (
                np.interp(relative_taps, sample_positions, cir.real, left=0.0, right=0.0)
                + 1j
                * np.interp(
                    relative_taps, sample_positions, cir.imag, left=0.0, right=0.0
                )
            )
            norm = float(np.sqrt(np.sum(np.abs(aligned[direct]) ** 2)))
            if norm == 0.0:
                continue
            aligned /= norm
            power_rows.append(np.abs(aligned) ** 2)
            if coherent_reference is None:
                coherent_reference = aligned.copy()
            phase = np.angle(np.vdot(coherent_reference[direct], aligned[direct]))
            coherent_rows.append(aligned * np.exp(-1j * phase))

        if not power_rows:
            continue
        power = np.mean(power_rows, axis=0)
        coherent_power = np.abs(np.mean(coherent_rows, axis=0)) ** 2
        link_profiles.append((power, coherent_power))
        link_rows.append((link, len(items), len(power_rows)))
        peak_region = (relative_taps >= 0.0) & (relative_taps <= 6.0)
        link_peaks.append(
            (link, float(relative_taps[peak_region][np.argmax(power[peak_region])]))
        )

    if not link_profiles:
        raise SystemExit("no valid 64-tap directed links were found")

    power = np.median(np.stack([profile[0] for profile in link_profiles]), axis=0)
    coherent_power = np.median(
        np.stack([profile[1] for profile in link_profiles]), axis=0
    )
    peak_region = (relative_taps >= -2.0) & (relative_taps <= 8.0)
    peak_power = float(np.max(power[peak_region]))
    coherent_peak_power = float(np.max(coherent_power[peak_region]))
    peak_tap = float(relative_taps[peak_region][np.argmax(power[peak_region])])
    power_db = 10.0 * np.log10(np.maximum(power, 1e-15) / peak_power)
    coherent_db = 10.0 * np.log10(
        np.maximum(coherent_power, 1e-15) / coherent_peak_power
    )

    baseline = (relative_taps >= -12.0) & (relative_taps <= -7.0)
    precursor = (relative_taps >= -6.0) & (relative_taps < 0.0)
    postcursor = (relative_taps >= 0.0) & (relative_taps < 6.0)
    baseline_power = float(np.median(power[baseline]))
    precursor_excess = float(np.sum(np.maximum(power[precursor] - baseline_power, 0.0)))
    postcursor_excess = float(np.sum(np.maximum(power[postcursor] - baseline_power, 0.0)))

    print(
        f"records={decode_stats['records']} observations={len(observations)} "
        f"links={len(link_profiles)} accepted_rows={sum(row[2] for row in link_rows)}"
    )
    print("link input_rows accepted_rows peak_relative_taps")
    peak_by_link = dict(link_peaks)
    for link, input_rows, accepted_rows in link_rows:
        print(
            f"{link[0]}>{link[1]} {input_rows} {accepted_rows} "
            f"{peak_by_link[link]:.4f}"
        )
    print(f"median_profile_peak_relative_taps={peak_tap:.4f}")
    print(
        "baseline_db_relative_peak="
        f"{10.0 * np.log10(baseline_power / peak_power):.2f}"
    )
    print(
        "precursor_excess_to_postcursor_excess_db="
        f"{10.0 * np.log10(max(precursor_excess, 1e-15) / postcursor_excess):.2f}"
    )
    print("relative_tap power_db coherent_power_db")
    for relative_tap in range(-8, 9):
        index = int(np.argmin(np.abs(relative_taps - relative_tap)))
        print(
            f"{relative_tap:+d} {power_db[index]:+.2f} "
            f"{coherent_db[index]:+.2f}"
        )
    for threshold_db in (3.0, 6.0, 10.0):
        indices = np.flatnonzero(
            precursor & (power >= baseline_power * 10.0 ** (threshold_db / 10.0))
        )
        value = f"{relative_taps[indices[0]]:.4f}" if len(indices) else "none"
        print(f"earliest_pre_first_path_above_{threshold_db:.0f}db={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
