#!/usr/bin/env python3
"""Empirically estimate the DW3110 DGC gain step from a Heimdall .husb capture.

For a static scene, the true per-link received power is roughly constant over
the capture. The accumulator energy varies with the DGC gain that was applied,
so the ratio of median energies across adjacent DGC states on the same link
reveals the actual gain step in dB. This checks whether the 2.65 dB/step
assumed by the host DSP matches the hardware.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import numpy as np

from radar_map.capture import decode_capture


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Estimate the effective DW3110 DGC gain step (dB) per state."
    )
    parser.add_argument("capture", type=Path)
    parser.add_argument("--min-count", type=int, default=200)
    args = parser.parse_args()

    observations, decode_stats = decode_capture(args.capture)
    grouped: dict[tuple[int, int], dict[int, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for item in observations:
        if item.obs_flags & 0x01 == 0 or item.accum_count == 0:
            continue
        values = np.frombuffer(item.cir_blob, dtype="<i2").reshape(-1, 2)
        cir = values[:, 0].astype(np.float64) + 1j * values[:, 1].astype(np.float64)
        energy = float(np.sum(np.abs(cir) ** 2)) / (item.accum_count**2)
        link = (item.observed_node_id, item.reporting_node_id)
        grouped[link][item.dgc_decision].append(energy)

    state_energies: dict[int, list[float]] = defaultdict(list)
    usable_links = 0
    for link, states in sorted(grouped.items()):
        present = sorted(states)
        link_deltas: dict[int, float] = {}
        for state in present:
            if len(states[state]) < args.min_count:
                continue
            state_energies[state].append(float(np.median(states[state])))
            if state > 0 and state - 1 in states and len(states[state - 1]) >= args.min_count:
                ratio = float(np.median(states[state])) / float(np.median(states[state - 1]))
                if ratio > 0:
                    link_deltas[state] = 10.0 * np.log10(ratio)
        if link_deltas:
            usable_links += 1

    print(
        f"records={decode_stats['records']} observations={len(observations)} "
        f"links_with_state_pairs={usable_links}"
    )
    print("state median_energy_n accum_units db_relative_to_lowest log10_relative")
    energies = sorted(state_energies)
    if not energies:
        raise SystemExit("no usable DGC states found")
    reference = float(np.median(state_energies[energies[0]]))
    for state in energies:
        value = float(np.median(state_energies[state]))
        print(
            f"{state} {value:.6g} {10.0 * np.log10(value / reference):+.2f} "
            f"{np.log10(value / reference):+.3f}"
        )

    print("\nadjacent-state step estimates (dB) per link pair")
    all_steps: list[tuple[int, int, float]] = []
    for link, states in sorted(grouped.items()):
        present = sorted(states)
        for state in present:
            if state == 0 or state - 1 not in states:
                continue
            if len(states[state]) < args.min_count or len(states[state - 1]) < args.min_count:
                continue
            ratio = float(np.median(states[state])) / float(np.median(states[state - 1]))
            if ratio > 0:
                all_steps.append((state - 1, state, 10.0 * np.log10(ratio)))
    for a, b, step in sorted(all_steps, key=lambda x: (x[0], x[1])):
        print(f"{a}->{b} {step:+.2f} dB")
    if all_steps:
        steps = np.asarray([s for _, _, s in all_steps])
        print(
            f"median_step_db={np.median(steps):.2f} "
            f"mean_step_db={np.mean(steps):.2f} "
            f"std_step_db={np.std(steps):.2f} n={len(steps)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
