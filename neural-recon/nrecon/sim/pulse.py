"""Pulse template contract (paper Sec. VI-A) and correlation kernel (Eq. 14).

Template v1 is the assumed MP-SRRC stand-in: an energy-normalized beta=0.5
SRRC at 499.2 MHz bandwidth, truncated and edge-tapered, shifted causal.
The receiver accumulator is a correlation output, so paths are rendered with
the matched-filter kernel r_p (Eq. 14), not the template alone. All shaping
numbers are template-v1 contract values recorded in
`artifacts/pulse/manifest.json`, never hidden constants.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import numpy as np

from nrecon.constants import BW_HZ, OVERSAMPLE, TS_NS

TEMPLATE_VERSION = 1

# Template-v1 contract values (PROVISIONAL shaping numbers, recorded in the
# manifest; superseded by measured link kernels in Phase 8).
_BETA = 0.5
_TP_NS = 1e9 / BW_HZ  # 2.0032 ns
_TRUNC_HALF_NS = 4.0  # truncate SRRC to +/-4 ns
_TAPER_HALF_NS = 0.5  # raised-cosine edge taper over outer 0.5 ns


@dataclass(frozen=True)
class Template:
    samples: np.ndarray  # fine grid, step `step_ns`
    t0_index: int  # index of the pulse center (reference time)
    beta: float
    tp_ns: float
    step_ns: float
    trunc_half_ns: float
    taper_half_ns: float
    version: int = TEMPLATE_VERSION

    @property
    def t0_ns(self) -> float:
        return self.t0_index * self.step_ns


@dataclass(frozen=True)
class Kernel:
    samples: np.ndarray  # fine-grid correlation kernel r_p
    peak_index: int
    step_ns: float
    tx_version: int
    rx_version: int
    version: int = TEMPLATE_VERSION


def srrc(t_ns: np.ndarray, beta: float, tp_ns: float) -> np.ndarray:
    """Paper Eq. (13) with removable singularities handled analytically.

    Limits (via L'Hopital where both numerator and denominator vanish,
    which is the case at both singularities for the contract beta=0.5):

      t = 0:            (pi(1-beta) + 4 beta) / pi
      |t| = tp/(4 beta):  N'(x0)/D'(x0),  x0 = 1/(4 beta)
    """
    x = np.asarray(t_ns, dtype=np.float64) / tp_ns
    a = np.pi * (1.0 - beta)
    b = np.pi * (1.0 + beta)
    x0 = 1.0 / (4.0 * beta)
    with np.errstate(divide="ignore", invalid="ignore"):
        y = (np.sin(a * x) + 4.0 * beta * x * np.cos(b * x)) / (
            np.pi * x * (1.0 - (4.0 * beta * x) ** 2)
        )
    lim_zero = (np.pi * (1.0 - beta) + 4.0 * beta) / np.pi
    lim_pole = (
        a * np.cos(a * x0)
        + 4.0 * beta * np.cos(b * x0)
        - b * np.sin(b * x0)
    ) / (-2.0 * np.pi)
    y = np.where(x == 0.0, lim_zero, y)
    y = np.where(np.abs(x) == x0, lim_pole, y)
    return y


def make_template_v1() -> Template:
    """Build the frozen template-v1 MP-SRRC stand-in."""
    step_ns = TS_NS / OVERSAMPLE
    half = int(round(_TRUNC_HALF_NS / step_ns))
    t_ns = np.arange(-half, half + 1, dtype=np.float64) * step_ns
    samples = srrc(t_ns, _BETA, _TP_NS)

    taper_n = int(round(_TAPER_HALF_NS / step_ns))
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(taper_n) / (taper_n - 1))
    samples[:taper_n] *= ramp
    samples[-taper_n:] *= ramp[::-1]

    samples = samples / np.sqrt(np.sum(samples**2))
    return Template(
        samples=samples,
        t0_index=half,
        beta=_BETA,
        tp_ns=_TP_NS,
        step_ns=step_ns,
        trunc_half_ns=_TRUNC_HALF_NS,
        taper_half_ns=_TAPER_HALF_NS,
    )


def correlation_kernel(tx: Template, rx: Template) -> Kernel:
    """Numerical correlation of Eq. (14) on the fine grid, energy-normalized."""
    if tx.step_ns != rx.step_ns:
        raise ValueError("templates must share the same fine-grid step")
    corr = np.correlate(tx.samples, rx.samples, mode="full")
    corr = corr / np.sqrt(np.sum(corr**2))
    peak_index = int(np.argmax(np.abs(corr)))
    return Kernel(
        samples=corr,
        peak_index=peak_index,
        step_ns=tx.step_ns,
        tx_version=tx.version,
        rx_version=rx.version,
    )


def _sha256(arr: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()


def _git_rev() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def export(out_dir: Union[str, Path]) -> dict:
    """Write template_v1.npy, kernel_v1.npy, and manifest.json to `out_dir`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    template = make_template_v1()
    kernel = correlation_kernel(template, template)
    taper_n = int(round(template.taper_half_ns / template.step_ns))
    np.save(out / "template_v1.npy", template.samples)
    np.save(out / "kernel_v1.npy", kernel.samples)
    manifest = {
        "format": "neural-recon-pulse-manifest",
        "version": TEMPLATE_VERSION,
        "generator": "nrecon.sim.pulse",
        "generator_git_rev": _git_rev(),
        "fine_grid_step_ns": template.step_ns,
        "beta": template.beta,
        "tp_ns": template.tp_ns,
        "trunc_half_ns": template.trunc_half_ns,
        "taper_half_ns": template.taper_half_ns,
        "taper_samples": taper_n,
        "template_samples": int(template.samples.size),
        "template_t0_index": template.t0_index,
        "template_sha256": _sha256(template.samples),
        "kernel_samples": int(kernel.samples.size),
        "kernel_peak_index": kernel.peak_index,
        "kernel_tx_rx_version": [kernel.tx_version, kernel.rx_version],
        "kernel_sha256": _sha256(kernel.samples),
    }
    with open(out / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def main(argv: Optional[list] = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "export":
        sys.exit("usage: python -m nrecon.sim.pulse export <out_dir>")
    manifest = export(argv[1])
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
