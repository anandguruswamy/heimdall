"""Sparse ellipsoid voting baseline (paper Sec. IX).

Per-link envelope peaks define bistatic ellipsoid shells; each shell is
sampled by rays from the transmitter, votes accumulate in a coarse voxel
hash, and the top cells become candidate points (also used as a per-scene
optimizer initializer).
"""

from __future__ import annotations

import numpy as np

from nrecon.constants import METRES_PER_TAP


def extract_peaks(env: np.ndarray, fp_taps: np.ndarray,
                  min_excess_taps: float = 4.0,
                  threshold_rel: float = 0.25) -> list:
    """Local envelope maxima beyond the first-path marker region.

    The default 4.0-tap exclusion clears the LOS main lobe and sidelobes
    (comparable in amplitude to quantized echoes). Returns
    [(link, tap, amplitude)].
    """
    peaks = []
    for li in range(env.shape[0]):
        e = env[li]
        lo = max(1, int(np.ceil(fp_taps[li] + min_excess_taps)))
        if lo >= e.shape[0] - 1:
            continue
        threshold = threshold_rel * float(np.max(e))
        for t in range(lo, e.shape[0] - 1):
            if e[t] >= threshold and e[t] >= e[t - 1] and e[t] >= e[t + 1]:
                peaks.append((li, t, float(e[t])))
    return peaks


def _shell_points(tx: np.ndarray, rx: np.ndarray, excess_m: float,
                  directions: np.ndarray) -> np.ndarray:
    """Points on the bistatic ellipsoid shell along `directions` from tx.

    Total path R = d + excess; along ray u: t = (R^2 - d^2) / (2 (R - u.b)).
    """
    baseline = rx - tx
    d = float(np.linalg.norm(baseline))
    if d < 1e-9:
        return np.zeros((0, 3))
    u = baseline / d
    r = d + excess_m
    dot = directions @ u
    denom = 2.0 * (r - dot * d)
    t = (r * r - d * d) / np.where(np.abs(denom) < 1e-9, 1e-9, denom)
    valid = (np.abs(denom) >= 1e-9) & (t > 0)
    t = t[valid]
    dirs = directions[valid]
    return tx + t[:, None] * dirs


def vote(peaks: list, nodes: np.ndarray, links, fp_taps: np.ndarray,
         cell_m: float = 0.25, n_az: int = 16, n_el: int = 8,
         n_candidates: int = 4) -> np.ndarray:
    """Vote peak ellipsoid shells into a coarse voxel hash.

    Returns candidate [K, 3] world positions (x, y, z), best first.
    """
    lo = nodes.min(axis=0) - 2.0
    hi = nodes.max(axis=0) + 2.0
    dims = np.ceil((hi - lo) / cell_m).astype(int) + 1
    votes = np.zeros(tuple(dims), dtype=np.float64)

    az = np.linspace(0.0, 2.0 * np.pi, n_az, endpoint=False)
    el = np.linspace(-np.pi / 2.0 + 0.15, np.pi / 2.0 - 0.15, n_el)
    directions = []
    for a in az:
        for e in el:
            directions.append(np.array([
                np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e),
            ]))
    directions = np.asarray(directions)

    for (li, tap, amp) in peaks:
        tx_i, rx_i = links[li]
        excess = (tap - fp_taps[li]) * METRES_PER_TAP
        if excess < 0.5 * METRES_PER_TAP:
            continue
        pts = _shell_points(nodes[tx_i], nodes[rx_i], excess, directions)
        for p in pts:
            idx = np.floor((p - lo) / cell_m).astype(int)
            if np.all(idx >= 0) and np.all(idx < dims):
                votes[tuple(idx)] += amp

    flat = votes.ravel()
    order = np.argsort(flat)[::-1]
    candidates = []
    min_dist = 1.5 * cell_m
    for idx in order:
        if flat[idx] <= 0:
            break
        z, y, x = np.unravel_index(idx, votes.shape)
        pos = lo + np.array([x, y, z]) * cell_m + cell_m / 2.0
        if all(np.linalg.norm(pos - c) >= min_dist for c in candidates):
            candidates.append(pos)
            if len(candidates) >= n_candidates:
                break
    if not candidates:
        return np.zeros((0, 3))
    return np.stack(candidates)
