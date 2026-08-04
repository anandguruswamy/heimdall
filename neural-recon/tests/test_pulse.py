"""Template and kernel contract tests (plan Phase 1)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from nrecon.sim.pulse import correlation_kernel, export, make_template_v1, srrc

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_template_unit_energy():
    t = make_template_v1()
    assert abs(float(np.sqrt(np.sum(t.samples**2))) - 1.0) < 1e-9


def test_template_finite_and_causal_support():
    t = make_template_v1()
    assert np.all(np.isfinite(t.samples))
    assert int(np.argmax(np.abs(t.samples))) == t.t0_index
    assert t.t0_ns > 0.0
    assert t.samples[0] == 0.0 and t.samples[-1] == 0.0


def test_template_negative_ten_db_bandwidth():
    t = make_template_v1()
    fs = 1e9 / t.step_ns
    n = 65536
    spec = np.abs(np.fft.rfft(t.samples, n))
    spec = spec / spec.max()
    db = 20.0 * np.log10(spec)
    f = np.fft.rfftfreq(n, d=t.step_ns * 1e-9)
    pk = int(np.argmax(db))
    hi = np.where(db[pk:] < -10.0)[0]
    assert hi.size > 0
    f_hi = float(f[pk + hi[0]])
    full_width = 2.0 * f_hi
    assert 500e6 <= full_width <= 700e6


def test_srrc_finite_everywhere_around_singularities():
    t = np.array([-4.0, -2.0, -0.5, -1e-15, 0.0, 1e-15, 0.5, 2.0, 4.0])
    y = srrc(t, 0.5, 2.003205128205128)
    assert np.all(np.isfinite(y))
    assert np.isclose(srrc(np.array([0.0]), 0.5, 2.003205128205128)[0],
                      srrc(np.array([1e-15]), 0.5, 2.003205128205128)[0])
    pole = 0.5 * 2.003205128205128
    assert np.isclose(srrc(np.array([pole]), 0.5, 2.003205128205128)[0],
                      srrc(np.array([-pole]), 0.5, 2.003205128205128)[0])


def test_kernel_peak_at_zero_offset():
    t = make_template_v1()
    k = correlation_kernel(t, t)
    assert k.peak_index == int(np.argmax(np.abs(k.samples)))
    assert k.samples[k.peak_index] > 0.0


def test_kernel_symmetric():
    t = make_template_v1()
    k = correlation_kernel(t, t)
    assert np.allclose(k.samples, k.samples[::-1], atol=1e-12)


def test_kernel_energy_normalized():
    t = make_template_v1()
    k = correlation_kernel(t, t)
    assert abs(float(np.sqrt(np.sum(k.samples**2))) - 1.0) < 1e-9


def test_export_manifest_hashes_match():
    import hashlib

    t = make_template_v1()
    k = correlation_kernel(t, t)
    sha = lambda arr: hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()
    manifest_path = REPO_ROOT / "artifacts" / "pulse" / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        assert manifest["template_sha256"] == sha(t.samples)
        assert manifest["kernel_sha256"] == sha(k.samples)
        assert manifest["fine_grid_step_ns"] == t.step_ns
