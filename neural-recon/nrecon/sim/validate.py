"""Dataset validation CLI: `python -m nrecon.sim.validate <dataset_dir>`.

Checks: array schema/dtypes/shapes per shard; manifest completeness;
bit-exact determinism (rebuild 5 scenes by seed); bit-exact re-render
consistency (re-render 5 scenes from stored labels + nuisance); split
disjointness by room seed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import torch
import yaml

from nrecon.constants import G_MAX, N_NODES, S_TAPS
from nrecon.sim.export import (
    SHARD_SIZE,
    build_scene_pipeline,
    nuisance_from_record,
    read_shard,
    render_record,
    spec_from_record,
    split_for_room,
)
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.sim.scenes import sample_scene

SCHEMA = {
    "cir_i16": (np.int16, (None, 20, S_TAPS, 2)),
    "link_valid": (np.bool_, (None, 20)),
    "fp_q10_6": (np.int32, (None, 20)),
    "fp_aligned": (np.float32, (None, 20)),
    "cir_start": (np.int32, (None, 20)),
    "dgc": (np.int8, (None, 20)),
    "accum": (np.int16, (None, 20)),
    "cfo": (np.float32, (None, 20)),
    "t_in_cycle": (np.float32, (None, 20)),
    "node_pos": (np.float32, (None, N_NODES, 3)),
    "prim_type": (np.int8, (None, G_MAX)),
    "prim_present": (np.float32, (None, G_MAX)),
    "prim_center": (np.float32, (None, G_MAX, 3)),
    "prim_rot": (np.float32, (None, G_MAX, 3, 3)),
    "prim_scale": (np.float32, (None, G_MAX, 3)),
    "prim_rho": (np.float32, (None, G_MAX, 2)),
    "prim_rough": (np.float32, (None, G_MAX)),
    "prim_atten": (np.float32, (None, G_MAX)),
    "prim_dynamic": (np.float32, (None, G_MAX)),
    "link_gain": (np.float32, (None, 20)),
    "link_phase": (np.float32, (None, 20)),
    "noise_std": (np.float32, (None, 20)),
    "resid_fir": (np.float32, (None, 20, 5, 2)),
    "reverb_tail": (np.float32, (None, 20, S_TAPS, 2)),
}

MANIFEST_KEYS = ("index", "scene_seed", "layout_index", "room_id", "layout_id",
                 "stage", "config_hash", "generator_git_rev",
                 "pulse_manifest_hash", "split")

ERRORS = []


def _err(msg: str) -> None:
    ERRORS.append(msg)
    print(f"ERROR: {msg}")


def _kernel():
    t = make_template_v1()
    return torch.as_tensor(correlation_kernel(t, t).samples)


def _load(root: Path):
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    manifest = []
    for mf in sorted(root.glob("shard-*.manifest.jsonl")):
        for line in mf.read_text(encoding="utf-8").splitlines():
            manifest.append(json.loads(line))
    shards = {}
    for npz in sorted(root.glob("shard-*.npz")):
        shards[npz.stem] = read_shard(npz)
    return cfg, manifest, shards


def check_schema(shards) -> None:
    for name, shard in shards.items():
        for key, (dtype, shape) in SCHEMA.items():
            if key not in shard:
                _err(f"{name}: missing {key}")
                continue
            arr = shard[key]
            if arr.dtype != dtype:
                _err(f"{name}: {key} dtype {arr.dtype} != {dtype}")
            if arr.ndim != len(shape) or any(
                s is not None and arr.shape[i] != s for i, s in enumerate(shape)
            ):
                _err(f"{name}: {key} shape {arr.shape} != {shape}")


def check_manifest_completeness(manifest, shards) -> None:
    total_records = sum(sh["cir_i16"].shape[0] for sh in shards.values())
    if total_records != len(manifest):
        _err(f"manifest has {len(manifest)} lines but shards hold {total_records} records")
    for line in manifest:
        for key in MANIFEST_KEYS:
            if key not in line:
                _err(f"manifest line {line.get('index')}: missing {key}")
        if line.get("split") not in ("train", "val", "test"):
            _err(f"manifest line {line.get('index')}: bad split {line.get('split')}")
    # split disjointness by room seed
    splits = {}
    for line in manifest:
        splits.setdefault(line["split"], set()).add(line["room_id"])
    for a in ("train", "val", "test"):
        for b in ("train", "val", "test"):
            if a < b and splits.get(a, set()) & splits.get(b, set()):
                _err(f"split overlap between {a} and {b}: {splits.get(a, set()) & splits.get(b, set())}")


def _record_at(shards, index: int) -> dict:
    shard = shards[f"shard-{index // SHARD_SIZE:06d}"]
    row = index % SHARD_SIZE
    return {k: v[row] for k, v in shard.items()}


def check_determinism(cfg, manifest, shards, kernel, n: int = 5) -> None:
    import numpy.random as npr

    seeds = [line["scene_seed"] for line in manifest[:n]]
    for seed in seeds:
        line = [l for l in manifest if l["scene_seed"] == seed][0]
        spec = sample_scene(seed, cfg, layout_index=line["layout_index"])
        rec = build_scene_pipeline(spec, cfg, kernel)
        stored = _record_at(shards, line["index"])
        if not np.array_equal(rec["cir_i16"], stored["cir_i16"]):
            _err(f"determinism failed for seed {seed}")
        if not np.array_equal(rec["fp_q10_6"], stored["fp_q10_6"]):
            _err(f"determinism failed for seed {seed} (metadata)")


def check_consistency(cfg, manifest, shards, kernel, n: int = 5) -> None:
    for line in manifest[:n]:
        stored = _record_at(shards, line["index"])
        spec = spec_from_record(stored, line["scene_seed"])
        nuis = nuisance_from_record(stored)
        rec = render_record(spec, nuis, kernel, noise_seed=line["scene_seed"])
        if not np.array_equal(rec["cir_i16"], stored["cir_i16"]):
            bad = int(np.count_nonzero(rec["cir_i16"] != stored["cir_i16"]))
            _err(f"consistency failed for index {line['index']} ({bad} taps differ)")
        if not np.array_equal(rec["link_valid"], stored["link_valid"]):
            _err(f"consistency failed for index {line['index']} (link_valid)")


def main(argv: list = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit("usage: python -m nrecon.sim.validate <dataset_dir>")
    root = Path(argv[0])
    cfg, manifest, shards = _load(root)
    print(f"validating {root}: {len(shards)} shards, {len(manifest)} records")
    check_schema(shards)
    check_manifest_completeness(manifest, shards)
    kernel = _kernel()
    check_determinism(cfg, manifest, shards, kernel)
    check_consistency(cfg, manifest, shards, kernel)
    if ERRORS:
        print(f"FAILED: {len(ERRORS)} errors")
        sys.exit(1)
    print("PASSED: schema, manifest, determinism, consistency, splits")


if __name__ == "__main__":
    main()
