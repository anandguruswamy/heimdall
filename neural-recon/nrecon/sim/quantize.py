"""Hardware quantization path (paper Sec. II-B, VI-E).

Synthetic export reproduces the DW3110 accumulator exactly: signed 18-bit
accumulator, arithmetic right shift by 2, `i16` I-then-Q. `from_i16` mirrors
the live pipeline's first-order scaling (paper Eq. (7);
`host-tools/radar-map/radar_map/processing.py::_scaled_cir`).
"""

from __future__ import annotations

import numpy as np

ACC_BITS = 18
ACC_MAX = 2 ** (ACC_BITS - 1) - 1  # 131071
ACC_MIN = -(2 ** (ACC_BITS - 1))  # -131072
SHIFT = 2


def _gain(dgc_decision: np.ndarray) -> np.ndarray:
    correction_db = (np.asarray(dgc_decision, dtype=np.float64) - 3.0) * 2.65
    return 10.0 ** (correction_db / 20.0)


def to_i16(h_float: np.ndarray, dgc_decision, accum_count) -> np.ndarray:
    """Float pipeline CIR -> hardware transport: int16 [..., 64, 2] I-then-Q.

    Inverse of Eq. (7): un-scale by `gain * max(1, accum)`, round to
    accumulator integers, saturate to the signed 18-bit range, arithmetic
    shift right by 2, cast to int16.
    """
    h = np.asarray(h_float)
    gain = _gain(dgc_decision)
    accum = np.maximum(1, np.asarray(accum_count, dtype=np.float64))
    acc = h * accum[..., None] / gain[..., None] if np.ndim(gain) else h * float(accum) / float(gain)
    acc_i = np.rint(acc.real).astype(np.int64)
    acc_q = np.rint(acc.imag).astype(np.int64)
    acc_i = np.clip(acc_i, ACC_MIN, ACC_MAX)
    acc_q = np.clip(acc_q, ACC_MIN, ACC_MAX)
    i16 = np.stack([acc_i >> SHIFT, acc_q >> SHIFT], axis=-1).astype(np.int16)
    return i16


def from_i16(i16: np.ndarray, dgc_decision, accum_count) -> np.ndarray:
    """Hardware transport -> scaled float pipeline CIR (paper Eq. (7))."""
    arr = np.asarray(i16)
    raw = arr[..., 0].astype(np.float64) + 1j * arr[..., 1].astype(np.float64)
    gain = _gain(dgc_decision)
    accum = np.maximum(1, np.asarray(accum_count, dtype=np.float64))
    scale = gain / accum
    if np.ndim(scale):
        return raw * scale[..., None]
    return raw * float(scale)


def to_i16_train(h: np.ndarray, dgc_decision, accum_count, ste: bool = False) -> np.ndarray:
    """Training path: straight-through estimator over the quantizer."""
    i16 = to_i16(h, dgc_decision, accum_count)
    quant = from_i16(i16, dgc_decision, accum_count)
    if ste:
        return h + (quant - h)
    return quant
