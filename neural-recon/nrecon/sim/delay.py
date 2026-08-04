"""Differentiable fractional-delay operator D_delta (paper Eq. (8)).

Finite-support windowed-sinc (Kaiser window, support 8 taps each side),
differentiable with respect to the delay; zero-filled outside the support.
Also provides fine-grid kernel evaluation at arbitrary fractional tap
offsets (used by UWBRender and by the delay-recovery path).
"""

from __future__ import annotations

import torch
import torch.nn.functional as F

from nrecon.constants import OVERSAMPLE

_SINC_SUPPORT = 8  # taps each side
_KAISER_BETA = 12.0


def _kaiser(x: torch.Tensor, beta: float = _KAISER_BETA) -> torch.Tensor:
    """Kaiser window evaluated at arbitrary (possibly fractional) positions;
    zero outside |x| <= _SINC_SUPPORT."""
    x = x.double()
    b = torch.tensor(beta, dtype=torch.float64)
    arg = 1.0 - (x / _SINC_SUPPORT) ** 2
    w = torch.i0(beta * torch.sqrt(arg.clamp(min=0.0))) / torch.i0(b)
    return torch.where(arg > 0.0, w, torch.zeros_like(w))


def fractional_shift(x: torch.Tensor, delta_taps: torch.Tensor) -> torch.Tensor:
    """Delay `x` along its last axis by `delta_taps` taps (positive = later).

    `x` has shape [..., S]; `delta_taps` is a scalar or broadcastable to
    `x.shape[:-1]`. Output equals `x(t - delta_taps * ts)` evaluated on the
    tap grid with a windowed-sinc interpolator, zero-filled outside support.
    """
    x = torch.as_tensor(x)
    delta = torch.as_tensor(delta_taps, dtype=x.dtype, device=x.device)
    leading = x.shape[:-1]
    S = x.shape[-1]
    if delta.ndim == 0:
        delta_b = delta.reshape(1, 1, 1)
    else:
        delta_b = delta.broadcast_to(leading).reshape(-1, 1, 1)

    k = torch.arange(-_SINC_SUPPORT, _SINC_SUPPORT + 1, dtype=torch.float64)
    w = _kaiser(k.view(1, 1, -1) - delta_b.double()).to(x.dtype)  # follows delay
    kernel = w * torch.sinc(k.view(1, 1, -1).to(x.dtype) + delta_b)  # [B,1,2S+1]

    xr = x.reshape(-1, 1, S)
    xp = F.pad(xr, (_SINC_SUPPORT, _SINC_SUPPORT))
    y = F.conv1d(xp, kernel)  # correlation form; length S by construction
    return y.reshape(x.shape)


def sample_kernel(
    kernel_samples: torch.Tensor, offsets_taps: torch.Tensor
) -> torch.Tensor:
    """Evaluate a fine-grid kernel at arbitrary fractional tap offsets.

    `kernel_samples` has shape [K] (fine grid, step `TS_NS / OVERSAMPLE`);
    `offsets_taps` is a scalar or arbitrary-shape tensor. Linear
    interpolation on the fine grid (differentiable); offsets outside the
    stored support yield zero. Returns the broadcast shape of `offsets_taps`.
    """
    kernel = torch.as_tensor(kernel_samples)
    if kernel.ndim != 1:
        raise ValueError("kernel_samples must be 1-D")
    K = kernel.shape[0]
    offsets = torch.as_tensor(offsets_taps, dtype=kernel.dtype, device=kernel.device)

    x = offsets * OVERSAMPLE
    in_range = (x >= 0.0) & (x <= K - 1)
    xc = x.clamp(0.0, K - 1)
    lo = torch.floor(xc).long()
    hi = torch.clamp(lo + 1, max=K - 1)
    frac = xc - lo.to(x.dtype)
    vals = kernel[lo] * (1.0 - frac) + kernel[hi] * frac
    return torch.where(in_range, vals, torch.zeros_like(vals))
