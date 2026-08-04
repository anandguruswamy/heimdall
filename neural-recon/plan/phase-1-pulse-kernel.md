# Phase 1 — Pulse Template, Correlation Kernel, Fractional Delay

Objective: freeze the assumed MP-SRRC template contract (paper Sec. VI-A),
build the accumulator correlation kernel r_p (paper Eq. (14)), and implement
the differentiable fractional-delay operator used everywhere downstream.

Prerequisites: Gate N0.

## Steps

1. `nrecon/sim/pulse.py`:
   - `srrc(t_ns: np.ndarray, beta: float, tp_ns: float) -> np.ndarray`
     implementing paper Eq. (13) with the removable singularities handled
     analytically (limits at t=0 and |t| = tp/(4*beta)).
   - `make_template_v1() -> Template` where `Template` is a frozen dataclass
     holding `samples: np.ndarray` (fine grid, step `TS_NS/OVERSAMPLE`),
     `t0_index`, `beta=0.5`, `tp_ns = 1e9/BW_HZ`, and provenance fields.
     Construction: SRRC(beta=0.5, Tp=1/499.2 MHz), truncate to +/-4 ns,
     apply a raised-cosine edge taper over the outer 0.5 ns, shift so the
     template is causal (precursor-free shaping stand-in), energy-normalize
     to unit L2. All shaping numbers are `PROVISIONAL` template-v1 contract
     values — record them in the manifest, not as hidden constants.
   - `correlation_kernel(tx: Template, rx: Template) -> Kernel` computing
     Eq. (14) by numerical correlation on the fine grid, energy-normalized,
     with `peak_index` metadata. For v1, tx == rx == template v1.
2. Artifact freeze: `python -m nrecon.sim.pulse export artifacts/pulse/`
   writes `template_v1.npy`, `kernel_v1.npy`, and `manifest.json` containing
   parameters, fine-grid step, SHA-256 of both arrays, generator git rev.
   Arrays are ignored by git; the manifest is committed. Any later change to
   pulse code must regenerate and re-hash, and the change must be logged in
   `DECISIONS.md` because it invalidates all rendered datasets.
3. `nrecon/sim/delay.py` (torch):
   - `fractional_shift(x: Tensor[..., S], delta_taps: Tensor[...]) -> Tensor`
     — finite-support windowed-sinc (Kaiser window, support 8 taps each
     side), differentiable w.r.t. `delta_taps`, zero-fill outside support.
     This is the operator D_delta of paper Eq. (8) and is also used by
     UWBRender to place kernels at fractional delays.
   - `sample_kernel(kernel, offsets_taps: Tensor) -> Tensor` — evaluate the
     fine-grid kernel at arbitrary fractional tap offsets by linear
     interpolation on the fine grid (differentiable).
4. Tests (`tests/test_pulse.py`, `tests/test_delay.py`):
   - Template: unit energy (1e-9), causal support, finite everywhere,
     -10 dB two-sided bandwidth of template v1 within [400, 620] MHz
     (sanity band, `PROVISIONAL`).
   - Kernel: peak at zero offset; symmetric within taper tolerance; energy
     normalized; `sample_kernel` at integer fine-grid points matches array.
   - Fractional delay: shift a synthetic pulse by known delta in
     [-3, 3] taps, recover the delta by correlation to <0.01 tap; shifting
     by 0 is identity to 1e-6; `torch.autograd.gradcheck` passes for
     `fractional_shift` and `sample_kernel` in float64.
5. Run full suite.

## Exit Gate N1

- [ ] `artifacts/pulse/manifest.json` committed with hashes; regeneration is
      bit-identical.
- [ ] All Phase 1 tests pass, including both gradchecks.
- [ ] Delay-recovery accuracy <0.01 tap demonstrated in test output.
