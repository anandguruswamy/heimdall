"""Baseline tests (plan Phase 4): backprojection localization, ellipsoid
voting candidates, and a fast 1-surfel gt_perturbed fit convergence.

Scene evidence is hardware-faithful (i16 quantization at the median
accumulation count 108), which bounds envelope-only localization: measured
DAS argmax within ~0.3-0.5 m (the volume is near-flat around the truth
with quantization-flattened shells), voting candidates within ~0.8 m,
while the per-scene optimizer recovers the surfel to <0.10 m. Tolerances
below reflect these measured limits (recorded in DECISIONS.md).
"""

from __future__ import annotations

import numpy as np
import torch

from nrecon.baselines.backprojection import (
    GridSpec,
    backproject,
    los_subtract,
    peak_xyz,
)
from nrecon.baselines.ellipsoid_voting import extract_peaks, vote
from nrecon.baselines.fit_scene import FitConfig, fit_scene, init_gt_perturbed
from nrecon.constants import directed_links
from nrecon.seeding import seed_all
from nrecon.sim.export import build_scene_pipeline
from nrecon.sim.primitives import SURFEL
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.sim.quantize import from_i16
from nrecon.sim.scenes import (
    LIVE_GEOMETRY,
    PrimitiveSpec,
    SceneSpec,
    rotation_from_normal,
    spec_to_scene,
)

import json

V1_SCENE_CFG = {
    "stage": 1, "scenes": 1, "node_mode": "fixed_live", "jitter_m": 0.0,
    "room": {"x_range": [4, 6], "y_range": [4, 6], "z_max": [2.4, 3.0],
             "partitions": [0, 0], "planes": False},
    "surfels": {"count": [0, 0]},
    "furniture": {"count": [0, 0]}, "people": {"count": [0, 0]}, "hw": {},
}


def _well_conditioned_surfel(seed: int) -> SceneSpec:
    data = json.loads(LIVE_GEOMETRY.read_text(encoding="utf-8"))
    rows = sorted(data["nodes"], key=lambda r: r["node_id"])
    nodes = np.asarray([r["position_m"] for r in rows], dtype=np.float64)
    rng = np.random.Generator(np.random.PCG64(seed ^ 0x51AB))
    for _ in range(64):
        mu = np.array([rng.uniform(0.6, 2.2), rng.uniform(0.5, 2.2),
                       rng.uniform(0.4, 1.4)])
        if min(np.linalg.norm(mu - n) for n in nodes) >= 0.8:
            break
    p = PrimitiveSpec(SURFEL, mu, rotation_from_normal(np.array([0.0, 0.0, 1.0])),
                      np.array([0.3, 0.3, 0.3]), 1.1 + 0.3j, 0.7)
    return SceneSpec(room_id=seed, layout_id=seed, stage=1, seed=seed,
                     nodes=nodes, primitives=[p])


def _scene_targets(seed: int):
    kernel = torch.as_tensor(
        correlation_kernel(make_template_v1(), make_template_v1()).samples)
    spec = _well_conditioned_surfel(seed)
    truth = spec_to_scene(spec)
    rec = build_scene_pipeline(spec, V1_SCENE_CFG, kernel)
    target = from_i16(rec["cir_i16"], rec["dgc"], rec["accum"])
    fp = rec["fp_aligned"].astype(np.float64)
    gain = 10.0 ** ((rec["dgc"].astype(np.float64) - 3.0) * 2.65 / 20.0)
    gain_accum = gain / np.maximum(1, rec["accum"].astype(np.float64))
    nodes = torch.as_tensor(spec.nodes, dtype=torch.float64)
    links = directed_links(5)
    return kernel, truth, rec, target, fp, gain_accum, nodes, links


def test_backprojection_localizes_surfel():
    seed_all(71)
    kernel, truth, rec, target, fp, gain_accum, nodes, links = _scene_targets(2000)
    env = np.abs(los_subtract(target, kernel.numpy(), fp))
    grid = GridSpec(-0.5, 3.5, -0.5, 3.2, 0.0, 2.2, spacing=0.15)
    res = backproject(env, nodes.numpy(), links, fp, grid)
    assert res.peaks, "no DAS peaks"
    mu = truth.center[0].numpy()
    pos = peak_xyz(res.peaks[0])
    assert np.linalg.norm(pos - mu) < 0.50, (pos, mu)


def test_ellipsoid_voting_finds_candidate_near_surfel():
    seed_all(72)
    kernel, truth, rec, target, fp, gain_accum, nodes, links = _scene_targets(2001)
    env = np.abs(los_subtract(target, kernel.numpy(), fp))
    peaks = extract_peaks(env, fp, min_excess_taps=1.0)
    assert len(peaks) > 0
    candidates = vote(peaks, nodes.numpy(), links, fp)
    assert candidates.shape[0] > 0
    mu = truth.center[0].numpy()
    dists = np.linalg.norm(candidates - mu, axis=-1)
    assert dists.min() < 1.00, (dists, mu)


def test_fit_scene_converges_on_trivial_surfel():
    seed_all(73)
    kernel, truth, rec, target, fp, gain_accum, nodes, links = _scene_targets(2002)
    rng = np.random.Generator(np.random.PCG64(9))
    init = init_gt_perturbed(truth, rng, pos_m=0.05, rot=0.05)
    cfg = FitConfig(iterations=200, lr=1e-2, envelope_first_frac=0.6)
    res = fit_scene(
        torch.as_tensor(target, dtype=torch.complex128),
        torch.as_tensor(fp, dtype=torch.float64),
        nodes, kernel, init, cfg, gain_accum=gain_accum,
    )
    c_fit = res.scene.center[0].detach().numpy()
    c_true = truth.center[0].numpy()
    # evidence-limited: quantized echo delays pin the center to ~0.1-0.15 m
    assert np.linalg.norm(c_fit - c_true) < 0.15
    assert res.runtime_s < 120
