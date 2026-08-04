# Heimdall Neural Scene Reconstruction (neural-recon)

This subfolder implements the design in
`docs/papers/neural-uwb-scene-reconstruction.html`: a differentiable UWB
forward model (UWBRender), a procedural synthetic dataset, classical
baselines, per-scene optimization, and a geometry-conditioned set-prediction
network that maps 20 directed 64-tap CIRs to a compact typed primitive scene.

## Scope rules

- All work stays inside `neural-recon/` until Gate N7 (evaluation) produces a
  go decision for real-data transfer. The only exceptions are read-only use of
  `deployment/radar-geometry.live-20260728.json` (fixed live node layout) and,
  in Phase 8 only, read-only replay of protected `.husb` captures.
- Nothing here touches firmware, the UNO Q service, contracts, or the live
  system.
- Real-data input contract (Phase 8): the live UNO Q CIR tab fits per-link
  gain/phase/timing (`linear_ls` / `robust_grid`, fallback base-aligned)
  and publishes one aligned/fitted "display" CIR to the CIR, waterfall, and
  FFT products. Real data enters the network as that fitted CIR plus its
  fit metadata (marker `f_ij`, per-link gain/phase/timing estimates, DGC,
  accumulation count) via the Phase 5 preprocessor's real-data source —
  not a host-side re-fit of raw CIRs. Synthetic records remain the
  training authority and already mirror the marker ~16 alignment.
- Generated datasets, checkpoints, run logs, and rendered artifacts are never
  committed (see `.gitignore`). Small, deterministic, hashed artifacts (pulse
  template manifests, configs, metrics reports) are committed.
- The paper is the normative design reference. Where this plan fixes a value
  the paper leaves open, the choice is recorded in `DECISIONS.md` with
  rationale, marked `PROVISIONAL` when it is a tuning guess rather than a
  requirement.

## How an agent executes this plan

1. Read `PLAN.md`. Find the first phase whose exit gate is unchecked.
2. Open `plan/phase-<k>-*.md` and execute its numbered steps in order.
3. After every step that creates or modifies code, run the full test suite:
   `python -m pytest tests -q` from `neural-recon/`.
4. A gate is passed only when every item in its Exit Gate checklist is
   verified by a command whose output you actually observed. Then check the
   box in `PLAN.md` and append a dated entry to `DECISIONS.md` if any open
   choice was resolved.
5. Never skip a gate. Never start training runs longer than ~1 minute of
   wall time without telling the user the expected duration first (workspace
   rule). Full curriculum training requires a CUDA host and an explicit user
   decision (see Phase 6).
6. If a step conflicts with observed reality (API change, numerical
   instability, impossible target), stop, record the conflict in
   `DECISIONS.md`, and ask the user rather than silently deviating.

## Planned layout

```text
neural-recon/
  README.md            this file
  PLAN.md              master phase index and gate checklist
  DECISIONS.md         dated decision log
  plan/                phase instructions (the executable plan)
  nrecon/              Python package (created in Phase 0)
    constants.py       physical and system constants
    sim/               pulse, primitives, UWBRender, scenes, hardware, export
    baselines/         backprojection, ellipsoid voting, per-scene fitting
    model/             encoder, set transformer, primitive decoder
    train/             losses, loop, curriculum configs
    eval/              metrics, protocol, reports
  configs/             YAML configs for datasets, training, experiments
  tests/               pytest suite
  artifacts/           small generated artifacts (manifests tracked, bulk ignored)
  datasets/            generated shards (ignored)
  runs/                training runs, checkpoints, logs (ignored)
  reports/             evaluation reports (tracked, small)
```

## Fixed conventions (all code must follow these)

- Units: metres, nanoseconds, radians. Right-handed frame identical to
  `heimdall-geometry/1` (`host-tools/radar-map/radar_map/model.py`).
- Constants (single source: `nrecon/constants.py`):
  - `C_AIR = 299_702_547.0` m/s (matches the existing radar-map value)
  - `FS_HZ = 998.4e6`, `TS_NS = 1e9 / FS_HZ` (~1.0016 ns)
  - `METRES_PER_TAP = C_AIR / FS_HZ` (~0.3002 m)
  - `FC_HZ = 7.9872e9` (UWB channel 9 center, current Heimdall profile)
  - `BW_HZ = 499.2e6`, `S_TAPS = 64`, `F0_MARKER = 16.0`
  - `N_NODES = 5`, `L_LINKS = 20`, `G_MAX = 48`, `W_SUPPORT = 16`
  - `OVERSAMPLE = 16` (fine grid step `TS_NS / 16` for pulse kernels)
- Directed link ordering: all `(tx, rx)` pairs with `tx != rx`, sorted
  lexicographically. Missing links are represented by a boolean mask, never
  by reordering or compaction.
- CIR storage: raw synthetic exports reproduce the hardware path exactly —
  signed 18-bit accumulator, arithmetic right shift by 2, `i16` I-then-Q
  (paper Sec. II-B and VI-E). Float pipelines apply the same first-order
  scaling as the live pipeline (paper Eq. (7)).
- Rotations: networks predict the continuous 6D representation; storage uses
  full 3x3 matrices; tests compare with symmetry-aware distances
  (paper Sec. VII-A).
- Randomness: every dataset, experiment, and training run takes an explicit
  integer seed recorded in its manifest with the generator git revision and
  config hash. Rebuilding with the same seed must be bit-identical.
- Python: type-hinted, no network access at runtime, `numpy` + `scipy` +
  `torch` only for numerics (exact pins fixed in Phase 0).

## Test command

```powershell
python -m pytest tests -q
```

run from `neural-recon/` in the Phase 0 environment.
