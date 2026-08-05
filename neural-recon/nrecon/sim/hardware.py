"""Hardware nuisance model (paper Sec. VIII-B).

Per-link randomizations anchored to the published real statistics: median
first-path marker 16.109 taps, median peak offset 1.69 taps after the
marker, median accumulation count 108, dominant DGC states 3-6. Stage
configs may disable any subset; stage 1 is noiseless.

DEVIATION (2026-08-05, sim-to-real gap): a real capture
(datasets/chair-occupancy-2026-08-04) showed CIR energy spread across
~42% of the 64 taps (mean magnitude 7x the pre-marker noise floor even
40+ taps after the peak, i.e. a genuine slowly-decaying reverberant
tail), while even the richest configured synthetic stage (3/4, ~12
primitives/scene avg) only reaches ~4.6% -- adding scene primitives alone
does not close this gap; the renderer's single-bounce discrete-primitive
model has no mechanism to produce a broadband decaying tail. `reverb_tail`
below adds a lightweight statistical late-multipath/diffuse-tail model
(Saleh-Valenzuela-style: single cluster, i.i.d. complex Gaussian per-tap
gain under an exponentially-decaying envelope) as a per-link nuisance,
calibrated against that one capture. PROVISIONAL and single-dataset: scale
and decay are drawn from a *range* per link/scene (not fixed constants),
since other real environments will plausibly have different multipath
density -- see DECISIONS.md.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from nrecon.constants import S_TAPS

MEDIAN_FP = 16.109  # taps
MEDIAN_PEAK_OFFSET = 1.69  # taps
MEDIAN_ACCUM = 108
DGC_DOMINANT = (3, 6)

RESID_TAPS = 5  # residual FIR length K_b (PROVISIONAL)
REVERB_ONSET_TAPS = 4  # taps after LOS before the reverb tail begins (PROVISIONAL;
                       # avoids double-counting the direct-path pulse's own ~8-tap width)


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
    reverb_tail: np.ndarray  # [L, S_TAPS] complex, late-multipath/diffuse tail


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

    reverb_tail = np.zeros((n, S_TAPS), dtype=np.complex128)
    if "reverb" in hw and hw["reverb"][1] > 0:
        reverb_tail = sample_reverb_tail(rng, n, hw)

    return Nuisance(
        gain=gain, phase=phase, noise_std=noise_std, dgc=dgc, accum=accum, cfo=cfo,
        fp_taps=fp_taps, fp_recorded=fp_recorded, peak_offset=peak_offset,
        resid_fir=resid_fir, missing=missing, t_in_cycle=t_in_cycle,
        reverb_tail=reverb_tail,
    )


def sample_reverb_tail(rng: np.random.Generator, n_links: int, hw: dict,
                       s_taps: int = S_TAPS) -> np.ndarray:
    """Draw per-link stochastic late-multipath/diffuse-tail taps.

    PROVISIONAL statistical model (single-cluster Saleh-Valenzuela-style:
    i.i.d. complex Gaussian per-tap gain under an exponentially-decaying
    power envelope starting `REVERB_ONSET_TAPS` after the LOS/direct-path
    delay), calibrated against one real capture
    (datasets/chair-occupancy-2026-08-04, see DECISIONS.md 2026-08-05).

    `hw["reverb"] = [lo, hi]`: per-link scale, drawn log-uniformly, as a
    fraction of that link's own rendered peak amplitude (applied later in
    `apply_reverb_tail`, once the peak is known).
    `hw.get("reverb_decay_taps", [12.0, 24.0])`: per-link decay time
    constant range, in taps. Both are ranges rather than fixed constants
    because only one real environment has been measured so far -- other
    rooms/materials plausibly have different multipath density (see
    DECISIONS.md).
    """
    lo, hi = hw["reverb"]
    scale = np.exp(rng.uniform(np.log(max(lo, 1e-6)), np.log(max(hi, lo + 1e-6)),
                               size=n_links))
    decay_lo, decay_hi = hw.get("reverb_decay_taps", [12.0, 24.0])
    decay_taps = rng.uniform(decay_lo, decay_hi, size=n_links)

    taps = np.arange(s_taps)
    active = taps >= REVERB_ONSET_TAPS
    env = np.zeros((n_links, s_taps))
    env[:, active] = np.exp(
        -(taps[None, active] - REVERB_ONSET_TAPS) / decay_taps[:, None])

    real = rng.standard_normal((n_links, s_taps))
    imag = rng.standard_normal((n_links, s_taps))
    tail = (real + 1j * imag) / np.sqrt(2.0) * env * scale[:, None]
    return tail.astype(np.complex128)


def apply_reverb_tail(h: np.ndarray, reverb_tail: np.ndarray) -> np.ndarray:
    """Add the stochastic late-multipath tail, scaled by each link's own
    rendered peak amplitude (see `sample_reverb_tail`)."""
    peak = np.abs(h).max(axis=-1, keepdims=True)
    return h + reverb_tail * peak


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
