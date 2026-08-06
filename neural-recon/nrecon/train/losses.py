"""Hybrid objective (paper Sec. VII): permutation-invariant set loss,
CIR reconstruction losses, and regularizers, with the PROVISIONAL initial
weights and running-grad-norm normalization.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
from scipy.optimize import linear_sum_assignment

from nrecon.constants import S_TAPS
from nrecon.sim.primitives import CAPSULE, PLANE, SURFEL, rot6d_to_matrix

ENV_EPS = 1e-3
FIRST_PATH_GUARD_TAPS = 5  # pre-first-path taps down-weighted in scene terms
EMPTY_SLOT_WEIGHT = 0.1  # DETR-style no-object down-weighting
CENTER_LOG_VAR_MIN = float(np.log(0.05))
UNCERTAINTY_PRIOR_WEIGHT = 1.0


@dataclass
class LossWeights:
    set_: float = 1.0
    cpx: float = 1.0
    env: float = 0.5
    fft: float = 0.1
    surf: float = 0.25
    reg: float = 0.01


@dataclass
class MatchWeights:
    type: float = 1.0
    center: float = 1.0
    scale: float = 0.5
    rot: float = 0.5


def rotation_distance_so3(r1: torch.Tensor, r2: torch.Tensor) -> torch.Tensor:
    """Frobenius distance to the identity of R1^T R2 -> [0, ~2.8]."""
    m = r1.transpose(-1, -2) @ r2
    return (torch.eye(3, dtype=r1.dtype, device=r1.device) - m).norm(dim=(-2, -1))


def symmetry_aware_rotation_distance(r1: torch.Tensor, r2: torch.Tensor,
                                      prim_type: torch.Tensor) -> torch.Tensor:
    """Minimum SO(3) distance over the primitive's symmetry group.

    Planes: 4 diag-sign rotations (patch invariance). Surfels: 8 diag-sign
    flips (covariance invariance). Capsules: axial rotations ignored ->
    angle between principal axes.
    """
    eye = torch.eye(3, dtype=r1.dtype, device=r1.device)
    signs = torch.stack([
        torch.diag(torch.as_tensor([a, b, c], dtype=r1.dtype, device=r1.device))
        for a in (1.0, -1.0) for b in (1.0, -1.0) for c in (1.0, -1.0)
    ])
    plane_group = signs[[0, 6, 5, 3]]

    def grouped_distance(group: torch.Tensor) -> torch.Tensor:
        relative = r1.transpose(-1, -2)[..., None, :, :] @ \
            (r2[..., None, :, :] @ group)
        return (eye - relative).norm(dim=(-2, -1)).min(dim=-1).values

    plane = grouped_distance(plane_group)
    surfel = grouped_distance(signs)
    # Keep acos backward finite even when the capsule branch is not selected:
    # torch.where's zero upstream gradient can otherwise meet acos'(1)=inf and
    # produce NaN through 0*inf.
    cosv = (r1[..., :, 2] * r2[..., :, 2]).sum(dim=-1).abs().clamp(
        1e-7, 1.0 - 1e-7)
    capsule = (torch.acos(cosv) - np.arccos(1.0 - 1e-7)).clamp(min=0.0)
    distance = torch.zeros_like(plane)
    distance = torch.where(prim_type == PLANE, plane, distance)
    distance = torch.where(prim_type == SURFEL, surfel, distance)
    return torch.where(prim_type == CAPSULE, capsule, distance)


def match_slots(pred: dict, truth_type: torch.Tensor, truth_center: torch.Tensor,
                truth_rot: torch.Tensor, truth_scale: torch.Tensor,
                truth_present: torch.Tensor, w: MatchWeights = None) -> tuple:
    """Detached Hungarian matching (Eq. (23)): cost of (pred slot, truth).

    Returns (row, col) index arrays [B, K] (pred -> truth) with -1 for
    unmatched truths, computed per batch element.
    """
    if w is None:
        w = MatchWeights()
    device = pred["center"].device
    b, g = pred["center"].shape[:2]
    present_cpu = (truth_present.detach().cpu().numpy() > 0.5)
    truth_indices = [np.flatnonzero(present_cpu[bi]) for bi in range(b)]
    max_truth = max((idx.size for idx in truth_indices), default=0)
    if max_truth > g:
        raise ValueError(
            f"model has {g} queries but batch contains {max_truth} primitives")
    rows_cpu = np.full((b, g), -1, dtype=np.int64)
    cols_cpu = np.full((b, g), -1, dtype=np.int64)
    if max_truth == 0:
        return (torch.as_tensor(rows_cpu, device=device),
                torch.as_tensor(cols_cpu, device=device))

    with torch.no_grad():
        pred_type = pred["type_logits"].argmax(-1)
        pred_rot = rot6d_to_matrix(pred["rot6d"]) if w.rot else None
        pred_scale = torch.exp(pred["scale_log"])
        costs = torch.full((b, g, max_truth), 1e6, dtype=torch.float64, device=device)
        for bi, truth_idx_cpu in enumerate(truth_indices):
            nt = truth_idx_cpu.size
            if nt == 0:
                continue
            truth_idx = torch.as_tensor(truth_idx_cpu, dtype=torch.long, device=device)
            tc = truth_center[bi, truth_idx]
            tr = truth_rot[bi, truth_idx]
            ts = truth_scale[bi, truth_idx]
            tt = truth_type[bi, truth_idx]
            cost = w.type * (pred_type[bi, :, None] != tt[None, :]).double()
            cost = cost + w.center * torch.linalg.vector_norm(
                pred["center"][bi, :, None, :] - tc[None, :, :], dim=-1)
            cost = cost + w.scale * (
                pred_scale[bi, :, None, :] - ts[None, :, :]).abs().sum(dim=-1)
            if w.rot:
                cost = cost + w.rot * symmetry_aware_rotation_distance(
                    pred_rot[bi, :, None, :, :].expand(g, nt, 3, 3),
                    tr[None, :, :, :].expand(g, nt, 3, 3),
                    pred_type[bi, :, None].expand(g, nt))
            costs[bi, :, :nt] = torch.nan_to_num(
                cost, nan=1e6, posinf=1e6, neginf=1e6)
        costs_cpu = costs.cpu().numpy()

    for bi, truth_idx in enumerate(truth_indices):
        nt = truth_idx.size
        if nt == 0:
            continue
        rr, cc = linear_sum_assignment(costs_cpu[bi, :, :nt])
        rows_cpu[bi, rr] = cc
        cols_cpu[bi, rr] = truth_idx[cc]
    return (torch.as_tensor(rows_cpu, device=device),
            torch.as_tensor(cols_cpu, device=device))


def set_loss(pred: dict, truth_type: torch.Tensor, truth_center: torch.Tensor,
             truth_rot: torch.Tensor, truth_scale: torch.Tensor,
             truth_rho: torch.Tensor, truth_present: torch.Tensor,
             rows: torch.Tensor, cols: torch.Tensor) -> torch.Tensor:
    """Eq. (24): type CE, presence BCE, center NLL, rotation, scale, rho."""
    b, g = pred["center"].shape[:2]
    type_logits = pred["type_logits"]
    device = type_logits.device
    loss = torch.zeros((), dtype=type_logits.dtype, device=device)

    # type CE: matched slots -> truth type; unmatched preds -> empty (0)
    empty = torch.zeros(b, g, dtype=torch.long, device=device)
    matched = rows >= 0
    safe_cols = cols.clamp(min=0)
    t_type = torch.gather(truth_type, 1, safe_cols)
    t_type = torch.where(matched, t_type, empty)
    type_weight = type_logits.new_ones(4)
    type_weight[0] = EMPTY_SLOT_WEIGHT
    loss = loss + torch.nn.functional.cross_entropy(
        type_logits.reshape(-1, 4), t_type.reshape(-1), weight=type_weight,
        reduction="mean")

    # presence BCE: matched -> 1, unmatched preds -> 0
    p_target = matched.to(type_logits.dtype)
    presence_loss = torch.nn.functional.binary_cross_entropy(
        pred["presence"].squeeze(-1), p_target, reduction="none")
    presence_weight = torch.where(
        matched, torch.ones_like(p_target),
        torch.full_like(p_target, EMPTY_SLOT_WEIGHT))
    loss = loss + (presence_loss * presence_weight).sum() / presence_weight.sum()

    def gather_truth(value: torch.Tensor) -> torch.Tensor:
        index = safe_cols[(...,) + (None,) * (value.ndim - 2)].expand(
            b, g, *value.shape[2:])
        return torch.gather(value, 1, index)

    weight = matched.to(type_logits.dtype)
    denominator = weight.sum().clamp(min=1.0)
    target_center = gather_truth(truth_center)
    geometry_mask = matched[..., None]
    pred_center = torch.where(geometry_mask, pred["center"], target_center)
    center_error = (pred_center - target_center).pow(2)
    log_var_center = torch.where(
        geometry_mask, pred["log_var_center"], torch.zeros_like(target_center))
    log_var_center = log_var_center.clamp(min=CENTER_LOG_VAR_MIN)
    center_nll = 0.5 * (
        center_error * torch.exp(-log_var_center) + log_var_center).sum(-1)
    loss = loss + (center_nll * weight).sum() / denominator

    tr = gather_truth(truth_rot)
    tt = torch.gather(truth_type, 1, safe_cols)
    identity6d = pred["rot6d"].new_tensor([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
    pred_rot6d = torch.where(
        matched[..., None], pred["rot6d"], identity6d)
    pr = rot6d_to_matrix(pred_rot6d)
    rd = symmetry_aware_rotation_distance(pr, tr, tt)
    loss = loss + (rd * weight).sum() / denominator

    target_scale = gather_truth(truth_scale)
    pred_scale_log = torch.where(
        geometry_mask, pred["scale_log"], torch.zeros_like(target_scale))
    target_scale_log = torch.where(
        geometry_mask, torch.log(target_scale.clamp(min=1e-9)),
        torch.zeros_like(target_scale))
    s_err = (pred_scale_log - target_scale_log).abs().sum(-1)
    loss = loss + (s_err * weight).sum() / denominator
    target_rho = gather_truth(truth_rho)
    pred_rho = torch.where(geometry_mask, pred["rho"], target_rho)
    rho_err = (pred_rho - target_rho).abs().sum(-1)
    loss = loss + (rho_err * weight).sum() / denominator
    return loss


def _phase_invariant_complex_loss(h_hat: torch.Tensor, h: torch.Tensor,
                                  eps: float = 1e-3) -> torch.Tensor:
    phi = torch.angle((h_hat.conj() * h).sum(dim=-1, keepdim=True))
    diff = h - h_hat * torch.exp(1j * phi)
    return torch.sqrt(diff.abs() ** 2 + eps**2).mean(dim=-1)


def render_losses(h_hat: torch.Tensor, h: torch.Tensor,
                  link_valid: torch.Tensor, full_valid_count=None,
                  sampling_probability: float = 1.0) -> dict:
    """Eq. (25) complex, Eq. (26) envelope, and 64-point FFT terms.

    Taps before the first-path guard are down-weighted for the scene terms
    but kept for noise calibration (Sec. VII-B).
    """
    w = torch.ones_like(h)
    w[..., :FIRST_PATH_GUARD_TAPS] = 0.1
    valid_links = link_valid.to(torch.bool)
    hh = torch.where(valid_links[..., None], h_hat * w, torch.zeros_like(h_hat))
    ht = torch.where(valid_links[..., None], h * w, torch.zeros_like(h))
    valid = valid_links.to(hh.real.dtype)
    if full_valid_count is None:
        denominator = valid.sum()
    else:
        denominator = torch.as_tensor(full_valid_count, dtype=valid.dtype,
                                      device=valid.device) * sampling_probability
    denominator = denominator.clamp(min=1.0)

    def reduce(per_link: torch.Tensor) -> torch.Tensor:
        return (per_link * valid).sum() / denominator

    cpx = reduce(_phase_invariant_complex_loss(hh, ht))
    env = reduce((torch.log(ENV_EPS + ht.abs()) -
                  torch.log(ENV_EPS + hh.abs())).abs().mean(dim=-1))
    fft_h = torch.fft.fft(hh, dim=-1).abs()
    fft_t = torch.fft.fft(ht, dim=-1).abs()
    fft_term = reduce((fft_h - fft_t).abs().mean(dim=-1))
    return {"cpx": cpx, "env": env, "fft": fft_term}


def regularizers(pred: dict) -> torch.Tensor:
    """Sec. VII-C subset: occupied-slot, giant-surfel, near-zero-thickness,
    plane-overlap, uncertainty-prior."""
    loss = torch.zeros((), dtype=pred["center"].dtype, device=pred["center"].device)
    loss = loss + pred["presence"].mean()  # occupied-slot penalty
    scale_log = pred["scale_log"]
    loss = loss + 0.1 * (scale_log - np.log(2.0)).relu().mean()  # giant-surfel
    loss = loss + 0.1 * (np.log(0.02) - scale_log).relu().mean()  # near-zero
    # plane-overlap repulsion
    idx = torch.arange(pred["center"].shape[1], device=pred["center"].device)
    mask = (pred["type_logits"].argmax(-1) == PLANE)
    if mask.sum() > 1:
        cm = pred["center"][mask]
        d2 = (cm[:, None, :] - cm[None, :, :]).pow(2).sum(-1)
        off = torch.triu(d2 < 0.25, diagonal=1).float()
        loss = loss + 0.05 * (off * torch.exp(-d2 / (2 * 0.5**2))).mean()
    # uncertainty prior: predicted log-vars near log(0.05)
    lv0 = torch.log(torch.as_tensor(0.05, dtype=pred["center"].dtype,
                                    device=pred["center"].device))
    loss = loss + UNCERTAINTY_PRIOR_WEIGHT * (
        pred["log_var_center"] - lv0).pow(2).mean()
    return loss


def total_loss(pred: dict, truth, h_hat: torch.Tensor, h: torch.Tensor,
               link_valid: torch.Tensor, w: LossWeights = None,
               return_matches: bool = False, full_valid_count=None,
               sampling_probability: float = 1.0,
               render_loss_scale: float = 1.0,
               match_weights: MatchWeights = None,
               set_loss_fn=None):
    """Compose Eq. (27). Per-part gradient-norm balancing is done by the
    trainer (running EMA of per-part grad norms scaling the weights)."""
    if w is None:
        w = LossWeights()
    rows, cols = match_slots(pred, truth["prim_type"], truth["prim_center"],
                             truth["prim_rot"], truth["prim_scale"],
                             truth["prim_present"], match_weights)
    if h_hat is None:
        zero = torch.zeros((), dtype=pred["center"].dtype, device=pred["center"].device)
        rendered = {"cpx": zero, "env": zero, "fft": zero}
    else:
        rendered = render_losses(h_hat, h, link_valid, full_valid_count,
                                 sampling_probability)
    set_loss_impl = set_loss if set_loss_fn is None else set_loss_fn
    parts = {
        "set": set_loss_impl(pred, truth["prim_type"], truth["prim_center"],
                             truth["prim_rot"], truth["prim_scale"],
                             truth["prim_rho"], truth["prim_present"], rows, cols),
        **rendered,
        "reg": regularizers(pred),
    }
    total = (w.set_ * parts["set"] + w.cpx * parts["cpx"]
             * render_loss_scale
             + w.env * parts["env"] * render_loss_scale
             + w.fft * parts["fft"] * render_loss_scale
             + w.reg * parts["reg"])
    parts["total"] = total
    if return_matches:
        return parts, (rows, cols)
    return parts
