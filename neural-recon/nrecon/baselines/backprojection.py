"""Dense delay-and-sum backprojection on synthetic tensors.

Same math as `host-tools/radar-map/radar_map/processing.py::backproject`,
reimplemented here against the synthetic schema (no cross-boundary import):
for each voxel and link, the excess path maps to a tap in the
first-path-aligned envelope; evidence is accumulated and normalized by
confidence. The direct path is removed per link by projecting out the
marker-aligned kernel template (paper Eq. (9) machinery), so the LOS main
lobe and sidelobes do not paint the baselines.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch

from nrecon.constants import METRES_PER_TAP
from nrecon.sim.delay import sample_kernel


@dataclass
class GridSpec:
    x0: float
    x1: float
    y0: float
    y1: float
    z0: float
    z1: float
    spacing: float

    def axes(self):
        x = np.arange(self.x0, self.x1 + self.spacing / 2, self.spacing)
        y = np.arange(self.y0, self.y1 + self.spacing / 2, self.spacing)
        z = np.arange(self.z0, self.z1 + self.spacing / 2, self.spacing)
        return x, y, z


@dataclass
class BackprojectionResult:
    volume: np.ndarray  # [Z, Y, X] zyx order
    confidence: np.ndarray
    x_m: np.ndarray
    y_m: np.ndarray
    z_m: np.ndarray
    peaks: list  # [(x_m, y_m, z_m, value)]


def envelope_from_record(cir_i16: np.ndarray, dgc: int, accum: int) -> np.ndarray:
    """Aligned envelope in the scaled pipeline domain (paper Eq. (7))."""
    from nrecon.sim.quantize import from_i16

    h = from_i16(cir_i16, dgc, accum)
    return np.abs(h)


def los_subtract(h_aligned: np.ndarray, kernel: np.ndarray,
                 fp_taps: np.ndarray) -> np.ndarray:
    """Remove the direct path per link by template projection.

    `h_aligned` [L, T] complex with the LOS at `fp_taps`; the template is
    the kernel placed at the marker. Returns the residual complex CIR.
    """
    peak = float(np.argmax(kernel)) / 16.0
    n = np.arange(h_aligned.shape[1], dtype=np.float64)
    out = np.empty_like(h_aligned)
    for l in range(h_aligned.shape[0]):
        t = sample_kernel(
            torch.as_tensor(kernel), torch.as_tensor(n - fp_taps[l] + peak)
        ).numpy()
        t = t / np.linalg.norm(t)
        a = np.vdot(t, h_aligned[l])  # complex projection coefficient
        out[l] = h_aligned[l] - a * t
    return out


def backproject(env: np.ndarray, nodes: np.ndarray, links, fp_taps: np.ndarray,
                grid: GridSpec, n_peaks: int = 8,
                los_exclude_taps: float = 1.0,
                tap_scale: float = 1.0) -> BackprojectionResult:
    """Delay-and-sum over the aligned residual envelopes.

    `env` [L, T] aligned so the LOS sits at `fp_taps[l]`; voxels map to
    envelope index `(fp_taps[l] + excess_m / METRES_PER_TAP) * tap_scale`
    (use `tap_scale=16` for a 16x-resampled envelope). Taps within
    `los_exclude_taps` of the marker are suppressed for the main-lobe
    residue after direct-path removal.
    """
    x_m, y_m, z_m = grid.axes()
    zz, yy, xx = np.meshgrid(z_m, y_m, x_m, indexing="ij")
    points = np.stack((xx, yy, zz), axis=-1)
    volume = np.zeros(xx.shape, dtype=np.float64)
    confidence = np.zeros(xx.shape, dtype=np.float64)
    taps = np.arange(env.shape[1], dtype=np.float64)
    for li, (tx, rx) in enumerate(links):
        p_tx = nodes[tx]
        p_rx = nodes[rx]
        direct = float(np.linalg.norm(p_tx - p_rx))
        excess = (
            np.linalg.norm(points - p_tx, axis=-1)
            + np.linalg.norm(points - p_rx, axis=-1)
            - direct
        )
        tap_idx = (fp_taps[li] + excess / METRES_PER_TAP) * tap_scale
        valid = (tap_idx >= 0) & (tap_idx <= env.shape[1] - 1)
        evidence = np.interp(tap_idx.ravel(), taps, env[li], left=0.0, right=0.0)
        evidence = evidence.reshape(volume.shape)
        evidence[excess < los_exclude_taps * METRES_PER_TAP] = 0.0
        weight = max(0.05, float(np.max(env[li])))
        volume += evidence * weight * valid
        confidence += weight * valid
    np.divide(volume, confidence, out=volume, where=confidence > 0)

    peaks = _extract_peaks(volume, grid, n_peaks)
    return BackprojectionResult(
        volume=volume.astype(np.float32), confidence=confidence.astype(np.float32),
        x_m=x_m, y_m=y_m, z_m=z_m, peaks=peaks,
    )


def _extract_peaks(volume: np.ndarray, grid: GridSpec, n_peaks: int) -> list:
    flat = volume.ravel()
    order = np.argsort(flat)[::-1]
    peaks = []  # (x_m, y_m, z_m, value)
    min_dist = 2 * grid.spacing
    for idx in order:
        if flat[idx] <= 0:
            break
        z, y, x = np.unravel_index(idx, volume.shape)
        pos = np.array([grid.x0 + x * grid.spacing, grid.y0 + y * grid.spacing,
                        grid.z0 + z * grid.spacing])
        if all(np.linalg.norm(pos - np.asarray(p[:3])) >= min_dist for p in peaks):
            peaks.append((float(pos[0]), float(pos[1]), float(pos[2]), float(flat[idx])))
            if len(peaks) >= n_peaks:
                break
    return peaks


def peak_xyz(peak: tuple) -> np.ndarray:
    return np.array(peak[:3], dtype=np.float64)
