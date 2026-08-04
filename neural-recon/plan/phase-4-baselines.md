# Phase 4 — Baselines and Per-Scene Optimization

Objective: implement the paper's non-neural baselines (Sec. IX) and use
per-scene gradient optimization to establish that UWBRender plus the chosen
primitives can recover controlled scenes at all. This is the observability
checkpoint the whole project depends on; a clear negative here stops Phase 5+
until the cause is understood.

Prerequisites: Gate N3.

## Steps

1. `nrecon/baselines/backprojection.py`: dense delay-and-sum on synthetic
   tensors — for each voxel and link, excess path -> tap (identical math to
   `host-tools/radar-map/radar_map/processing.py::backproject`, reimplemented
   here against the synthetic schema; do not import across the boundary).
   Inputs: envelope of `from_i16` CIRs aligned to the first-path marker.
   Output: `(z,y,x)` volume + argmax peaks list.
2. `nrecon/baselines/ellipsoid_voting.py`: per-link peak extraction
   (envelope local maxima above noise-scaled threshold) then sparse voting:
   sample each peak's ellipsoid shell, accumulate votes in a coarse voxel
   hash, return clustered candidate points. This is the sparse baseline and
   later an optimizer initializer.
3. `nrecon/baselines/fit_scene.py` — the per-scene optimizer:
   - Optimize `SceneTensors` leaves with Adam through `render_scene`.
   - Loss: phase-invariant complex Charbonnier (paper Eq. (25)) +
     log-envelope (Eq. (26)) with a curriculum flag `envelope_first`
     (envelope-only for the first fraction of iterations, then add complex;
     rationale: carrier-phase local minima, verified conclusion from the
     design review).
   - Regularizers: presence sparsity, scale bounds, plane-overlap penalty
     (paper Sec. VII-C subset; weights `PROVISIONAL` in config).
   - Init modes: `gt_perturbed` (ground truth + configurable noise),
     `random_multistart` (K restarts, keep best), `voting` (from step 2
     candidates as surfels; planes from room-boundary hypotheses).
   - Discrete handling v1: primitive types and cardinality are FIXED per
     run (from init); presence may decay below a pruning threshold. Type
     search/merging is explicitly out of scope for v1 (documented
     limitation; the paper's network provides types later).
   - Outputs: fitted scene, loss traces, per-link residuals, runtime — all
     written to `runs/fit/<name>/`.
4. Experiment V1 (`configs/exp-v1-surfel.yaml`): stage-1-style noiseless
   scene, 1 surfel, fixed live geometry, `random_multistart` K=8.
   Success (`PROVISIONAL`): best restart surfel center within 10 cm of
   truth; residual envelope loss below the noise floor; report failure rate
   across restarts.
5. Experiment V2 — synthetic decisive experiment (paper Sec. XI): scenes
   with 2-4 planes + 1 surfel at the fixed live geometry; init modes
   `gt_perturbed` (5 cm / 5 deg) and `voting`; 20 scenes.
   Report per scene: plane normal error, plane offset error, surfel center
   error, envelope + complex residuals, held-out-link residual (fit on 16
   links, evaluate on 4 withheld links), runtime, restarts needed.
   Success targets (`PROVISIONAL`): median normal error <5 deg, offset
   <5 cm, surfel center <10 cm from `gt_perturbed`; from `voting`, any
   success is informative — record basin-of-attraction findings.
6. Experiment V3 (stress, optional if V2 passes cleanly): repeat V2 with
   stage-3 nuisance enabled to observe degradation; note which nuisances
   break which parameter estimates.
7. Write `reports/N4-observability.md`: tables for V1/V2(/V3), failure-mode
   analysis (delay association, duplicated primitives, plane
   no-valid-reflection basins — paper-anticipated pathologies), and an
   explicit verdict: does direct optimization + UWBRender recover simple
   known scenes? If NO: stop, analyze whether the fault is renderer bugs,
   loss shaping, or true observability, record in `DECISIONS.md`, consult
   the user before proceeding to Phase 5.
8. Tests: backprojection localizes a single synthetic surfel to within one
   voxel (mirrors the existing radar-map synthetic test); voting returns a
   candidate within 30 cm of that surfel; `fit_scene` on a trivial 1-surfel
   `gt_perturbed` case converges in <200 iterations in CI time.

## Exit Gate N4

- [ ] All baseline tests pass.
- [ ] V1 and V2 executed; `reports/N4-observability.md` committed with
      quantitative tables and a written verdict.
- [ ] If the verdict is negative, user consulted before any Phase 5 work.
