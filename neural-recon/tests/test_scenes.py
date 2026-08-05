"""Scene sampler tests (plan Phase 3)."""

from __future__ import annotations

import numpy as np

from nrecon.constants import G_MAX
from nrecon.seeding import seed_all
from nrecon.sim.primitives import CAPSULE, PLANE, SURFEL
from nrecon.sim.scenes import (
    _MAX_SEPARATION,
    _MIN_SEPARATION,
    PrimitiveSpec,
    _sample_room,
    _sample_room_tilt,
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
    for p in spec.primitives:
        assert np.all(np.isfinite(p.center))
        assert np.allclose(p.rot.T @ p.rot, np.eye(3), atol=1e-9)
        assert abs(np.linalg.det(p.rot) - 1.0) < 1e-9
        assert np.all(p.scale > 0)

    # Room wall/partition plane count specifically (a full sample_scene()
    # also mixes in furniture-table planes, which share type == PLANE, so
    # this must be checked against _sample_room()'s own output).
    room_rng = np.random.Generator(np.random.PCG64(9001))
    room_planes = _sample_room(room_rng, STAGE3_CFG["room"], 5.0, 5.0, 2.5)
    assert 6 <= len(room_planes) <= 8


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


def test_random_node_placement_convention():
    """User-specified real deployment convention (2026-08-05): nodes 0/1/2
    share one height, node 3 sits higher, node 4 is typically lower; every
    pairwise distance stays within [_MIN_SEPARATION, _MAX_SEPARATION]."""
    rng = np.random.Generator(np.random.PCG64(7001))
    node4_below_012 = 0
    n_trials = 60
    for _ in range(n_trials):
        nodes = sample_nodes(rng, {}, mode="random")
        z = nodes[:, 2]
        assert abs(z[0] - z[1]) < 1e-9
        assert abs(z[1] - z[2]) < 1e-9
        assert z[3] > z[0]  # node 3 higher than the shared 0/1/2 height
        if z[4] < z[0]:
            node4_below_012 += 1
        dist = np.linalg.norm(nodes[:, None, :] - nodes[None, :, :], axis=-1)
        pair_d = dist[np.triu_indices(5, k=1)]
        assert pair_d.min() >= _MIN_SEPARATION - 1e-9
        assert pair_d.max() <= _MAX_SEPARATION + 1e-9
    # "typically lower": node 4 should be below the 0/1/2 height more often
    # than not (not a hard per-draw constraint, so check the aggregate).
    assert node4_below_012 > n_trials * 0.5


def test_room_tilt_disabled_by_default_enabled_when_configured():
    seed_all(67)
    cfg_no_tilt = dict(STAGE3_CFG["room"])
    room_rng = np.random.Generator(np.random.PCG64(1))
    r_identity = _sample_room_tilt(room_rng, cfg_no_tilt.get("tilt_deg"))
    assert np.allclose(r_identity, np.eye(3))

    # With tilt_deg configured, the room's wall normals should no longer be
    # exactly axis-aligned (the whole point of the tilt).
    cfg_tilt = dict(STAGE3_CFG)
    cfg_tilt["room"] = dict(STAGE3_CFG["room"])
    cfg_tilt["room"]["tilt_deg"] = [2.0, 5.0]
    spec = sample_scene(2002, cfg_tilt)
    planes = [p for p in spec.primitives if p.type == PLANE]
    axis_normals = [np.eye(3)[:, 2] * s for s in (1.0, -1.0)] + \
        [np.eye(3)[:, i] * s for i in (0, 1) for s in (1.0, -1.0)]
    off_axis = 0
    for p in planes:
        normal = p.rot[:, 2]
        if not any(np.allclose(normal, a, atol=1e-6) for a in axis_normals):
            off_axis += 1
    assert off_axis > 0

    # Rotations must stay valid (orthonormal, det +1) after tilting.
    for p in planes:
        assert np.allclose(p.rot.T @ p.rot, np.eye(3), atol=1e-9)
        assert abs(np.linalg.det(p.rot) - 1.0) < 1e-9


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
