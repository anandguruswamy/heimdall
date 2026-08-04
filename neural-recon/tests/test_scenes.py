"""Scene sampler tests (plan Phase 3)."""

from __future__ import annotations

import numpy as np

from nrecon.constants import G_MAX
from nrecon.seeding import seed_all
from nrecon.sim.primitives import CAPSULE, PLANE, SURFEL
from nrecon.sim.scenes import (
    _MIN_SEPARATION,
    PrimitiveSpec,
    sample_scene,
    sample_nodes,
    spec_to_scene,
)

STAGE3_CFG = {
    "stage": 3,
    "scenes": 1,
    "node_mode": "fixed_live",
    "jitter_m": 0.03,
    "room": {"x_range": [3.0, 8.0], "y_range": [3.0, 8.0], "z_max": [2.2, 3.2],
             "partitions": [0, 2], "planes": True},
    "surfels": {"count": [1, 4]},
    "furniture": {"count": [0, 3]},
    "people": {"count": [0, 2], "height": [1.5, 1.95], "radius": [0.12, 0.22]},
    "hw": {},
}


def test_room_plane_count_and_sizes():
    seed_all(61)
    spec = sample_scene(1001, STAGE3_CFG)
    planes = [p for p in spec.primitives if p.type == PLANE]
    assert 4 <= len(planes) <= 8
    for p in spec.primitives:
        assert np.all(np.isfinite(p.center))
        assert np.allclose(p.rot.T @ p.rot, np.eye(3), atol=1e-9)
        assert abs(np.linalg.det(p.rot) - 1.0) < 1e-9
        assert np.all(p.scale > 0)


def test_sampler_bounds_and_gmax():
    seed_all(62)
    for seed in range(200, 206):
        spec = sample_scene(seed, STAGE3_CFG)
        assert len(spec.primitives) <= G_MAX
        for p in spec.primitives:
            assert np.all(np.isfinite(p.center))
            if p.type == CAPSULE:
                half_len = p.scale[0]
                radius = p.scale[1]
                assert 0.12 <= radius <= 0.22
                assert 0.3 < half_len < 1.0
                assert p.dynamic == 1.0
                assert p.atten >= 0.0


def test_scene_is_deterministic_and_nodes_valid():
    a = sample_scene(77, STAGE3_CFG)
    b = sample_scene(77, STAGE3_CFG)
    assert len(a.primitives) == len(b.primitives)
    for pa, pb in zip(a.primitives, b.primitives):
        assert np.array_equal(pa.center, pb.center)
        assert pa.rho == pb.rho
    assert np.array_equal(a.nodes, b.nodes)
    centered = a.nodes - a.nodes.mean(axis=0)
    sv = np.linalg.svd(centered, compute_uv=False)
    assert sv[-1] > 0.1
    for i in range(len(a.nodes)):
        for j in range(i + 1, len(a.nodes)):
            assert np.linalg.norm(a.nodes[i] - a.nodes[j]) >= _MIN_SEPARATION


def test_random_layouts_non_coplanar():
    seed_all(63)
    for seed in range(400, 410):
        cfg = dict(STAGE3_CFG)
        cfg["node_mode"] = "random"
        spec = sample_scene(seed, cfg)
        centered = spec.nodes - spec.nodes.mean(axis=0)
        sv = np.linalg.svd(centered, compute_uv=False)
        assert sv[-1] > 0.1, seed
        assert sv[1] > 0.5, seed


def test_multiple_layouts_share_scene_objects():
    seed_all(64)
    cfg = dict(STAGE3_CFG)
    cfg["node_mode"] = "random"
    a = sample_scene(501, cfg, layout_index=0)
    b = sample_scene(501, cfg, layout_index=1)
    assert not np.array_equal(a.nodes, b.nodes)
    assert len(a.primitives) == len(b.primitives)
    for pa, pb in zip(a.primitives, b.primitives):
        assert np.array_equal(pa.center, pb.center)


def test_spec_to_scene_roundtrip():
    seed_all(65)
    spec = sample_scene(31, STAGE3_CFG)
    scene = spec_to_scene(spec)
    assert int(scene.type_id.numel()) == G_MAX
    for g, p in enumerate(spec.primitives):
        assert int(scene.type_id[g]) == p.type
        assert float(scene.presence[g]) == 1.0
        r = scene.center[g].numpy()
        assert np.allclose(r, p.center)
        rec_rot6d = scene.rot6d[g].numpy()
        rec_rot = np.stack([rec_rot6d[:3], rec_rot6d[3:]], axis=-1)
        assert np.allclose(rec_rot, p.rot[:, :2], atol=1e-9)


def test_stage1_surfel_only_scene():
    seed_all(66)
    cfg = {
        "stage": 1, "scenes": 1, "node_mode": "fixed_live", "jitter_m": 0.0,
        "room": {"x_range": [4, 6], "y_range": [4, 6], "z_max": [2.4, 3.0],
                 "partitions": [0, 0], "planes": False},
        "surfels": {"count": [1, 3]},
        "furniture": {"count": [0, 0]}, "people": {"count": [0, 0]}, "hw": {},
    }
    for seed in range(900, 903):
        spec = sample_scene(seed, cfg)
        assert all(p.type == SURFEL for p in spec.primitives)
        assert 1 <= len(spec.primitives) <= 3
