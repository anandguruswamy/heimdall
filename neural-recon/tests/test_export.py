"""Dataset export/validation tests (plan Phase 3). Mini-builds of every
stage are validated in tmp dirs (seconds each)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import torch

from nrecon.sim.export import (
    SHARD_SIZE,
    build_dataset,
    read_shard,
    split_for_room,
    write_shard,
)
from nrecon.sim.pulse import correlation_kernel, make_template_v1

CONFIGS = {
    1: {
        "stage": 1, "scenes": 8, "base_seed": 0, "node_mode": "fixed_live",
        "jitter_m": 0.0, "layouts_per_scene": 1,
        "room": {"x_range": [4, 6], "y_range": [4, 6], "z_max": [2.4, 3.0],
                 "partitions": [0, 0], "planes": False},
        "surfels": {"count": [1, 3]}, "furniture": {"count": [0, 0]},
        "people": {"count": [0, 0]}, "hw": {},
    },
    2: {
        "stage": 2, "scenes": 8, "base_seed": 100000, "node_mode": "fixed_live",
        "jitter_m": 0.03, "layouts_per_scene": 1,
        "room": {"x_range": [3, 8], "y_range": [3, 8], "z_max": [2.2, 3.2],
                 "partitions": [0, 2], "planes": True},
        "surfels": {"count": [1, 4]}, "furniture": {"count": [0, 3]},
        "people": {"count": [0, 0]}, "hw": {},
    },
    3: {
        "stage": 3, "scenes": 8, "base_seed": 200000, "node_mode": "fixed_live",
        "jitter_m": 0.03, "layouts_per_scene": 1,
        "room": {"x_range": [3, 8], "y_range": [3, 8], "z_max": [2.2, 3.2],
                 "partitions": [0, 2], "planes": True},
        "surfels": {"count": [1, 4]}, "furniture": {"count": [0, 3]},
        "people": {"count": [0, 2], "height": [1.5, 1.95], "radius": [0.12, 0.22]},
        "hw": {"gain_db": [0.0, 1.5], "phase": True, "noise_std": [0.002, 0.6],
               "dgc": [3, 6], "accum": [40, 200], "cfo_ppm": [0.0, 10.0],
               "fp_jitter": 0.15, "peak_offset": [1.69, 0.2], "resid": 0.05,
               "missing_link_p": 0.02, "false_fp_p": 0.005},
    },
    4: {
        "stage": 4, "scenes": 8, "base_seed": 300000, "node_mode": "random",
        "jitter_m": 0.0, "layouts_per_scene": 2,
        "room": {"x_range": [3, 8], "y_range": [3, 8], "z_max": [2.2, 3.2],
                 "partitions": [0, 2], "planes": True},
        "surfels": {"count": [1, 4]}, "furniture": {"count": [0, 3]},
        "people": {"count": [0, 2], "height": [1.5, 1.95], "radius": [0.12, 0.22]},
        "hw": {"gain_db": [0.0, 1.5], "phase": True, "noise_std": [0.002, 0.6],
               "dgc": [3, 6], "accum": [40, 200], "cfo_ppm": [0.0, 10.0],
               "fp_jitter": 0.15, "peak_offset": [1.69, 0.2], "resid": 0.05,
               "missing_link_p": 0.02, "false_fp_p": 0.005},
    },
}


def _kernel():
    t = make_template_v1()
    return torch.as_tensor(correlation_kernel(t, t).samples)


def _build_mini(stage: int, tmp_path: Path) -> Path:
    out = tmp_path / f"stage{stage}"
    build_dataset(CONFIGS[stage], out, _kernel())
    return out


def test_mini_builds_validate_all_stages(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for stage in (1, 2, 3, 4):
        out = _build_mini(stage, tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m", "nrecon.sim.validate", str(out)],
            cwd=root, capture_output=True, text=True, timeout=600,
        )
        assert proc.returncode == 0, f"stage {stage}:\n{proc.stdout}\n{proc.stderr}"
        assert "PASSED" in proc.stdout


def test_shard_roundtrip(tmp_path):
    records = []
    manifest = []
    for i in range(3):
        cfg = CONFIGS[1]
        from nrecon.sim.export import build_scene_pipeline
        from nrecon.sim.scenes import sample_scene

        spec = sample_scene(i, cfg)
        rec = build_scene_pipeline(spec, cfg, _kernel())
        records.append(rec)
        manifest.append({"index": i, "scene_seed": i, "split": "train"})
    base = tmp_path / "shard-000000"
    write_shard(base, records, manifest)
    data = read_shard(base)
    assert data["cir_i16"].shape == (3, 20, 64, 2)
    assert data["cir_i16"].dtype == np.int16
    assert np.array_equal(data["cir_i16"][1], records[1]["cir_i16"])
    assert np.array_equal(data["node_pos"][0], records[0]["node_pos"])


def test_build_bit_identical_twice(tmp_path):
    out1 = _build_mini(1, tmp_path / "a")
    out2 = tmp_path / "b"
    build_dataset(CONFIGS[1], out2, _kernel())
    a = read_shard(out1 / "shard-000000")
    b = read_shard(out2 / "shard-000000")
    assert np.array_equal(a["cir_i16"], b["cir_i16"])
    assert np.array_equal(a["fp_q10_6"], b["fp_q10_6"])


def test_split_disjoint_by_room():
    rooms = {}
    for i in range(5000):
        rooms.setdefault(split_for_room(i), set()).add(i)
    assert not (rooms["train"] & rooms["val"])
    assert not (rooms["train"] & rooms["test"])
    assert not (rooms["val"] & rooms["test"])


def test_stage3_noise_and_nuisance_present(tmp_path):
    out = _build_mini(3, tmp_path)
    data = read_shard(out / "shard-000000")
    assert np.any(data["noise_std"] > 0)
    assert np.any(data["link_gain"] != 1.0)
    assert np.any(data["resid_fir"] != 0.0)
    assert data["dgc"].min() >= 3 and data["dgc"].max() <= 6
    assert data["accum"].min() >= 40 and data["accum"].max() <= 200
    # LOS marker near the 16-tap median on valid links
    fp = data["fp_q10_6"].astype(np.float64) / 64.0 - data["cir_start"]
    valid = data["link_valid"]
    assert np.median(fp[valid]) > 15.0


def test_missing_links_zeroed(tmp_path):
    out = _build_mini(3, tmp_path)
    data = read_shard(out / "shard-000000")
    missing = ~data["link_valid"]
    assert np.any(missing)
    assert np.all(data["cir_i16"][missing] == 0)
