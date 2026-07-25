#!/usr/bin/env python3
"""Capture Phase 1 `CIR,...` UART records into a reproducible CSV file."""

from __future__ import annotations

import argparse
import csv
import math
import time
from pathlib import Path

import serial


TAP_COUNT = 64


def parse_record(line: str) -> dict[str, int | list[tuple[int, int]]] | None:
    if not line.startswith("CIR,"):
        return None
    fields = line.removeprefix("CIR,").strip().split(",")
    values: dict[str, str] = {}
    for field in fields:
        key, value = field.split("=", 1)
        values[key] = value
    taps = [tuple(map(int, tap.split(":"))) for tap in values.pop("taps").split(";")]
    if len(taps) != TAP_COUNT:
        raise ValueError(f"expected {TAP_COUNT} taps, got {len(taps)}")
    record: dict[str, int | list[tuple[int, int]]] = {
        key: int(value) for key, value in values.items()
    }
    record["taps"] = taps
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, int | list[tuple[int, int]]]] = []
    deadline = time.monotonic() + args.timeout
    with serial.Serial(args.port, 115200, timeout=0.25) as uart:
        while len(rows) < args.frames and time.monotonic() < deadline:
            line = uart.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            record = parse_record(line)
            if record is not None:
                rows.append(record)
                print(f"captured {len(rows)}/{args.frames}: seq={record['seq']}")

    if not rows:
        raise SystemExit("no CIR records received")

    metadata = [
        "seq", "rx_ts", "cfo_raw", "fp", "fp_raw", "diag_peak", "full_peak", "start"
    ]
    tap_fields = [name for i in range(TAP_COUNT) for name in (f"i{i}", f"q{i}")]
    with args.output.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=metadata + tap_fields)
        writer.writeheader()
        for record in rows:
            row = {key: record[key] for key in metadata}
            for index, (real, imag) in enumerate(record["taps"]):
                row[f"i{index}"] = real
                row[f"q{index}"] = imag
            writer.writerow(row)

    peak_offsets = []
    peak_to_lead = []
    peak_to_tail = []
    for record in rows:
        powers = [real * real + imag * imag for real, imag in record["taps"]]
        peak = max(range(TAP_COUNT), key=powers.__getitem__)
        lead = sum(powers[:8]) / 8
        tail = sum(powers[40:]) / 24
        peak_offsets.append(peak)
        peak_to_lead.append(10 * math.log10(max(powers[peak], 1) / max(lead, 1)))
        peak_to_tail.append(10 * math.log10(max(powers[peak], 1) / max(tail, 1)))
    print(
        f"saved {len(rows)} frames to {args.output}; "
        f"peak_rel={min(peak_offsets)}..{max(peak_offsets)}, "
        f"peak/lead={sum(peak_to_lead)/len(rows):.1f} dB, "
        f"peak/tail={sum(peak_to_tail)/len(rows):.1f} dB"
    )
    return 0 if len(rows) == args.frames else 2


if __name__ == "__main__":
    raise SystemExit(main())
