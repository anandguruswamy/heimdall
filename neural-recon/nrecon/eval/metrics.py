"""Evaluation metrics (plan Phase 7 step 1).

Scope note (2026-08-05, PROVISIONAL/pragmatic first pass): implements
primitive-recovery metrics (type accuracy, plane normal/offset error,
surfel center/covariance error, capsule center/size error) and a
held-out-link physical-consistency check. Matching reuses the Phase 6
Hungarian cost (`nrecon.train.losses.match_slots`) per the plan. Deferred
for a later pass: extent IoU, surface Chamfer distance, voxel occupancy
IoU, path-delay-vs-privileged-tables, cross-link consistency, and
uncertainty-calibration reliability diagrams -- see
`reports/N7-evaluation.md` for what was actually run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import torch

from nrecon.sim.primitives import CAPSULE, PLANE, SURFEL, rot6d_to_matrix
from nrecon.train.losses import LossWeights, match_slots, render_losses


def _covariance_from_rot_scale(rot: torch.Tensor, scale: torch.Tensor) -> torch.Tensor:
    """Sigma = R diag(s^2) R^T (paper Eq. (16)), batched over leading dims.

    `rot` [..., 3, 3] full rotation matrices; `scale` [..., 3] linear
    (not log) half-extents.
    """
    s2 = scale.clamp(min=1e-6) ** 2
    return rot @ torch.diag_embed(s2) @ rot.transpose(-1, -2)


@dataclass
class PrimitiveRecoveryResult:
    n_truth: int = 0
    n_matched: int = 0
    n_pred_unmatched: int = 0  # predicted "false positives" (presence>=0.5, unmatched)
    type_correct: int = 0  # of matched pairs, predicted argmax type == truth type
    plane_normal_err_deg: list = field(default_factory=list)
    plane_offset_err_m: list = field(default_factory=list)
    surfel_center_err_m: list = field(default_factory=list)
    surfel_cov_frobenius_err: list = field(default_factory=list)
    capsule_center_err_m: list = field(default_factory=list)
    capsule_halflen_err_m: list = field(default_factory=list)
    capsule_radius_err_m: list = field(default_factory=list)

    @property
    def type_accuracy(self) -> float:
        return self.type_correct / self.n_matched if self.n_matched else float("nan")

    @property
    def recall(self) -> float:
        return self.n_matched / self.n_truth if self.n_truth else float("nan")

    def summary(self) -> dict:
        def _stat(vals):
            if not vals:
                return {"median": float("nan"), "mean": float("nan"), "n": 0}
            arr = np.asarray(vals)
            return {"median": float(np.median(arr)), "mean": float(arr.mean()), "n": len(arr)}

        return {
            "n_truth": self.n_truth,
            "n_matched": self.n_matched,
            "n_pred_unmatched": self.n_pred_unmatched,
            "recall": self.recall,
            "type_accuracy": self.type_accuracy,
            "plane_normal_err_deg": _stat(self.plane_normal_err_deg),
            "plane_offset_err_m": _stat(self.plane_offset_err_m),
            "surfel_center_err_m": _stat(self.surfel_center_err_m),
            "surfel_cov_frobenius_err": _stat(self.surfel_cov_frobenius_err),
            "capsule_center_err_m": _stat(self.capsule_center_err_m),
            "capsule_halflen_err_m": _stat(self.capsule_halflen_err_m),
            "capsule_radius_err_m": _stat(self.capsule_radius_err_m),
        }


def primitive_recovery_metrics(pred: dict, truth: dict,
                               presence_threshold: float = 0.5) -> PrimitiveRecoveryResult:
    """Primitive-recovery metrics over a batch (plan Phase 7 step 1).

    `pred` is the network's `split_heads` output [B, G, ...]; `truth` has
    `prim_type/prim_present/prim_center/prim_rot/prim_scale` [B, G, ...].
    Matching is the Phase 6 Hungarian cost (`match_slots`); "false
    positive" predicted slots are presence>=`presence_threshold` and
    unmatched to any truth primitive.
    """
    rows, cols = match_slots(pred, truth["prim_type"], truth["prim_center"],
                             truth["prim_rot"], truth["prim_scale"],
                             truth["prim_present"])
    result = PrimitiveRecoveryResult()
    b, g = rows.shape
    pred_type = pred["type_logits"].argmax(-1)
    pred_rot = rot6d_to_matrix(pred["rot6d"])
    pred_scale = torch.exp(pred["scale_log"])

    for bi in range(b):
        n_truth_bi = int((truth["prim_present"][bi] > 0.5).sum())
        result.n_truth += n_truth_bi
        matched = rows[bi] >= 0
        result.n_matched += int(matched.sum())
        pred_present_mask = (pred["presence"][bi, :, 0] >= presence_threshold)
        result.n_pred_unmatched += int((pred_present_mask & ~matched).sum())

        for gi in torch.nonzero(matched).squeeze(-1).tolist():
            ti = int(cols[bi, gi])
            tt = int(truth["prim_type"][bi, ti])
            pt = int(pred_type[bi, gi])
            if pt == tt:
                result.type_correct += 1
            if tt == PLANE:
                n_fit = pred_rot[bi, gi, :, 2]
                n_truth = truth["prim_rot"][bi, ti, :, 2]
                cosv = (n_fit * n_truth).sum().abs().clamp(0.0, 1.0)
                ang = float(torch.rad2deg(torch.acos(cosv)))
                c_fit = pred["center"][bi, gi]
                c_truth = truth["prim_center"][bi, ti]
                off = float(((n_truth * c_fit).sum() - (n_truth * c_truth).sum()).abs())
                result.plane_normal_err_deg.append(ang)
                result.plane_offset_err_m.append(off)
            elif tt == SURFEL:
                c_fit = pred["center"][bi, gi]
                c_truth = truth["prim_center"][bi, ti]
                result.surfel_center_err_m.append(
                    float(torch.linalg.vector_norm(c_fit - c_truth)))
                sigma_fit = _covariance_from_rot_scale(pred_rot[bi, gi], pred_scale[bi, gi])
                sigma_truth = _covariance_from_rot_scale(
                    truth["prim_rot"][bi, ti], truth["prim_scale"][bi, ti])
                result.surfel_cov_frobenius_err.append(
                    float(torch.linalg.matrix_norm(sigma_fit - sigma_truth)))
            elif tt == CAPSULE:
                c_fit = pred["center"][bi, gi]
                c_truth = truth["prim_center"][bi, ti]
                result.capsule_center_err_m.append(
                    float(torch.linalg.vector_norm(c_fit - c_truth)))
                result.capsule_halflen_err_m.append(
                    float((pred_scale[bi, gi, 0] - truth["prim_scale"][bi, ti, 0]).abs()))
                result.capsule_radius_err_m.append(
                    float((pred_scale[bi, gi, 1] - truth["prim_scale"][bi, ti, 1]).abs()))
    return result


@dataclass
class HeldOutLinkResult:
    complex_err: list = field(default_factory=list)
    envelope_err: list = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "complex_err": {"median": float(np.median(self.complex_err)) if self.complex_err else float("nan"),
                            "mean": float(np.mean(self.complex_err)) if self.complex_err else float("nan"),
                            "n": len(self.complex_err)},
            "envelope_err": {"median": float(np.median(self.envelope_err)) if self.envelope_err else float("nan"),
                             "mean": float(np.mean(self.envelope_err)) if self.envelope_err else float("nan"),
                             "n": len(self.envelope_err)},
        }


def held_out_link_consistency(h_hat: torch.Tensor, target: torch.Tensor,
                              held_out_mask: torch.Tensor) -> dict:
    """Physical-consistency check (plan Phase 7 step 1): complex/envelope
    error of the rendered *predicted* scene against the target CIR,
    restricted to links withheld from the network's input (`held_out_mask`
    [B, L] bool). Requires the caller to have run the network with those
    links masked out of `link_valid` but still render+compare on them
    (network input masking and this evaluation mask are independent)."""
    b = h_hat.shape[0]
    result = HeldOutLinkResult()
    for bi in range(b):
        mask = held_out_mask[bi]
        if not mask.any():
            continue
        parts = render_losses(h_hat[bi:bi + 1, mask], target[bi:bi + 1, mask],
                              torch.ones(1, int(mask.sum()), dtype=torch.bool))
        result.complex_err.append(float(parts["cpx"]))
        result.envelope_err.append(float(parts["env"]))
    return result
