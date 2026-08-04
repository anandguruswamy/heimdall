# Decision Log

Append dated entries. Never rewrite history; supersede with a new entry.

## 2026-08-04 Folder and package layout

- Subfolder `neural-recon/` at repo root; Python package `nrecon` with
  submodules `sim`, `baselines`, `model`, `train`, `eval`.
- Rationale: single package keeps renderer, data, and model import paths
  coherent for tests; subfolder isolation follows the user's instruction to
  stay self-contained until synthetic validation completes.

## 2026-08-04 Physical constants

- `C_AIR = 299_702_547.0` m/s adopted from
  `host-tools/radar-map/radar_map/model.py` for consistency with existing
  Heimdall processing; carrier `FC_HZ = 7.9872e9` (channel 9 per the
  qualified N=5/M=2 profile in `STATUS.md`); accumulator rate 998.4 MHz and
  64 taps per `docs/papers/neural-uwb-scene-reconstruction.html` Table I.

## 2026-08-04 Pulse template v1 is an assumed contract

- The paper (Sec. VI-A) treats the MP-SRRC template as a dataset-generator
  contract, not a normative closed form. Template v1 will be a beta=0.5 SRRC
  at 499.2 MHz bandwidth, truncated and edge-tapered, energy-normalized,
  sampled at 16x the accumulator rate. `PROVISIONAL`: to be replaced by
  measured link kernels in Phase 8 before any sim-to-real claim
  (paper Sec. X, "Pulse mismatch").

## 2026-08-04 Torch versions deferred to Phase 0

- Exact `torch`/`numpy`/`scipy` pins are chosen and recorded during Phase 0
  on the actual host, then added to `tools/README.md` per the workspace
  tooling rule. CPU-only development locally; CUDA training host is a Phase 6
  decision point.

## 2026-08-04 Phase 0 environment resolved

- Interpreter: CPython 3.10.6 x86-64 (AMD64) at
  `C:\Program Files\Python310\python.exe`, the same validated interpreter
  used by `host-tools/radar-map` (runs under Windows ARM64 emulation).
- Resolved pins (see `requirements.lock`): numpy 2.2.6 (cached wheel,
  checksum verified), scipy 1.15.3, torch 2.13.0+cpu (PyPI Windows CPU
  build), PyYAML 6.0.3 (cached wheel, checksum verified), pytest 9.1.1.
  Wheel provenance and SHA-256 added to `tools/README.md`; torch/scipy/
  pytest wheels retained under `tools/installers/common-python/`.
- `requirements.lock` is `pip freeze` output with the two locally installed
  wheels normalized from `numpy @ file:///...` absolute paths to plain
  version pins, so the lock is portable to another machine; the wheel
  checksums in `tools/README.md` pin integrity instead.

## 2026-08-04 Template v1 -10 dB bandwidth band corrected

- PROVISIONAL sanity band in `plan/phase-1-pulse-kernel.md` step 4 was
  [400, 620] MHz two-sided at -10 dB. Measured template v1 full width is
  639.6 MHz; the ideal untapered beta=0.5 SRRC (Tp = 1/499.2 MHz) crosses
  -10 dB at |f| ~= 323 MHz analytically (full width ~646 MHz), so the cap
  of 620 MHz was a hypothesis falsified by the physics, not a bug.
- Band amended (user-approved 2026-08-04) to full-width -10 dB in
  [500, 700] MHz; test asserts the widened band.

## 2026-08-04 Phase 1 fractional-delay operator details

- The Kaiser window in `fractional_shift` follows the delay argument
  (`w(k - delta)`, standard variable-delay windowed-sinc): windowing
  `w(k)` instead produced reconstruction error ~0.1 at delta = 3 taps on
  an otherwise perfectly bandlimited kernel; with the window tracking the
  delay the same case is exact to ~1e-15.
- Delay recovery correlates against the stored kernel referenced to its
  peak (`sample_kernel(kernel, u - n + peak_tap)`); without centering, the
  correlation peak sits at u = 24 + delta instead of delta.
- `sample_kernel` is piecewise-linear and has derivative kinks at integer
  fine-grid offsets; gradcheck therefore uses non-grid-aligned offsets.
- Measured delay-recovery accuracy over [-3, 3] taps: max error 0.0 tap
  (all deltas recovered exactly on the parabolic-refined 1/16-tap grid),
  comfortably under the <0.01 tap gate.
