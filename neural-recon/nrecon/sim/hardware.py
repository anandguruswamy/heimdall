"""Hardware nuisance model (paper Sec. VIII-B).

Per-link randomizations anchored to the published real statistics: median
first-path marker 16.109 taps, median peak offset 1.69 taps after the
marker, median accumulation count 108, dominant DGC states 3-6. Stage
configs may disable any subset; stage 1 is noiseless.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MEDIAN_FP = 16.109  # taps
MEDIAN_PEAK_OFFSET = 1.69  # taps
MEDIAN_ACCUM = 108
DGC_DOMINANT = (3, 6)

RESID_TAPS = 5  # residual FIR length K_b (PROVISIONAL)


@dataclass
class Nuisance:
    gain: np.ndarray  # [L] linear
    phase: np.ndarray  # [L] rad
    noise_std: np.ndarray  # [L]
    dgc: np.ndarray  # [L] int8-ish
    accum: np.ndarray  # [L]
    cfo: np.ndarray  # [L] ppm (metadata only in v1)
    fp_taps: np.ndarray  # [L] true first-path marker used for alignment
    fp_recorded: np.ndarray  # [L] marker stored in metadata (may be corrupted)
    peak_offset: np.ndarray  # [L] fractional taps after the marker
    resid_fir: np.ndarray  # [L, K_b] complex
    missing: np.ndarray  # [L] bool
    t_in_cycle: np.ndarray  # [L] seconds within the 35 ms cycle


def sample_nuisance(rng: np.random.Generator, cfg: dict, n_links: int) -> Nuisance:
    """Draw per-link nuisance for `cfg['hw']` (fields disabled when absent)."""
    hw = cfg.get("hw", {})
    n = n_links

    gain = np.ones(n)
    if "gain_db" in hw:
        db = rng.normal(hw["gain_db"][0], hw["gain_db"][1], size=n)
        gain = 10.0 ** (db / 20.0)

    phase = np.zeros(n)
    if "phase" in hw:
        phase = rng.uniform(-np.pi, np.pi, size=n)

    noise_std = np.zeros(n)
    if "noise_std" in hw:
        noise_std = np.exp(rng.normal(np.log(hw["noise_std"][0]), hw["noise_std"][1], size=n))

    dgc = np.full(n, 3, dtype=np.int8)
    if "dgc" in hw:
        lo, hi = hw["dgc"]
        dgc = rng.integers(lo, hi + 1, size=n).astype(np.int8)

    accum = np.full(n, MEDIAN_ACCUM, dtype=np.int16)
    if "accum" in hw:
        lo, hi = hw["accum"]
        accum = np.round(np.exp(rng.normal(np.log(lo), 0.3, size=n))).clip(
            lo, hi).astype(np.int16)

    cfo = np.zeros(n, dtype=np.float32)
    if "cfo_ppm" in hw:
        cfo = rng.normal(hw["cfo_ppm"][0], hw["cfo_ppm"][1], size=n).astype(np.float32)

    fp_taps = np.full(n, MEDIAN_FP)
    if "fp_jitter" in hw:
        std = hw["fp_jitter"]
        fp_taps = fp_taps + rng.normal(0.0, std, size=n)
    fp_recorded = fp_taps.copy()
    if "false_fp_p" in hw and hw["false_fp_p"] > 0:
        false = rng.random(n) < hw["false_fp_p"]
        jumps = rng.choice([-72.0, 72.0], size=int(false.sum()))
        fp_recorded[false] = fp_recorded[false] + jumps

    peak_offset = np.zeros(n)
    if "peak_offset" in hw:
        peak_offset = np.abs(rng.normal(hw["peak_offset"][0], hw["peak_offset"][1], size=n))

    resid_fir = np.zeros((n, RESID_TAPS), dtype=np.complex128)
    resid_fir[:, RESID_TAPS // 2] = 1.0  # identity default
    if "resid" in hw:
        strength = hw["resid"]
        center = RESID_TAPS // 2
        for l in range(n):
            taps = (rng.standard_normal(RESID_TAPS) + 1j * rng.standard_normal(RESID_TAPS))
            taps *= strength / RESID_TAPS
            taps[center] += 1.0 + 0.05 * rng.standard_normal()
            resid_fir[l] = taps

    missing = np.zeros(n, dtype=bool)
    if "missing_link_p" in hw:
        missing = rng.random(n) < hw["missing_link_p"]

    t_in_cycle = rng.uniform(0.0, 35e-3, size=n).astype(np.float32)

    return Nuisance(
        gain=gain, phase=phase, noise_std=noise_std, dgc=dgc, accum=accum, cfo=cfo,
        fp_taps=fp_taps, fp_recorded=fp_recorded, peak_offset=peak_offset,
        resid_fir=resid_fir, missing=missing, t_in_cycle=t_in_cycle,
    )


def apply_resid_fir(h: np.ndarray, resid_fir: np.ndarray) -> np.ndarray:
    """Convolve per-link CIRs with the residual FIR (center-windowed)."""
    out = np.empty_like(h)
    k_b = resid_fir.shape[-1]
    half = k_b // 2
    for l in range(h.shape[0]):
        c = np.convolve(h[l], resid_fir[l])
        start = half
        out[l] = c[start:start + h.shape[1]]
    return out
