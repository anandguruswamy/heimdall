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
        # Real wall-clock step time from the "wall_s" column (seconds since
        # run start, logged every `log_every` steps); older runs predating
        # that column report NaN rather than the bogus log-interval count
        # this used to silently return.
        out["step_time_s"] = float("nan")
        out["steps_per_sec"] = float("nan")
        if rows and "wall_s" in rows[0] and len(rows) > 1:
            wall = np.array([r.get("wall_s", float("nan")) for r in rows])
            valid = np.nonzero(~np.isnan(wall))[0]
            if len(valid) > 1:
                i0, i1 = valid[0], valid[-1]
                dsteps = steps[i1] - steps[i0]
                dwall = wall[i1] - wall[i0]
                if dsteps > 0 and dwall > 0:
                    out["step_time_s"] = float(dwall / dsteps)
                    out["steps_per_sec"] = float(dsteps / dwall)
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
