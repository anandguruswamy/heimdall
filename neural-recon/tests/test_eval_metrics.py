"""Evaluation metrics unit tests (plan Phase 7 exit gate: "Metrics module
unit-tested (hand-built scenes with known errors)")."""

from __future__ import annotations

import numpy as np
import torch

from nrecon.eval.metrics import held_out_link_consistency, primitive_recovery_metrics
from nrecon.sim.primitives import CAPSULE, PLANE, SURFEL, rot6d_to_matrix


def _pred(g: int = 4, b: int = 1):
    return {
        "type_logits": torch.zeros(b, g, 4, dtype=torch.float64),
        "presence": torch.zeros(b, g, 1, dtype=torch.float64),
        "center": torch.zeros(b, g, 3, dtype=torch.float64),
        "rot6d": torch.tensor([1.0, 0.0, 0.0, 0.0, 1.0, 0.0], dtype=torch.float64
                              ).expand(b, g, 6).clone(),
        "scale_log": torch.zeros(b, g, 3, dtype=torch.float64),
    }


def _truth(g: int = 4, b: int = 1):
    return {
        "prim_type": torch.zeros(b, g, dtype=torch.long),
        "prim_present": torch.zeros(b, g, dtype=torch.float64),
        "prim_center": torch.zeros(b, g, 3, dtype=torch.float64),
        "prim_rot": torch.eye(3, dtype=torch.float64).expand(b, g, 3, 3).clone(),
        "prim_scale": torch.ones(b, g, 3, dtype=torch.float64) * 0.3,
    }


def test_primitive_recovery_perfect_match():
    p = _pred()
    t = _truth()
    t["prim_type"][0, 0] = SURFEL
    t["prim_present"][0, 0] = 1.0
    t["prim_center"][0, 0] = torch.tensor([1.0, 0.5, 0.2])
    p["type_logits"][0, 0, SURFEL] = 5.0
    p["presence"][0, 0, 0] = 1.0
    p["center"][0, 0] = t["prim_center"][0, 0].clone()
    p["scale_log"][0, 0] = torch.log(t["prim_scale"][0, 0])

    res = primitive_recovery_metrics(p, t)
    s = res.summary()
    assert s["n_truth"] == 1
    assert s["n_matched"] == 1
    assert s["type_accuracy"] == 1.0
    assert s["surfel_center_err_m"]["median"] < 1e-9
    assert s["surfel_cov_frobenius_err"]["median"] < 1e-9


def test_primitive_recovery_plane_known_normal_and_offset_error():
    p = _pred()
    t = _truth()
    t["prim_type"][0, 0] = PLANE
    t["prim_present"][0, 0] = 1.0
    t["prim_center"][0, 0] = torch.tensor([0.0, 0.0, 0.0])
    t["prim_rot"][0, 0] = torch.eye(3, dtype=torch.float64)  # normal = +z
    p["type_logits"][0, 0, PLANE] = 5.0
    p["presence"][0, 0, 0] = 1.0
    # Predicted plane offset by 0.2 m along the true normal (+z), and
    # its normal tilted 10 deg about the x-axis (rotate the z/y columns).
    p["center"][0, 0] = torch.tensor([0.0, 0.0, 0.2])
    theta = np.radians(10.0)
    c, s_ = np.cos(theta), np.sin(theta)
    tilted = torch.tensor([1.0, 0.0, 0.0, 0.0, c, s_], dtype=torch.float64)
    p["rot6d"][0, 0] = tilted  # columns: c1=x, c2=(0,cos,sin) -> normal c3 tilted 10 deg from z

    res = primitive_recovery_metrics(p, t)
    s = res.summary()
    assert s["n_matched"] == 1
    assert abs(s["plane_offset_err_m"]["median"] - 0.2) < 1e-6
    assert abs(s["plane_normal_err_deg"]["median"] - 10.0) < 1e-3


def test_primitive_recovery_capsule_known_size_error():
    p = _pred()
    t = _truth()
    t["prim_type"][0, 0] = CAPSULE
    t["prim_present"][0, 0] = 1.0
    t["prim_scale"][0, 0] = torch.tensor([0.6, 0.15, 0.15])  # halflen, radius, radius
    p["type_logits"][0, 0, CAPSULE] = 5.0
    p["presence"][0, 0, 0] = 1.0
    p["center"][0, 0] = t["prim_center"][0, 0].clone()
    p["scale_log"][0, 0] = torch.log(torch.tensor([0.5, 0.20, 0.20]))  # halflen -0.1, radius +0.05

    res = primitive_recovery_metrics(p, t)
    s = res.summary()
    assert abs(s["capsule_halflen_err_m"]["median"] - 0.1) < 1e-6
    assert abs(s["capsule_radius_err_m"]["median"] - 0.05) < 1e-6


def test_primitive_recovery_false_positive_and_missed_truth():
    p = _pred(g=4)
    t = _truth(g=4)
    # One truth primitive, unmatched by any confident prediction (all
    # slots empty-type / low presence) -> recall should be 0.
    t["prim_type"][0, 0] = SURFEL
    t["prim_present"][0, 0] = 1.0
    t["prim_center"][0, 0] = torch.tensor([2.0, 2.0, 2.0])
    # A confident but spurious prediction far from any truth -> false positive.
    p["type_logits"][0, 1, SURFEL] = 5.0
    p["presence"][0, 1, 0] = 0.9
    p["center"][0, 1] = torch.tensor([-5.0, -5.0, -5.0])

    res = primitive_recovery_metrics(p, t)
    s = res.summary()
    assert s["n_truth"] == 1
    assert s["n_matched"] == 1  # Hungarian still matches something (empty vs truth)
    assert s["n_pred_unmatched"] == 1  # the confident spurious slot elsewhere


def test_held_out_link_consistency_isolates_masked_links():
    b, l, taps = 1, 4, 8
    target = torch.zeros(b, l, taps, dtype=torch.complex128)
    h_hat = torch.zeros(b, l, taps, dtype=torch.complex128)
    # Links 0,1 match exactly; links 2,3 have a large *magnitude* mismatch
    # (render_losses is phase-invariant by design, so a pure sign/phase
    # flip is correctly treated as equivalent -- only an amplitude
    # difference shows up in either the envelope or complex term).
    target[0, 2] = 1.0
    h_hat[0, 2] = 5.0
    target[0, 3] = 2.0
    h_hat[0, 3] = 8.0

    mask_good = torch.tensor([[True, True, False, False]])
    mask_bad = torch.tensor([[False, False, True, True]])

    good = held_out_link_consistency(h_hat, target, mask_good)
    bad = held_out_link_consistency(h_hat, target, mask_bad)
    assert good.summary()["envelope_err"]["median"] < 1e-6
    assert bad.summary()["envelope_err"]["median"] > 0.5


def test_held_out_link_consistency_empty_mask():
    b, l, taps = 2, 4, 8
    target = torch.zeros(b, l, taps, dtype=torch.complex128)
    h_hat = torch.zeros(b, l, taps, dtype=torch.complex128)
    mask = torch.zeros(b, l, dtype=torch.bool)
    res = held_out_link_consistency(h_hat, target, mask)
    s = res.summary()
    assert s["envelope_err"]["n"] == 0
