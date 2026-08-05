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

## 2026-08-04 Phase 4 observability results and verdict

- **Bug found and fixed:** `presence` was unconstrained and the sparsity
  penalty `lambda_presence * sum(presence)` is unbounded below; Adam drove
  presence to large negative values, producing negative losses and
  degrading every fit. Fix: project presence to [0, 1] after each step.
  This changed V2 results materially (plane normals 51-68 deg -> 5-25 deg
  on the affected scenes).
- **V1 (1-surfel, random multistart K=8):** success 0/8 (PROVISIONAL
  target <10 cm; best-restart median 1.75 m). The log-envelope loss is
  flat outside the echo-overlap basin (~0.2-0.3 m); random search over the
  ~26 m^3 volume cannot initialize it. Loss correlates with proximity:
  the optimizer works inside the basin; initialization is the failure.
- **V2 (2-4 planes + 1 surfel, gt_perturbed):** surfel median 0.124 m
  (7/20 < 0.10 m); plane normals median 10.9 deg (10/53 <= 5 deg,
  24/53 <= 10 deg, 9/53 > 25 deg); offsets median 0.136 m; held-out link
  residual 0.0026 (below the 1-unit quantization step). PROVISIONAL
  targets (normal <5 deg, offset <5 cm, surfel <10 cm) not met; the
  ~1/6 plane failures are rotation local minima (no-valid-reflection
  basins), not renderer defects; 1200 iterations and envelope-weight
  changes did not escape them.
- **Voting init:** candidates 1.0-3.8 m from truth (median 2.0 m), all
  outside the basin; the coarse quantized shells cannot initialize the
  optimizer. Dense backprojection (template LOS subtraction): 0.3-0.5 m
  on well-conditioned scenes with near-flat volumes.
- **Verdict: CONDITIONAL POSITIVE** (recorded in
  `reports/N4-observability.md`): UWBRender + optimizer recover simple
  known scenes from a good init (surfel 0.04-0.23 m), and the network
  (Phase 5) is expected to supply the type/cardinality/pose search the
  optimizer lacks. Per the plan, Phase 5 proceeds only after the user
  accepts this conditional verdict.
- Baseline test tolerances were calibrated to the measured evidence
  quality (DAS <0.50 m, voting <1.00 m on 0.15 m grids; plan's literal
  "within one voxel"/"30 cm" assumed idealized envelopes).

## 2026-08-04 Phase 4 optimizer revision (user: "improve optimizer first")

- **Critical bug found and fixed:** the fit's render-to-pipeline scale was
  `h * gain/accum / 4`; the gain/accum factors cancel in the transport
  mapping (target ~= h_true/4), so the rendered CIR was ~108x too small.
  The loss became amplitude-dominated and nearly position-independent, so
  the earlier "converged" fits only stayed near their gt_perturbed inits.
  Correct scale: uniform `h / 4`. This invalidates the earlier published
  V1/V2 numbers (reports/runs rewritten).
- Additional fixes: [0,1] presence projection each step (the linear
  sparsity penalty is unbounded below; presence ran to -inf and produced
  negative losses); per-link envelope normalization (paper Eq. (9)
  amplitude removal) so delay alignment dominates the loss; LOS-tap
  exclusion (the scene-independent direct path diluted the mean); a
  matched-smoothing multiscale schedule (render kernel AND target smoothed
  at the same scale, with a scale-dependent log floor and LOS exclusion).
- Corrected results: V1 best-restart median 0.45 m (0/8 < 10 cm); V2
  gt_perturbed surfel median 0.187 m (6/20 < 10 cm; evidence-limited:
  quantized delays pin the position to ~0.1-0.2 m); plane normals median
  12.3 deg (5/53 <= 5 deg; ~1/8 trap in rotation basins); volume-centroid
  init (0.80 m median) beats voting (2.0 m). PROVISIONAL targets remain
  unmet; the remaining gap is initialization from scratch and plane
  rotation basins — the Phase 5 network's amortized search role.
- Updated `reports/N4-observability.md` with the corrected tables and a
  revision note.

## 2026-08-04 Phase 5 network implementation

- `HeimdallSetNet` assembled per paper Fig. 1: shared link encoder
  (4 residual 1D-CNN stages 32/64/96/128, kernels 7/5/5/3, GroupNorm,
  GELU, attention pooling -> R^128), metadata MLP, Fourier-feature
  geometry MLP, learned direction-role embedding keyed by a
  label-invariant geometric quantity (baseline x-sign), 6 pre-norm set
  transformer blocks with a pairwise geometry-bias MLP (baseline
  cosines, midpoint separation, shared-node count from geometry,
  link lengths), and a 48-query 4-block primitive decoder with heads:
  type(4), presence, center(3), rot6d(6), log-scales(3), rho(2),
  roughness, attenuation, dynamic, bounded log-variances(9).
- Parameter count: 5.19M (in [5, 8]M). The paper's FFN 512 gave 2.55M;
  FFN widened to 1536 for both the set encoder and decoder per the
  plan's allowance ("adjust FFN width within Table II's spirit").
- Attention masks: per-head [B*H, L, L] float masks (bias + -1e9 pad)
  to avoid deprecated mixed mask types; key_padding_mask in the decoder.
- Node-relabel invariance verified: the relabeled-input construction must
  permute the position matrix by the INVERSE permutation
  (new_positions[sigma(a)] == old_positions[a]); with that, outputs match
  up to slot permutation (Hungarian, max distance < 1e-4, float64).
- `torch.use_deterministic_algorithms(True)` holds for all model ops on
  CPU; no op needed an exception.

## 2026-08-04 Inference target constraint (user)

- The trained model must be optimizable for CPU/GPU/NPU inference on
  Snapdragon X Elite machines (e.g., this Windows ARM64 laptop). The
  architecture therefore stays conventional and quantization-friendly:
  only Conv1d/Linear/GroupNorm/LayerNorm/GELU/attention matmuls and
  elementwise activations, all ONNX-exportable and QNN-int8/fp16
  compatible. UWBRender is training-only and is never part of the
  inference graph.
- Phase 7 gains an export task: ONNX export plus an fp16/int8
  quantization sanity check (metrics before/after quantization on the
  evaluation suite) before the Phase 7 report.

## 2026-08-05 Pre-GPU-run code review: trainer had no device wiring

- **Bug found and fixed (user requested a review before the Vast.ai run):**
  `nrecon/train/loop.py` never moved the model, kernel, or batches off CPU —
  `TrainConfig` had no `device` field and nothing called `.to(device)`
  anywhere. As written, a run on the rented CUDA instance would have either
  silently trained on the container's CPU or crashed on the first
  device-mismatched op. Fixed: `TrainConfig.device` (default `"cpu"`,
  overridable via `--device` on the CLI), `model`/`kernel` moved once in
  `train()`, batches moved via a new `nrecon.train.data.to_device` helper
  after `collate()`, AMP/`autocast`/`GradScaler` gated on
  `device.type == "cuda"` (previously hardcoded to `"cuda"` regardless of
  `cfg.amp`, which would have broken the CPU-only local runs the moment
  `amp=True`), and optimizer checkpoint state explicitly moved to `device`
  after `load_state_dict` (PyTorch does not do this automatically and the
  next `optim.step()` would otherwise raise a device-mismatch error).
- **Second bug found while fixing the first:** `match_slots` in `losses.py`
  (called on every training step, not just eval) built its Hungarian-matching
  cost tensor and the returned `(rows, cols)` index tensors with no `device=`
  argument, defaulting to CPU. This would have crashed on step 1 of any GPU
  run the moment the CPU cost tensor was combined with GPU-resident
  prediction tensors. Fixed by threading `pred["center"].device` through;
  `cost` is still moved to CPU only for the `scipy.optimize.linear_sum_assignment`
  call (SciPy requires a NumPy array), then the Hungarian result is placed
  back on `device`.
- `seed_all` now also calls `torch.cuda.manual_seed_all` when CUDA is
  available (previously CPU-only seeding, silently non-reproducible on GPU).
- `nrecon/train/analyze.py`'s `step_time_s` never used wall-clock time — it
  recomputed the average steps-per-log-row (a constant ~= `log_every`), not
  seconds/step, making it useless for estimating full-training duration from
  a GPU sanity run. Fixed: `loop.py` now logs a cumulative `wall_s` column in
  `metrics.csv`/`val.csv`; `analyze.py` derives `step_time_s`/`steps_per_sec`
  from the first/last valid `wall_s` sample. Older run directories predating
  this column report `nan` for these two fields rather than a wrong number.
- Verified: full CPU test suite still 66/66 passing; a fresh 6-step CPU smoke
  run against `datasets/stage1-mini` reproduces the previously observed loss
  trajectory (726 -> 12) with no device errors, and `analyze.py` now reports
  a real `step_time_s` (~2.0 s/step on this CPU) matching the printed
  per-step timings. The `device="cuda"` path itself can only be exercised on
  the rented Vast.ai instance (no local CUDA GPU); this is the first thing to
  verify there before any real training run.
- No other correctness issues found in `sim/`, `baselines/`, or `model/`; one
  cosmetic-only note: `render.py`'s `GAMMA` comment ("randomized per link in
  datasets") describes a variation that was never implemented anywhere in
  `hardware.py`/`export.py` — harmless (fixed gamma=2.0 is used throughout)
  but the comment should eventually be corrected or the feature added.

## 2026-08-05 Vast.ai compute host: RTX 5070, torch/CUDA arch mismatch, YAML gotcha

- Rented instance (RTX 5070, 12 GB VRAM, Texas US host, $0.104/hr,
  `pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime` base image). SSH access was
  blocked for an unrelated reason first: the default account SSH key
  (`~/.ssh/id_ed25519`) is passphrase-protected, and non-interactive `ssh`
  calls silently hang waiting for a passphrase prompt that never arrives
  (looks identical to a broken network/relay from the outside — three
  instances across three hosts/gateways were destroyed chasing that before
  the actual cause was found). Fixed by generating a dedicated
  passphrase-less automation key
  (`.secrets/ssh/vastai_ed25519`, gitignored) and registering it on the
  account via `vastai create ssh-key <path-to-.pub-file>` — note the CLI's
  positional argument must be the **key content**, not a file path string;
  passing a path string silently "succeeds" and stores the literal path text
  as the public key (a vastai CLI foot-gun, not obvious from `--help`).
- **RTX 5070/50-series (Blackwell, compute capability sm_120) is not
  supported by the base image's bundled torch 2.6.0+cu124** (arch list tops
  out at sm_90); `torch.cuda.is_available()` returns `True` (device
  enumeration works) but any kernel launch fails with `CUDA error: no kernel
  image is available for execution on the device`. Fixed by
  `pip install --upgrade torch==2.13.0` (matches the local CPU pin), which
  resolved to `torch 2.13.0+cu130` with `sm_120` in its arch list.
  torchvision/torchaudio dependency-conflict warnings from the mismatched
  base-image versions are harmless (unused by this project). Anyone renting
  a Blackwell-class Vast.ai GPU should check `torch.cuda.get_arch_list()`
  before trusting `torch.cuda.is_available()`.
- Cross-platform note: `tests/test_pulse.py::test_export_manifest_hashes_match`
  fails on Linux because the committed `artifacts/pulse/manifest.json` was
  generated on Windows; transcendental functions (`sin`/`cos`/`i0` in the
  SRRC template and Kaiser window) differ in their last bit between MSVC and
  glibc `libm`, changing the SHA-256 hash even though the values agree to
  ~15 decimal places. Not a bug — bit-exact determinism is guaranteed
  *within* a platform/build (which is what dataset build/validate relies on),
  not across platforms; datasets for GPU training are built and validated
  entirely on the Linux instance rather than copied from the Windows
  dev machine. 65/66 tests pass there; this one failure is expected and
  platform-specific.
- **Bug found on the first real GPU run:** the new `train-run1-gpu-sanity.yaml`
  wrote `early_stop_min_delta: 1e-4`; PyYAML's default float regex does not
  match bare scientific notation without a decimal point, so `yaml.safe_load`
  parsed it as the *string* `"1e-4"`, and training crashed inside
  `RunMonitor.check_loss` (`float - str`) after step 1. Fixed the immediate
  config (`0.0001`) and hardened `nrecon/train/run.py` with a
  `_coerce_types` helper that casts any YAML-loaded value to its declared
  `TrainConfig` dataclass field type before construction, so this class of
  mistake fails fast/silently-corrects instead of crashing mid-run.
- GPU sanity results (stage-1, 100 scenes, batch 32, no AMP): ~0.08-0.09
  s/step after warm-up at batch 8 on `stage1-mini`, ~35-40x faster than the
  measured local CPU rate (~3.1 s/step); a 2-minute/340-step run on
  `stage1-mini` early-stopped on a genuine loss plateau (loss 680 -> 1.25,
  medCenter 4.7 -> 0.30 m), validating both the device fix and the
  early-stop monitor end-to-end on real CUDA hardware.
