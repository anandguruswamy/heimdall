"""Network-facing input pipeline (paper Sec. IV), shared between synthetic
and real data.

Synthetic path: `from_i16` scaling (Eq. (7)), fractional alignment of the
first path to marker F0 = 16 (Eq. (8)), common-phase removal against the
direct-path template with robust per-link amplitude normalization (Eq. (9)),
then the three sample channels of Eq. (10) plus geometry features of
Eq. (11). Real-data source (Phase 8): the live UNO Q aligned/fitted CIR is
accepted as-is (its own gain/phase/timing fit already performs the
alignment this module does for synthetic data).

DEVIATION (2026-08-05, user directive; see DECISIONS.md): the paper's
per-link scalar metadata (marker offset, log-gain, DGC, accum, CFO,
observation time, missing-link flag) is dropped as a network input.
Rationale: DGC/accum/CFO are hardware-transport artifacts with no
equivalent in the real live-fitted CIR (see DECISIONS.md); the marker
offset is already fully consumed by the alignment step above (every CIR,
synthetic or real, is re-centered to the same fixed reference before the
network ever sees it) and its raw value carries residual real-hardware fit
error the network cannot interpret; the missing-link flag duplicates the
`link_valid` mask already passed separately for attention masking. Network
inputs are therefore CIR channels (`preprocess_cirs`) plus geometry
(`geometry_features`) only.
"""

from __future__ import annotations

import numpy as np
import torch

from nrecon.constants import F0_MARKER, S_TAPS
from nrecon.sim.delay import fractional_shift
from nrecon.sim.quantize import from_i16

ENVELOPE_EPS = 1e-3  # log-magnitude floor (paper Eq. (10))


def preprocess_cirs(cir_i16: np.ndarray, dgc: np.ndarray, accum: np.ndarray,
                    fp_aligned: np.ndarray, kernel: torch.Tensor,
                    normalize: bool = True) -> torch.Tensor:
    """Scaled, marker-aligned, phase-normalized CIRs -> [..., L, 64, 3].

    `cir_i16` [..., L, 64, 2]; `fp_aligned` [..., L] is the stored total
    alignment (marker + hardware peak offset). The LOS is brought to the
    fixed marker F0 = 16 and the direct-path template phase is removed
    (Eq. (9)); `normalize=False` skips the amplitude normalization.
    """
    h = torch.as_tensor(from_i16(cir_i16, dgc, accum))
    shift = torch.as_tensor(F0_MARKER - fp_aligned, dtype=torch.float64)
    h_al = fractional_shift(h, shift)

    if normalize:
        # Eq. (9): remove the direct-path template phase, normalize by a
        # robust per-link amplitude (template = kernel placed at the marker)
        n = torch.arange(S_TAPS, dtype=torch.float64)
        peak = float(torch.argmax(kernel)) / 16.0
        template = sample_kernel_1d(kernel, n - F0_MARKER + peak)
        template = template / template.norm()
        corr = (h_al * template).sum(dim=-1, keepdim=True)  # [..., L, 1]
        phase = torch.angle(corr)
        amp = corr.abs().clamp(min=1e-9)
        h_norm = h_al * torch.exp(-1j * phase) / amp
    else:
        h_norm = h_al

    x = torch.stack([
        h_norm.real, h_norm.imag,
        torch.log(ENVELOPE_EPS + h_norm.abs()),
    ], dim=-1)
    return x.to(torch.float32)


def sample_kernel_1d(kernel: torch.Tensor, offsets: torch.Tensor) -> torch.Tensor:
    from nrecon.sim.delay import sample_kernel

    return sample_kernel(kernel, offsets)


def geometry_features(node_pos: np.ndarray, links) -> torch.Tensor:
    """Eq. (11): [p_i', p_j', p_j'-p_i', |p_j'-p_i'|, s_p] -> [L, 11] (or
    [B, L, 11]) in link order. `node_pos` is [N, 3] or [B, N, 3]."""
    p = torch.as_tensor(node_pos, dtype=torch.float32)
    batched = p.ndim == 3
    if not batched:
        p = p.unsqueeze(0)
    b, n, _ = p.shape
    centroid = p.mean(dim=1, keepdim=True)
    p_c = p - centroid
    baseline = p[:, :, None, :] - p[:, None, :, :]  # [B, N, N, 3]
    lengths = torch.linalg.vector_norm(baseline, dim=-1)
    s_p = torch.sqrt((lengths**2).mean())
    p_n = p_c / s_p
    g = torch.zeros(b, n, n, 11, dtype=torch.float32)
    g[..., 0:3] = p_n[:, :, None, :]
    g[..., 3:6] = p_n[:, None, :, :]
    g[..., 6:9] = baseline / s_p
    g[..., 9] = lengths
    g[..., 10] = s_p
    rows = torch.as_tensor([a for a, _ in links], dtype=torch.long)
    cols = torch.as_tensor([bb for _, bb in links], dtype=torch.long)
    out = g[:, rows, cols]  # [B, L, 11]
    if not batched:
        out = out.squeeze(0)
    return out
