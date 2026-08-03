#!/usr/bin/env python3
"""Report DGC state distribution and raw energy per state for capture clips."""

from __future__ import annotations

import argparse
import collections
from pathlib import Path

import numpy as np

from radar_map.capture import decode_capture


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    observations, stats = decode_capture(args.capture)
    dgc = collections.Counter()
    accum = collections.Counter()
    per_link = collections.defaultdict(collections.Counter)
    for item in observations:
        if item.obs_flags & 0x01 == 0:
            continue
        dgc[item.dgc_decision] += 1
        accum[item.accum_count] += 1
        per_link[(item.observed_node_id, item.reporting_node_id)][item.dgc_decision] += 1
    print(f"records={stats['records']} observations={len(observations)}")
    print("dgc:", dict(sorted(dgc.items())))
    print("accum:", dict(sorted(accum.items())))
    for link in sorted(per_link):
        states = {s: c for s, c in sorted(per_link[link].items())}
        print(f"  link {link[0]}>{link[1]}: {states}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
