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
    Complex inputs are shifted componentwise.
    """
    x = torch.as_tensor(x)
    if torch.is_complex(x):
        return _fractional_shift_real(x.real, delta_taps) + 1j * _fractional_shift_real(
            x.imag, delta_taps
        )
    return _fractional_shift_real(x, delta_taps)


def _fractional_shift_real(x: torch.Tensor, delta_taps: torch.Tensor) -> torch.Tensor:
    """Shift by an exact integer roll plus a windowed-sinc fractional part.

    Splitting `delta` into `round(delta) + frac` keeps the sinc support
    (`_SINC_SUPPORT` taps) valid for any shift magnitude: the integer part
    is an exact zero-filled roll, the fractional part |frac| <= 0.5 is
    applied with the windowed-sinc kernel centered on the delay.
    """
    x = torch.as_tensor(x)
    delta = torch.as_tensor(delta_taps, dtype=x.dtype, device=x.device)
    leading = x.shape[:-1]
    S = x.shape[-1]
    if delta.ndim == 0:
        d_int = torch.round(delta).long().reshape(1)
        f = (delta - d_int.to(delta.dtype)).reshape(1, 1, 1)
    else:
        db = delta.broadcast_to(leading).reshape(-1)
        d_int = torch.round(db).long()
        f = (db - d_int.to(db.dtype)).reshape(-1, 1, 1)

    xr = x.reshape(-1, S)
    cols = torch.arange(S, dtype=torch.long, device=x.device)
    idx = cols[None, :] - d_int[:, None]
    mask = (idx >= 0) & (idx < S)
    idx = idx.clamp(0, S - 1)
    rolled = torch.where(mask, xr.gather(1, idx), torch.zeros_like(xr))

    k = torch.arange(-_SINC_SUPPORT, _SINC_SUPPORT + 1, dtype=x.dtype, device=x.device)
    w = _kaiser(k.view(1, 1, -1) + f).to(x.dtype)  # window centered on the delay
    kernel = w * torch.sinc(k.view(1, 1, -1) + f)  # [B,1,2S+1]

    xp = F.pad(rolled[:, None, :], (_SINC_SUPPORT, _SINC_SUPPORT))
    out = F.conv1d(xp, kernel)  # [B, B, S]; row b convolved with every kernel
    idx_b = torch.arange(out.shape[0], device=out.device)
    y = out[idx_b, idx_b]  # each row with its own kernel
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
