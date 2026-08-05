"""Loss unit tests (plan Phase 6 step 1)."""

from __future__ import annotations

import numpy as np
import torch

from nrecon.constants import directed_links
from nrecon.seeding import seed_all
from nrecon.sim.primitives import CAPSULE, PLANE, SURFEL
from nrecon.sim.primitives import rot6d_to_matrix
from nrecon.train.losses import (
    LossWeights,
    match_slots,
    regularizers,
    render_losses,
    rotation_distance_so3,
    set_loss,
    symmetry_aware_rotation_distance,
    total_loss,
)


def _pred(g: int = 48, b: int = 1):
    return {
        "type_logits": torch.zeros(b, g, 4, dtype=torch.float64),
        "presence": torch.zeros(b, g, 1, dtype=torch.float64),
        "center": torch.randn(b, g, 3, dtype=torch.float64),
        "rot6d": torch.randn(b, g, 6, dtype=torch.float64),
        "scale_log": torch.randn(b, g, 3, dtype=torch.float64),
        "rho": torch.randn(b, g, 2, dtype=torch.float64),
        "roughness": torch.zeros(b, g, 1, dtype=torch.float64),
        "atten": torch.zeros(b, g, 1, dtype=torch.float64),
        "dynamic": torch.zeros(b, g, 1, dtype=torch.float64),
        "log_var_center": torch.full((b, g, 3), -3.0, dtype=torch.float64),
        "log_var_scale": torch.full((b, g, 3), -3.0, dtype=torch.float64),
        "log_var_rot": torch.full((b, g, 3), -3.0, dtype=torch.float64),
    }


def _truth(g: int = 48):
    return {
        "prim_type": torch.zeros(g, dtype=torch.long),
        "prim_present": torch.zeros(g, dtype=torch.float64),
        "prim_center": torch.zeros(g, 3, dtype=torch.float64),
        "prim_rot": torch.zeros(g, 3, 3, dtype=torch.float64),
        "prim_scale": torch.zeros(g, 3, dtype=torch.float64),
        "prim_rho": torch.zeros(g, 2, dtype=torch.float64),
    }


def test_rotation_symmetry_zero_distance():
    seed_all(91)
    r = rot6d_to_matrix(torch.randn(6, dtype=torch.float64))
    # plane rotated 180 deg about its normal (tangent flip) -> zero
    s = torch.diag(torch.tensor([-1.0, -1.0, 1.0], dtype=torch.float64))
    r2 = r @ s
    d = symmetry_aware_rotation_distance(
        r[None], r2[None], torch.tensor([PLANE], dtype=torch.long))
    assert float(d[0]) < 1e-9
    # surfel with axis sign flips -> zero
    s2 = torch.diag(torch.tensor([-1.0, 1.0, -1.0], dtype=torch.float64))
    d2 = symmetry_aware_rotation_distance(
        r[None], (r @ s2)[None], torch.tensor([SURFEL], dtype=torch.long))
    assert float(d2[0]) < 1e-9
    # capsule: axial rotation about the principal axis -> zero
    ax = torch.tensor([0.0, 0.0, 1.0], dtype=torch.float64)
    ang = torch.tensor(0.7)
    c = torch.cos(ang)
    s_ = torch.sin(ang)
    rax = torch.tensor([[c, -s_, 0.0], [s_, c, 0.0], [0.0, 0.0, 1.0]],
                       dtype=torch.float64)
    d3 = symmetry_aware_rotation_distance(
        rax[None], torch.eye(3, dtype=torch.float64)[None],
        torch.tensor([CAPSULE], dtype=torch.long))
    assert float(d3[0]) < 1e-9
    # rotation_distance_so3 is positive for a real rotation
    assert float(rotation_distance_so3(
        rax[None], torch.eye(3, dtype=torch.float64)[None])[0]) > 0.5


def test_matching_correctness_hand_built():
    seed_all(92)
    p = _pred()
    t = _truth()
    # two truths at known centers, type surfel
    t["prim_type"][0] = SURFEL
    t["prim_present"][0] = 1.0
    t["prim_center"][0] = torch.tensor([1.0, 0.0, 0.0])
    t["prim_type"][1] = SURFEL
    t["prim_present"][1] = 1.0
    t["prim_center"][1] = torch.tensor([-1.0, 0.0, 0.0])
    t["prim_rot"][0] = torch.eye(3, dtype=torch.float64)
    t["prim_rot"][1] = torch.eye(3, dtype=torch.float64)
    t["prim_scale"][0] = torch.tensor([0.3, 0.3, 0.3])
    t["prim_scale"][1] = torch.tensor([0.3, 0.3, 0.3])
    # preds at the same positions
    p["type_logits"][0, 0, SURFEL] = 5.0
    p["presence"][0, 0, 0] = 1.0
    p["center"][0, 0] = t["prim_center"][0]
    p["type_logits"][0, 1, SURFEL] = 5.0
    p["presence"][0, 1, 0] = 1.0
    p["center"][0, 1] = t["prim_center"][1]
    p["scale_log"][0, 0] = torch.log(t["prim_scale"][0])
    p["scale_log"][0, 1] = torch.log(t["prim_scale"][1])
    rows, cols = match_slots(p, t["prim_type"][None], t["prim_center"][None],
                             t["prim_rot"][None], t["prim_scale"][None],
                             t["prim_present"][None])
    matched = {int(r) for r in rows[0][rows[0] >= 0].tolist()}
    assert matched == {0, 1}


def test_set_loss_decreases_toward_truth():
    seed_all(93)
    p = _pred()
    t = _truth()
    t["prim_type"][0] = SURFEL
    t["prim_present"][0] = 1.0
    t["prim_center"][0] = torch.tensor([0.5, 0.2, 0.1])
    t["prim_rot"][0] = torch.eye(3, dtype=torch.float64)
    t["prim_scale"][0] = torch.tensor([0.3, 0.3, 0.3])
    t["prim_rho"][0] = torch.tensor([0.5, 0.2])
    p["type_logits"][0, 0, SURFEL] = 5.0
    p["presence"][0, 0, 0] = 1.0
    p["center"][0, 0] = t["prim_center"][0] + torch.tensor([0.3, 0.3, 0.3])
    p["scale_log"][0, 0] = torch.log(t["prim_scale"][0])
    rows, cols = match_slots(p, t["prim_type"][None], t["prim_center"][None],
                             t["prim_rot"][None], t["prim_scale"][None],
                             t["prim_present"][None])
    l_far = set_loss(p, t["prim_type"][None], t["prim_center"][None],
                     t["prim_rot"][None], t["prim_scale"][None],
                     t["prim_rho"][None], t["prim_present"][None], rows, cols)
    p["center"][0, 0] = t["prim_center"][0].clone()
    l_near = set_loss(p, t["prim_type"][None], t["prim_center"][None],
                      t["prim_rot"][None], t["prim_scale"][None],
                      t["prim_rho"][None], t["prim_present"][None], rows, cols)
    assert float(l_near) < float(l_far)


def test_losses_finite_masked_and_empty():
    seed_all(94)
    kernel = torch.randn(257, dtype=torch.float64)
    h = torch.randn(2, 20, 64, dtype=torch.complex128) * 0.01
    valid = torch.ones(2, 20, dtype=torch.bool)
    valid[0, :3] = False
    parts = render_losses(h, h + 0.001, valid)
    for v in parts.values():
        assert torch.isfinite(v)
    # empty scene: no truths -> matching trivially, losses finite
    p = _pred()
    t = _truth()
    rows, cols = match_slots(p, t["prim_type"][None], t["prim_center"][None],
                             t["prim_rot"][None], t["prim_scale"][None],
                             t["prim_present"][None])
    assert (rows == -1).all()
    ls = set_loss(p, t["prim_type"][None], t["prim_center"][None],
                  t["prim_rot"][None], t["prim_scale"][None], t["prim_rho"][None],
                  t["prim_present"][None], rows, cols)
    assert torch.isfinite(ls)
    assert torch.isfinite(regularizers(p))
    t_batch = {k: v[None] for k, v in t.items()}
    full = total_loss(p, t_batch, h, h, valid)
    assert torch.isfinite(full["total"])
