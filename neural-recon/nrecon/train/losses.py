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
    d = torch.zeros(r1.shape[:-2], dtype=r1.dtype, device=r1.device)
    eye = torch.eye(3, dtype=r1.dtype, device=r1.device)
    signs = [torch.diag(torch.as_tensor([a, b, c], dtype=r1.dtype, device=r1.device))
             for a in (1.0, -1.0) for b in (1.0, -1.0) for c in (1.0, -1.0)]
    plane_group = [torch.eye(3, dtype=r1.dtype, device=r1.device),
                   torch.diag(torch.as_tensor([-1.0, -1.0, 1.0], dtype=r1.dtype,
                                              device=r1.device)),
                   torch.diag(torch.as_tensor([-1.0, 1.0, -1.0], dtype=r1.dtype,
                                              device=r1.device)),
                   torch.diag(torch.as_tensor([1.0, -1.0, -1.0], dtype=r1.dtype,
                                              device=r1.device))]
    for t in (PLANE, SURFEL, CAPSULE):
        mask = (prim_type == t)
        if not mask.any():
            continue
        if t == CAPSULE:
            ax1 = r1[mask][:, :, 2]
            ax2 = r2[mask][:, :, 2]
            cosv = (ax1 * ax2).sum(dim=-1).abs().clamp(-1.0, 1.0)
            d[mask] = torch.acos(cosv)
            continue
        group = plane_group if t == PLANE else signs
        best = torch.full((int(mask.sum()),), float("inf"), dtype=r1.dtype,
                          device=r1.device)
        for s in group:
            err = (eye[None, :, :] - r1[mask].transpose(-1, -2) @ r2[mask] @ s[None]).norm(
                dim=(-2, -1))
            best = torch.minimum(best, err)
        d[mask] = best
    return d


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
    rows, cols = [], []
    for bi in range(b):
        t_idx = torch.nonzero(truth_present[bi] > 0.5).squeeze(-1)
        nt = t_idx.numel()
        if nt == 0:
            rows.append(torch.full((g,), -1, dtype=torch.long, device=device))
            cols.append(torch.full((g,), -1, dtype=torch.long, device=device))
            continue
        tc = truth_center[bi, t_idx]
        tr = truth_rot[bi, t_idx]
        ts = truth_scale[bi, t_idx]
        tt = truth_type[bi, t_idx]
        pc = pred["center"][bi]
        pr = rot6d_to_matrix(pred["rot6d"][bi])
        ps = torch.exp(pred["scale_log"][bi])
        cost = torch.zeros(g, nt, dtype=torch.float64, device=device)
        cost += w.type * (pred["type_logits"][bi].argmax(-1)[:, None] != tt[None, :]).double()
        cost += w.center * torch.linalg.vector_norm(pc[:, None, :] - tc[None, :, :], dim=-1)
        cost += w.scale * (ps[:, None, :] - ts[None, :, :]).abs().sum(dim=-1)
        cost += w.rot * symmetry_aware_rotation_distance(
            pr[:, None, :, :].expand(g, nt, 3, 3),
            tr[None, :, :, :].expand(g, nt, 3, 3),
            pred["type_logits"][bi].argmax(-1)[:, None].expand(g, nt))
        # A NaN/Inf prediction (e.g. a transient instability early in
        # training, especially right after a curriculum warm-start into a
        # new dataset) makes scipy's linear_sum_assignment raise
        # "cost matrix is infeasible" and crash the whole run instead of
        # letting RunMonitor's loss-based NaN/degenerate checks handle it
        # gracefully. Sanitize to a large-but-finite cost so matching
        # always succeeds; the (likely garbage) match for that step gets
        # caught by the normal loss/degenerate monitoring instead.
        cost = torch.nan_to_num(cost, nan=1e6, posinf=1e6, neginf=1e6)
        # scipy needs a CPU array regardless of the compute device.
        rr, cc = linear_sum_assignment(cost.detach().cpu().numpy())
        rows_full = torch.full((g,), -1, dtype=torch.long, device=device)
        rows_full[torch.as_tensor(rr, dtype=torch.long, device=device)] = \
            torch.as_tensor(cc, dtype=torch.long, device=device)
        cols_full = torch.full((g,), -1, dtype=torch.long, device=device)
        matched = rows_full >= 0
        cols_full[matched] = t_idx[rows_full[matched]]
        rows.append(rows_full)
        cols.append(cols_full)
    return torch.stack(rows), torch.stack(cols)


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
    t_type = empty.clone()
    t_type[matched] = truth_type[
        torch.arange(b, device=device)[:, None].expand(b, g)[matched],
        cols[matched]]
    loss = loss + torch.nn.functional.cross_entropy(
        type_logits.reshape(-1, 4), t_type.reshape(-1), reduction="mean")

    # presence BCE: matched -> 1, unmatched preds -> 0
    p_target = matched.to(type_logits.dtype)
    loss = loss + torch.nn.functional.binary_cross_entropy(
        pred["presence"].squeeze(-1), p_target, reduction="mean")

    # center NLL with predicted sigma (bounded log-variance)
    if matched.any():
        mc = cols[matched]
        bi = torch.arange(b, device=device)[:, None].expand(b, g)[matched]
        e = (pred["center"][matched] - truth_center[bi, mc]).pow(2).sum(-1)
        lv = pred["log_var_center"][matched].sum(-1)
        nll = 0.5 * (e * torch.exp(-lv) + lv)
        loss = loss + nll.mean()

    # rotation (symmetry-aware) over matched slots
    if matched.any():
        pr = rot6d_to_matrix(pred["rot6d"][matched])
        tr = truth_rot[bi, mc]
        tt = truth_type[bi, mc]
        rd = symmetry_aware_rotation_distance(pr, tr, tt)
        loss = loss + rd.mean()

    # log-scale L1 and rho L1 over matched slots
    if matched.any():
        s_err = (pred["scale_log"][matched] -
                 torch.log(truth_scale[bi, mc].clamp(min=1e-9))).abs().sum(-1)
        loss = loss + s_err.mean()
        rho_err = (pred["rho"][matched] - truth_rho[bi, mc]).abs().sum(-1)
        loss = loss + rho_err.mean()
    return loss


def _phase_invariant_complex_loss(h_hat: torch.Tensor, h: torch.Tensor,
                                  eps: float = 1e-3) -> torch.Tensor:
    phi = torch.angle((h_hat.conj() * h).sum(dim=-1, keepdim=True))
    diff = h - h_hat * torch.exp(1j * phi)
    return torch.sqrt(diff.abs() ** 2 + eps**2).mean()


def render_losses(h_hat: torch.Tensor, h: torch.Tensor,
                  link_valid: torch.Tensor) -> dict:
    """Eq. (25) complex, Eq. (26) envelope, and 64-point FFT terms.

    Taps before the first-path guard are down-weighted for the scene terms
    but kept for noise calibration (Sec. VII-B).
    """
    w = torch.ones_like(h)
    w[..., :FIRST_PATH_GUARD_TAPS] = 0.1
    hh = h_hat * w
    ht = h * w
    cpx = _phase_invariant_complex_loss(hh, ht)
    env = (torch.log(ENV_EPS + ht.abs()) -
           torch.log(ENV_EPS + hh.abs())).abs().mean()
    fft_h = torch.fft.fft(hh, dim=-1).abs()
    fft_t = torch.fft.fft(ht, dim=-1).abs()
    fft_term = (fft_h - fft_t).abs().mean()
    return {"cpx": cpx, "env": env, "fft": fft_term}


def regularizers(pred: dict) -> torch.Tensor:
    """Sec. VII-C subset: occupied-slot, giant-surfel, near-zero-thickness,
    plane-overlap, uncertainty-prior."""
    loss = torch.zeros((), dtype=pred["center"].dtype, device=pred["center"].device)
    loss = loss + pred["presence"].mean()  # occupied-slot penalty
    s = torch.exp(pred["scale_log"])
    loss = loss + 0.1 * (s - 2.0).relu().mean()  # giant-surfel
    loss = loss + 0.1 * (0.02 - s).relu().mean()  # near-zero-thickness
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
    loss = loss + 0.01 * (pred["log_var_center"] - lv0).pow(2).mean()
    return loss


def total_loss(pred: dict, truth, h_hat: torch.Tensor, h: torch.Tensor,
               link_valid: torch.Tensor, w: LossWeights = None) -> dict:
    """Compose Eq. (27). Per-part gradient-norm balancing is done by the
    trainer (running EMA of per-part grad norms scaling the weights)."""
    if w is None:
        w = LossWeights()
    rows, cols = match_slots(pred, truth["prim_type"], truth["prim_center"],
                             truth["prim_rot"], truth["prim_scale"],
                             truth["prim_present"])
    parts = {
        "set": set_loss(pred, truth["prim_type"], truth["prim_center"],
                        truth["prim_rot"], truth["prim_scale"], truth["prim_rho"],
                        truth["prim_present"], rows, cols),
        **render_losses(h_hat, h, link_valid),
        "reg": regularizers(pred),
    }
    total = (w.set_ * parts["set"] + w.cpx * parts["cpx"]
             + w.env * parts["env"] + w.fft * parts["fft"]
             + w.reg * parts["reg"])
    parts["total"] = total
    return parts
