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

## 2026-08-04 Phase 2 UWBRender implementation notes

- `fractional_shift` splits the delay into an exact zero-filled integer roll
  plus the windowed-sinc applied to the fractional remainder (|f| <= 0.5).
  A pure windowed-sinc over a fixed +/-8 tap support cannot realize shifts
  beyond the support (e.g., the F0 = 16 marker alignment), and the window
  must be centered on the fractional delay itself; the roll+frac form is
  exact for integer shifts and keeps one small window everywhere.
- Plane image-source denominator guard: `torch.sign(0)` is 0, so an exact
  zero denominator (receiver on the mirror plane) produced 0/0 NaN; the
  guard now forces the sign to +/-1 for |den| < 1e-6.
- Image-source singularity: a specular point (or surfel center, or capsule
  quadrature point) within ~0.02 m of a node makes (d1*d2)^(-gamma/2)
  diverge (e.g., a receiver lying on the floor plane). A smooth near-node
  gate (0.02 -> 0.10 m, `PROVISIONAL`) suppresses these degenerate paths;
  without it the demo produced echoes of amplitude ~1e10.
- Quantization roundtrip: `to_i16` maps the float CIR to the i16 transport
  via acc18 >> 2, and `from_i16` (Eq. (7) mirror) scales the transport
  without restoring the dropped low bits. The roundtrip therefore
  reconstructs h/4 within +-0.875 accumulator units (rounding +-0.5 plus
  the two shifted-off bits), not h itself; the test asserts that property.
- Performance smoke (G_MAX=48 mixed slots, 20 links, CPU float64,
  forward+backward): measured 1.79 s vs the PROVISIONAL 30 s budget.
- Demo (`python -m nrecon.sim.demo`, live geometry JSON read-only): LOS
  peak at the F0=16 marker tap on every link; room-wall plane echoes at
  taps 19-26 (excess 1-3 m, plausible for the 2.5 m demo room); surfel
  and capsule echoes visible; envelopes sane, SVGs written to
  `artifacts/demo/`. Note: the live node layout places nodes 0-2 on the
  floor plane (z = 0), which is exactly the degenerate image-source case
  exercised above.

## 2026-08-04 Phase 3 dataset schema and semantics

- Shard schema extends the plan's array list with `fp_aligned [B,L] f32`,
  the total alignment shift (marker + hardware peak offset) actually
  applied to each CIR. Without it the bit-exact re-render consistency
  check cannot reproduce the stored CIRs (the stored `fp_q10_6` may be
  corrupted by false-first-path injection).
- False-first-path injection (the ~72-sample CIA anomaly) corrupts the
  recorded `fp_q10_6`/first-path metadata only; the CIR window remains
  aligned to the true first path. Modeling choice, v1.
- `layouts_per_scene > 1` (stage 4) renders the same sampled scene under
  several node layouts: objects are drawn from `PCG64(seed)` and the
  layout from a dedicated layout RNG, so objects are bit-identical across
  layouts and the room seed (and therefore the train/val/test split) is
  shared.
- Per-link complex AWGN is reproducible: `render_scene` now takes a
  `noise_seed` and an optional per-link `noise_std` tensor; the record
  pipeline uses the scene seed, so rebuild-by-seed and re-render-from-
  labels are both bit-exact on CPU.
- Split assignment is by hash of the room seed (80/10/10), so stage 4
  holds out entire room families from test. `cir_start = round(f) - 16`
  per Table I; `fp_q10_6 = round((f_recorded + cir_start) * 64)` per
  Eq. (3).
- Full stage 1 build (100 scenes): measured 29.5 scenes/s, built and
  validated (schema/manifest/determinism/consistency/splits all PASSED).
  Stages 2-4 (10k scenes) are deferred to Phase 6 per the plan.

## 2026-08-04 Live aligned/fitted CIR pipeline becomes the real-data input contract

- The UNO Q live pipeline (heimdall-service `pipeline.rs`, commit 09a6aea
  "Feed CIR products from selected fit") now computes one fitted "display"
  CIR per frame — `linear_ls` gain/phase/timing fit, `robust_grid`, or
  base-aligned fallback — and feeds it to the CIR, waterfall, slow-FFT, and
  fast-FFT products, gated by a `processing_epoch` so products are
  consistent within one processing configuration.
- Impact on the plan: Phases 0-4 are unaffected (the synthetic pipeline
  remains the training authority, and its marker ~16 alignment already
  matches the live `marker_aligned` convention). The real-data input
  contract changes: Phase 5's preprocessor must accept the live fitted
  CIR as-is and map the fit's per-link estimates onto the paper's
  nuisance metadata (log `a_ij`, phase, marker `f_ij`), and Phase 8 must
  (a) fit hardware statistics preferentially from the live fit's per-link
  gain/phase/timing estimates, and (b) record fit mode + fitted metadata
  in captures for reproducibility.
- Plan updated accordingly: `plan/phase-5-network.md` step 1, rewritten
  `plan/phase-8-real-transfer.md` steps, `README.md` scope note, `PLAN.md`
  Phase 8 row.
