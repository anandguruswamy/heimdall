"""Quantization path tests (plan Phase 2)."""

from __future__ import annotations

import numpy as np

from nrecon.seeding import seed_all
from nrecon.sim.quantize import from_i16, to_i16


def test_roundtrip_within_quantization_bound():
    seed_all(31)
    h = (np.random.randn(3, 64) + 1j * np.random.randn(3, 64)) * 0.05
    dgc = 3
    accum = 100
    back = from_i16(to_i16(h, dgc, accum), dgc, accum)
    gain = 10.0 ** ((dgc - 3.0) * 2.65 / 20.0)
    scale = gain / max(1, accum)
    # to_i16 maps float h (accumulator domain) to the i16 transport via
    # acc >> 2; from_i16 (Eq. 7) scales the transport without restoring the
    # low bits, so the roundtrip reconstructs h/4 within the quantization
    # step: rounding (<=0.5) plus 2 dropped bits (<=3) -> <= 0.875 acc units.
    bound = 0.875 * scale + 1e-12
    diff = back - h / 4.0
    assert np.all(np.abs(diff.real) <= bound)
    assert np.all(np.abs(diff.imag) <= bound)


def test_integer_path_bit_stable():
    seed_all(32)
    h = (np.random.randn(2, 64) + 1j * np.random.randn(2, 64)) * 0.1
    a = to_i16(h, 2, 50)
    b = to_i16(h, 2, 50)
    assert np.array_equal(a, b)
    assert a.dtype == np.int16
    assert a.shape == (2, 64, 2)


def test_i16_range_and_saturation():
    h = np.full((1, 64), 1e6 + 1e6j)
    out = to_i16(h, 3, 1)
    assert np.all(out[..., 0] == 32767)
    assert np.all(out[..., 1] == 32767)
    h = np.full((1, 64), -1e6 - 1e6j)
    out = to_i16(h, 3, 1)
    assert np.all(out[..., 0] == -32768)
    assert np.all(out[..., 1] == -32768)


def test_arithmetic_shift_negative():
    # signed 18-bit -5 shifted right by 2 is floor(-5/4) = -2
    h = np.array([[-5.0 + -5.0j]])
    out = to_i16(h, 3, 1)
    assert int(out[0, 0, 0]) == -2
    assert int(out[0, 0, 1]) == -2


def test_from_i16_matches_radar_map_scaling():
    i16 = np.array([[[100, -200]]], dtype=np.int16)
    out = from_i16(i16, 3, 40)
    assert np.isclose(out[0, 0], (100.0 - 200.0j) / 40.0)
    out2 = from_i16(i16, 7, 40)
    gain = 10.0 ** ((7.0 - 3.0) * 2.65 / 20.0)
    assert np.isclose(out2[0, 0], (100.0 - 200.0j) * gain / 40.0)
