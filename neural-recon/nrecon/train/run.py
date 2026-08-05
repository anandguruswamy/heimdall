"""Training CLI: `python -m nrecon.train.run --config <yaml> [--out runs]`."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

from nrecon.train.loop import TrainConfig, train


def main(argv: list = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="runs")
    ap.add_argument("--epochs", type=int, default=None)
    ap.add_argument("--device", default=None,
                    help="override config device, e.g. cpu, cuda, cuda:0")
    ap.add_argument("--max-minutes", type=float, default=None,
                    help="override config max_minutes (wall-clock cap)")
    args = ap.parse_args(argv)

    raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.epochs:
        raw["epochs"] = args.epochs
    if args.device:
        raw["device"] = args.device
    if args.max_minutes is not None:
        raw["max_minutes"] = args.max_minutes
    cfg = TrainConfig(**raw)
    rev = "unknown"
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True).stdout.strip()
    except Exception:
        pass
    (Path(args.out) / cfg.name).mkdir(parents=True, exist_ok=True)
    with open(Path(args.out) / cfg.name / "run-manifest.json", "w",
              encoding="utf-8") as f:
        json.dump({"config": raw, "git_rev": rev}, f, indent=2)
    result = train(cfg, out_dir=args.out)
    print(f"done: {result['steps']} steps, final loss {result['final_loss']:.4f}")


if __name__ == "__main__":
    main()
