"""Fractional-delay and kernel-sampling tests (plan Phase 1)."""

from __future__ import annotations

import numpy as np
import torch

from nrecon.constants import S_TAPS
from nrecon.seeding import seed_all
from nrecon.sim.delay import fractional_shift, sample_kernel
from nrecon.sim.pulse import make_template_v1


def _tap_grid_reference(kernel_samples: torch.Tensor) -> torch.Tensor:
    return sample_kernel(kernel_samples, torch.arange(S_TAPS, dtype=torch.float64))


def _recover_delay(kernel_samples: torch.Tensor, y: torch.Tensor, guess: float) -> float:
    """Recover the fractional delay of `y` (tap-grid signal) by fine-grid
    correlation with the kernel reference centered at zero offset,
    refined by parabolic interpolation."""
    y64 = y.double()
    k = kernel_samples.double()
    peak_tap = float(torch.argmax(k)) / 16.0  # kernel peak position in taps
    width = 8  # taps
    lo_m = int((guess - width) * 16)
    hi_m = int((guess + width) * 16) + 1
    g = torch.empty(hi_m - lo_m)
    n = torch.arange(S_TAPS, dtype=torch.float64)
    for j, m in enumerate(range(lo_m, hi_m)):
        offs = m / 16.0 - n + peak_tap
        g[j] = torch.sum(y64 * sample_kernel(k, offs))
    m_star = int(torch.argmax(g))
    if 0 < m_star < g.numel() - 1:
        denom = g[m_star - 1] - 2 * g[m_star] + g[m_star + 1]
        corr = 0.5 * (g[m_star - 1] - g[m_star + 1]) / denom if denom != 0 else 0.0
    else:
        corr = 0.0
    return (lo_m + m_star + float(corr)) / 16.0 - peak_tap


def test_fractional_shift_zero_is_identity():
    seed_all(7)
    x = torch.randn(12, dtype=torch.float64)
    y = fractional_shift(x, torch.tensor(0.0, dtype=torch.float64))
    assert torch.allclose(y, x, atol=1e-6)


def test_delay_recovery_accuracy():
    from nrecon.sim.pulse import correlation_kernel, make_template_v1

    t = make_template_v1()
    kernel = torch.as_tensor(correlation_kernel(t, t).samples, dtype=torch.float64)
    x = _tap_grid_reference(kernel)
    for delta in (-2.5, -1.0, -0.25, 0.0, 0.25, 1.0, 2.5, 3.0):
        y = fractional_shift(x, torch.tensor(delta, dtype=torch.float64))
        est = _recover_delay(kernel, y, delta)
        assert abs(est - delta) < 0.01, f"delta={delta} recovered {est}"


def test_sample_kernel_integer_points_match():
    seed_all(3)
    kernel = torch.randn(257, dtype=torch.float64)
    idx = torch.tensor([0, 5, 128, 256], dtype=torch.float64)
    offs = idx / 16.0
    vals = sample_kernel(kernel, offs)
    assert torch.allclose(vals, kernel[idx.long()], atol=1e-12)


def test_sample_kernel_out_of_range_zero():
    kernel = torch.ones(257, dtype=torch.float64)
    offs = torch.tensor([-3.0, 20.0], dtype=torch.float64)
    assert torch.all(sample_kernel(kernel, offs) == 0.0)


def test_fractional_shift_gradcheck():
    seed_all(11)
    x = torch.randn(8, dtype=torch.float64, requires_grad=True)
    delta = torch.tensor(1.3, dtype=torch.float64, requires_grad=True)
    assert torch.autograd.gradcheck(
        fractional_shift, (x, delta), eps=1e-6, atol=1e-5, rtol=1e-3
    )


def test_sample_kernel_gradcheck():
    seed_all(13)
    kernel = torch.randn(33, dtype=torch.float64, requires_grad=True)
    # offsets must avoid exact integer fine-grid positions: linear
    # interpolation is C1 there and finite differences straddle the kink
    offs = torch.tensor([1.1, 1.53125, 2.125], dtype=torch.float64, requires_grad=True)
    assert torch.autograd.gradcheck(
        sample_kernel, (kernel, offs), eps=1e-6, atol=1e-5, rtol=1e-3
    )
