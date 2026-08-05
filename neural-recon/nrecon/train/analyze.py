"""Training run analysis: `python -m nrecon.train.analyze <run_dir>`.

Reads metrics.csv + val.csv and prints a trajectory summary for the
monitor-and-decide loop (loss trend, matched-center trend, plateau/
degeneracy flags, step time).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np


def summarize(run_dir: str) -> dict:
    d = Path(run_dir)
    rows = []
    with open(d / "metrics.csv", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({k: float(v) for k, v in row.items() if v != ""})
    out = {}
    if rows:
        losses = np.array([r["loss"] for r in rows])
        centers = np.array([r["matched_center"] for r in rows if r["matched_center"] == r["matched_center"]])
        steps = np.array([r["step"] for r in rows])
        out["steps"] = int(steps[-1])
        out["loss_first"] = float(losses[0])
        out["loss_last"] = float(losses[-1])
        out["loss_min"] = float(losses.min())
        # trend over the last 25% of logged steps
        k = max(2, len(losses) // 4)
        tail = losses[-k:]
        out["loss_tail_slope"] = float(np.polyfit(range(k), tail, 1)[0])
        if len(centers):
            out["med_center_first"] = float(centers[0])
            out["med_center_last"] = float(centers[-1])
            out["med_center_min"] = float(np.nanmin(centers))
        out["step_time_s"] = float(
            (steps[-1] - steps[0]) / max(1, (len(steps) - 1) * max(1e-9, 1))) if len(steps) > 1 else float("nan")
    val_rows = []
    vf = d / "val.csv"
    if vf.exists():
        with open(vf, newline="") as f:
            for row in csv.DictReader(f):
                if row["val_loss"]:
                    val_rows.append({k: float(v) for k, v in row.items() if v != ""})
    if val_rows:
        out["val_loss_last"] = float(val_rows[-1]["val_loss"])
        out["val_med_center_last"] = float(val_rows[-1]["val_med_center"])
    return out


def main(argv: list = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit("usage: python -m nrecon.train.analyze <run_dir>")
    s = summarize(argv[0])
    for k, v in s.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
