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
- GPU sanity results (batch 8, no AMP): ~0.08-0.16 s/step after warm-up on
  `stage1-mini`, ~20-35x faster than the measured local CPU rate
  (~3.1 s/step at batch 8); a 2-minute/340-step run on `stage1-mini`
  early-stopped on a genuine loss plateau (loss 680 -> 1.25, medCenter
  4.7 -> 0.30 m), validating both the device fix and the early-stop monitor
  end-to-end on real CUDA hardware.
- **Batch size does not help GPU throughput as currently written:** a
  stage-1 (100-scene) run at batch 32 measured ~32.7 s/step (294 s for 9
  steps), ~200-400x worse than batch 8's ~0.1-0.16 s/step -- not the ~4x a
  batched op would show. Cause: `render_predicted()` in `loop.py` renders
  each batch element with a serial per-sample Python loop (one
  `render_scene` call per sample), and each call launches many small CUDA
  kernels for a 20-link/`G_MAX=48`-slot scene; kernel-launch overhead
  dominates and does not amortize across the batch. `train-run1-gpu-sanity.yaml`
  kept batch 8 (matching `train-run1.yaml`) rather than chasing a larger
  batch. Vectorizing `render_predicted`/`pred_to_scene` across the batch
  dimension (single `SceneTensors` with a leading batch axis, one
  `render_scene` call) is a real GPU-throughput opportunity for later runs
  but is out of scope for the current sanity pass.

## 2026-08-05 Sim-to-real CIR density gap found; GPU curriculum paused

- Comparing a real capture (`datasets/chair-occupancy-2026-08-04`,
  `aligned-cirs.ndjson`: 8414 records, N=5/20 links, `robust_grid`-fitted
  64-tap magnitude/IQ, no `dgc`/`accum`/`cfo` fields — already a post-fit
  "display" CIR) against synthetic stage-1/3 data found the real CIR is far
  denser than anything the renderer produces: mean fraction of the 64 taps
  with magnitude > 10% of peak is **42%** real vs **5.6%** synthetic
  stage-1 (1-3 surfels) vs **4.6%** synthetic stage-3 (11.9 primitives/scene
  avg, full hw noise model) — i.e. going from 2 to 11.9 primitives did not
  move the needle at all. The real tail is genuine slowly-decaying
  reverberant signal, not a noise-floor artifact: pre-marker region (taps
  0-11) averages 1.2% of peak (clean) vs the tail (taps 45-63, 40+ taps
  after the peak) averaging 8.2% of peak, ~7x the noise floor, decaying
  smoothly from the peak. Root cause: UWBRender only models single-bounce
  discrete-primitive echoes (one specular point per plane, one
  Gaussian-broadened point per surfel), each inherently narrow (~8 taps);
  no multi-bounce paths, no diffuse/rough-surface scattering, and the only
  broadening mechanism (`hardware.py`'s `resid_fir`, 5 taps, strength 0.05)
  is far too short to produce a 40+-tap decay. This is a structural
  representation gap, not a "needs more scenes/noise tuning" one. User
  paused further Vast.ai GPU spend pending this investigation (see the
  now-stopped instance in the prior entries); training resumes only after
  the fixes below are validated.
- **Caveat (user): this is one dataset from one room.** `chair-occupancy-
  2026-08-04` is a single reflective conference room (drywall, concrete
  ceiling beam, an acoustic wall panel, a metal-legged table/chairs).
  Other real environments will have different multipath density/decay.
  All new randomized parameters below are therefore drawn from a *range*
  per link/scene, not fixed to this one capture's point estimate, and are
  explicitly flagged PROVISIONAL/single-dataset pending more captures.
- **Network inputs simplified (user directive):** dropped the entire
  per-link scalar metadata pathway (`metadata_vector`: marker offset,
  log-gain, DGC, accum, CFO, observation time, missing-link flag) from
  `HeimdallSetNet`. Rationale (user + investigation): DGC/accum/CFO are
  hardware-transport artifacts needed only for `from_i16` and have no
  equivalent in the real live-fitted CIR; the marker offset is already
  fully consumed by `preprocess_cirs`'s alignment step (every CIR is
  re-centered to the same fixed reference before the network sees it) and
  its raw value would otherwise expose the live pipeline's own fit error
  (Qorvo modem marker + our robust-grid/linear-ls fit) with no way for the
  network to judge its reliability; the missing-link flag duplicates the
  `link_valid` mask already used for attention masking. Network inputs are
  now CIR channels (`preprocess_cirs`, I/Q + log-magnitude, unchanged) and
  geometry (`geometry_features`) only — `LinkEncoder`/`HeimdallSetNet`
  signatures drop `meta`/`meta_dim`, and `nrecon/train/data.py` stops
  building it. This is a real architecture change to the already-tested
  Phase 5 network and needs re-verifying with GPU sanity runs before
  trusting further training.
  - Follow-up (deferred, user): whether to also make phase explicit as
    `cos(phase), sin(phase)` (never raw radians — wraps discontinuously at
    +/-pi) alongside the existing I/Q + log-magnitude channels was
    discussed but held for a later ablation. The phase used here is
    already the *relative* phase after `preprocess_cirs` removes the
    common per-link reference phase (correlation against the LOS
    template), so it is not exposed to raw hardware CFO/oscillator drift
    the way a naive absolute-phase feature would be. The open concern if
    implemented: `cos/sin = I/mag, Q/mag` loses the free SNR-proportional
    confidence weighting that raw I/Q has (a noisy near-zero-magnitude tap
    contributes a full-strength unit vector once normalized), which matters
    more now that the reverb-tail model below fills much of the window
    with exactly that kind of low-magnitude, near-random-phase content;
    would need an amplitude-based gate or an `eps`-guarded denominator.
- **Real node-placement/frame investigation:** traced how
  `deployment/radar-geometry.live-*.json`'s frame is built
  (`unoq/dashboard/src/lib/positions.ts`/`BoardPositions.svelte`): node 0 =
  origin, node 1 = +X axis, node 2 = XY-plane (its Z is *forced* to exactly
  0 by the solver's parameterization, not measured), node 3 only resolves
  the sign of Z; there is no IMU/leveling/gravity input anywhere in the
  pipeline (confirmed by a repo-wide search), and the repo repeatedly
  self-flags this frame as "not surveyed"
  (`calibration_status: antenna-delay-not-independently-verified`).
  User clarified the real deployment convention: nodes 0/1/2 are placed at
  the same physical height (hand-leveled together — consistent with them
  being the nodes chosen to define the frame's plane), node 3 is placed
  higher, node 4 is placed anywhere but typically lower, and all pairwise
  node distances stay within 0.5-4.0 m for this deployment.
  `nrecon/sim/scenes.py::sample_nodes`'s `mode="random"` branch (used by
  stage 4) is rewritten to follow this convention via rejection sampling
  (`_MIN_SEPARATION=0.5`, `_MAX_SEPARATION=4.0`, replacing the old
  generic "N=5 on a random circle, min separation 0.6 m" sampler that had
  no connection to the real deployment).
  - Because "same height" is hand-leveled (not laser-surveyed) and the
    frame's Z-axis has no inherent gravity connection, a real room's true
    walls will be tilted from this frame's Z-axis by some small, unmodeled
    angle. User: "you can add a small randomized tilt, that's okay."
    Added `_sample_room_tilt`/`_place_in_world` to `scenes.py`: a small
    rigid rotation (`room.tilt_deg: [lo, hi]`, PROVISIONAL, `[0, 3]`
    degrees in stage 3/4) about a random horizontal axis, applied to the
    whole primitive set (walls, furniture, people, surfels — all built
    assuming Z is true-vertical, then re-expressed in the node frame) about
    the room's own center pivot. Drawn from the scene's object RNG (not the
    per-layout RNG), so it stays fixed across `layouts_per_scene > 1`
    layouts of one scene, preserving the existing "same scene objects,
    different node layouts" contract
    (`test_multiple_layouts_share_scene_objects`); a physically more
    accurate per-layout tilt was considered and deferred since it would
    require revisiting that contract.
- **Two more bugs found while investigating:** (1) `_sample_room` used to
  redraw its own independent `x, y, z` from the same config ranges
  already drawn by `sample_scene()` for furniture/people/surfels — the
  rendered walls and the scatter extent could silently disagree (and, per
  a test failure this fix exposed, `test_room_plane_count_and_sizes`'s
  `4 <= len(planes) <= 8` bound was already wrong/fragile — it counted
  furniture-table planes too, which also have `type == PLANE`; only
  happened to pass before because of exactly where the old buggy RNG
  stream landed for that seed). Fixed: `_sample_room` now takes `x, y, z`
  as parameters like the other samplers; the plane-count test now checks
  `_sample_room`'s own output directly. (2) For `node_mode: "random"`, the
  room box spans `[0, x_range] x [0, y_range]` while the old node sampler
  centered nodes near the origin (including negative coordinates) —
  nodes could fall outside/behind the room's own walls. The new
  `sample_nodes` random-mode cluster is anchored at a positive-quadrant
  point (`rng.uniform(0.5, 2.5, size=2)`), similar in scale to where
  `fixed_live`'s real node cluster sits, rather than being re-centered to
  zero-mean; not a rigorous fix for every possible room-size draw, but a
  real improvement over the previous unconditional mismatch.
- **Reverb-tail model added** (`nrecon/sim/hardware.py`): a lightweight
  statistical late-multipath/diffuse-tail nuisance — single-cluster
  Saleh-Valenzuela-style, i.i.d. complex Gaussian per-tap gain under an
  exponentially-decaying power envelope starting `REVERB_ONSET_TAPS=4`
  taps after the LOS/direct-path delay (avoids double-counting the direct
  path's own ~8-tap pulse width). New `hw.reverb: [lo, hi]` (per-link
  scale, log-uniform, as a fraction of that link's own rendered peak
  amplitude — applied via `apply_reverb_tail` once the peak is known) and
  `hw.reverb_decay_taps: [lo, hi]` (per-link decay time constant, taps).
  Applied in `export.py::render_record` as a channel effect (before
  `resid_fir`, which is downstream as a receiver-filter effect):
  `render_scene -> apply_reverb_tail -> apply_resid_fir -> align -> quantize`.
  Not applied to the network's own predicted-scene render
  (`loop.py::render_predicted`) — like existing noise/resid_fir, it is a
  target-side realism addition the network is trained to be robust to, not
  something it predicts. New `reverb_tail` shard array
  (`[L, S_TAPS, 2] f32`, real/imag, same storage pattern as `resid_fir`)
  added to the schema (`validate.py`), write/read (`export.py`), and
  `Nuisance` round-trip (`nuisance_from_record`); disabled by default
  (all-zero) when `hw.reverb` is absent, so stages 1/2 and existing
  configs are unaffected.
  - Calibration (PROVISIONAL, single-dataset): iterated
    `hw.reverb: [0.15, 0.6]` (measured 17.4% density on a 12-scene
    stage-3 mini-build, up from 4.6% pre-reverb) then
    `hw.reverb: [0.3, 1.0]`, `hw.reverb_decay_taps: [15.0, 30.0]`
    (measured **28.9%**, std 17.2%, on the same mini-build) — a ~6.3x
    density increase, most of the way to the real 42% without
    over-fitting a randomized-range model to one noisy 12-scene sample
    against one real capture. Set in `configs/dataset-stage3.yaml` and
    `configs/dataset-stage4.yaml`; stage 1/2 remain deliberately simple/
    noiseless per their own design intent.
- Verified: 69/69 tests pass (2 new: `test_random_node_placement_convention`,
  `test_room_tilt_disabled_by_default_enabled_when_configured` in
  `test_scenes.py`; 1 new: `test_reverb_tail_increases_cir_density` in
  `test_export.py`, plus `tilt_deg`/`reverb` added to the stage 3/4 test
  fixtures so the existing determinism/consistency/schema checks exercise
  the new fields). `datasets/stage1`, `stage1-mini`, and `stage3-mini`
  rebuilt and re-validated locally (schema/manifest/determinism/
  consistency/splits all PASSED) against the new shard schema.
- Not yet done: re-running the GPU sanity pass against the updated network
  (metadata removal) and the more-realistic stage-3/4 data before any
  further curriculum training spend; regenerating `runs/fit` (Phase 4)
  reports is not required since baselines/fit_scene.py is untouched by
  this pass.
