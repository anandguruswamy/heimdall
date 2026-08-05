"""Network-facing input pipeline (paper Sec. IV), shared between synthetic
and real data.

Synthetic path: `from_i16` scaling (Eq. (7)), fractional alignment of the
first path to marker F0 = 16 (Eq. (8)), common-phase removal against the
direct-path template with robust per-link amplitude normalization (Eq. (9)),
then the three sample channels of Eq. (10) plus the metadata vector and
geometry features of Eq. (11). Real-data source (Phase 8): the live UNO Q
aligned/fitted CIR is accepted as-is with its fit metadata.
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


def metadata_vector(fp_aligned: np.ndarray, dgc: np.ndarray, accum: np.ndarray,
                    cfo: np.ndarray, t_in_cycle: np.ndarray,
                    link_valid: np.ndarray, log_gain: np.ndarray = None) -> torch.Tensor:
    """Scalar metadata [..., L, M]: marker f_ij, log a_ij, DGC, accum, CFO,
    quality flags, normalized observation time, missing-link mask (paper
    Sec. IV-B)."""
    m_fp = torch.as_tensor(fp_aligned - F0_MARKER, dtype=torch.float32)[..., None]
    if log_gain is None:
        m_gain = torch.zeros_like(m_fp)
    else:
        m_gain = torch.as_tensor(np.log(log_gain), dtype=torch.float32)[..., None]
    m_dgc = torch.as_tensor(dgc, dtype=torch.float32)[..., None] / 16.0
    m_accum = torch.as_tensor(accum, dtype=torch.float32)[..., None] / 256.0
    m_cfo = torch.as_tensor(cfo, dtype=torch.float32)[..., None] / 100.0
    m_t = torch.as_tensor(t_in_cycle, dtype=torch.float32)[..., None] / 35e-3
    m_valid = torch.as_tensor(~link_valid, dtype=torch.float32)[..., None]
    return torch.cat([m_fp, m_gain, m_dgc, m_accum, m_cfo, m_t, m_valid], dim=-1)


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
