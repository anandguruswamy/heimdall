"""Accuracy, boundaries, and gradients for cached surfel pulse backends."""

from __future__ import annotations

import pytest
import torch

from nrecon.constants import S_TAPS
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.sim.render import (
    _gauss_broadened_batch,
    _place,
    build_surfel_pulse_lookup,
    kernel_peak_taps,
    sample_surfel_pulse_lookup,
)


def _kernel():
    template = make_template_v1()
    return torch.as_tensor(correlation_kernel(template, template).samples)


def _exact(kernel, sigma, delta):
    taps = torch.arange(S_TAPS, dtype=kernel.dtype)
    broadened = _gauss_broadened_batch(kernel, sigma)
    return _place(broadened, taps, delta, kernel_peak_taps(kernel))


def test_lookup_shapes_and_storage():
    kernel = _kernel()
    fine = build_surfel_pulse_lookup(kernel, "bank-16x", sigma_bins=32)
    phase = build_surfel_pulse_lookup(
        kernel, "cache-1x-phase", sigma_bins=32, phase_bins=64)
    assert fine.table.shape == (32, kernel.numel())
    assert phase.table.shape == (32, 65, S_TAPS)
    assert not fine.table.requires_grad
    assert not phase.table.requires_grad


def test_bank_16x_matches_analytic_sweep():
    kernel = _kernel()
    lookup = build_surfel_pulse_lookup(kernel, "bank-16x")
    sigma = torch.linspace(0.01e-9, 15e-9, 97, dtype=kernel.dtype)
    delta = torch.linspace(-0.49, 8.49, 97, dtype=kernel.dtype)
    expected = _exact(kernel, sigma, delta)
    actual = sample_surfel_pulse_lookup(lookup, sigma, delta)
    nrmse = torch.linalg.vector_norm(actual - expected) / torch.linalg.vector_norm(expected)
    assert float(nrmse) < 1e-3


def test_phase_cache_accuracy_is_stratified_by_width():
    kernel = _kernel()
    lookup = build_surfel_pulse_lookup(kernel, "cache-1x-phase")
    delta = torch.linspace(-0.49, 8.49, 64, dtype=kernel.dtype)
    narrow_sigma = torch.linspace(0.01e-9, 2e-9, 64, dtype=kernel.dtype)
    broad_sigma = torch.linspace(5e-9, 15e-9, 64, dtype=kernel.dtype)
    for sigma, limit in ((narrow_sigma, 2e-3), (broad_sigma, 0.15)):
        expected = _exact(kernel, sigma, delta)
        actual = sample_surfel_pulse_lookup(lookup, sigma, delta)
        nrmse = torch.linalg.vector_norm(actual - expected) / torch.linalg.vector_norm(expected)
        assert float(nrmse) < limit


@pytest.mark.parametrize("backend", ["bank-16x", "cache-1x-phase"])
def test_lookup_is_continuous_across_integer_delays(backend):
    kernel = _kernel()
    lookup = build_surfel_pulse_lookup(kernel, backend)
    sigma = torch.tensor([1.3e-9], dtype=kernel.dtype)
    left = sample_surfel_pulse_lookup(
        lookup, sigma, torch.tensor([2.0 - 1e-7], dtype=kernel.dtype))
    right = sample_surfel_pulse_lookup(
        lookup, sigma, torch.tensor([2.0 + 1e-7], dtype=kernel.dtype))
    assert float((left - right).abs().max()) < 5e-5


@pytest.mark.parametrize("backend", ["bank-16x", "cache-1x-phase"])
def test_lookup_gradients_match_finite_differences(backend):
    kernel = _kernel()
    lookup = build_surfel_pulse_lookup(
        kernel, backend, sigma_bins=32, phase_bins=32)
    sigma_ns = torch.tensor([1.234, 6.789], dtype=torch.float64, requires_grad=True)
    delta = torch.tensor([0.237, 3.413], dtype=torch.float64, requires_grad=True)

    def evaluate(sigma_ns_arg, delta_arg):
        return sample_surfel_pulse_lookup(lookup, sigma_ns_arg * 1e-9, delta_arg)

    assert torch.autograd.gradcheck(
        evaluate, (sigma_ns, delta), eps=1e-5, atol=1e-5, rtol=1e-3)


def test_sigma_is_capped_at_fifteen_ns():
    kernel = _kernel()
    lookup = build_surfel_pulse_lookup(kernel, "bank-16x", sigma_max_ns=15.0)
    delta = torch.tensor([0.3], dtype=kernel.dtype)
    at_cap = sample_surfel_pulse_lookup(
        lookup, torch.tensor([15e-9], dtype=kernel.dtype), delta)
    above_cap = sample_surfel_pulse_lookup(
        lookup, torch.tensor([30e-9], dtype=kernel.dtype), delta)
    assert torch.allclose(at_cap, above_cap, atol=1e-15, rtol=1e-14)
