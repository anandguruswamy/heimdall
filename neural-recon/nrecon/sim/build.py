"""Dataset build CLI: `python -m nrecon.sim.build --config <yaml> --out <dir>`.

Prints the measured scenes/sec and ETA up front; warns when the estimated
build exceeds 30 s.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import torch
import yaml

from nrecon.sim.export import build_dataset, config_hash
from nrecon.sim.pulse import correlation_kernel, make_template_v1


def _kernel():
    t = make_template_v1()
    return torch.as_tensor(correlation_kernel(t, t).samples)


def main(argv: list = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=None, help="mini-build scene count")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    kernel = _kernel()
    n_seeds = args.limit if args.limit is not None else int(cfg["scenes"])
    layouts = int(cfg.get("layouts_per_scene", 1))
    total = n_seeds * layouts

    probe = min(3, n_seeds)
    t0 = time.perf_counter()
    from nrecon.sim.export import build_scene_pipeline
    from nrecon.sim.scenes import sample_scene

    for i in range(probe):
        spec = sample_scene(int(cfg.get("base_seed", 0)) + i, cfg, layout_index=0)
        build_scene_pipeline(spec, cfg, kernel)
    rate = probe / max(time.perf_counter() - t0, 1e-9)
    eta = (total - probe) / rate
    print(f"stage {cfg['stage']}: {total} records ({n_seeds} scenes x {layouts} layouts)")
    print(f"measured rate: {rate:.1f} scenes/s -> ETA {eta:.0f}s")
    if eta > 30.0:
        print(f"WARNING: build exceeds 30 s (about {eta:.0f} s); continuing")

    manifest = build_dataset(cfg, Path(args.out), kernel, limit=args.limit)
    print(f"built {len(manifest)} records -> {args.out}")
    print(f"config hash: {config_hash(cfg)}")


if __name__ == "__main__":
    main()
