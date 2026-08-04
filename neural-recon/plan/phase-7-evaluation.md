# Phase 7 — Evaluation Protocol and Decisive Experiment

Objective: measure the trained network against all baselines under the
paper's three-level protocol (Sec. IX), run the decisive experiment
(Sec. XI), and produce an explicit go/no-go for real-data transfer.

Prerequisites: Gate N6.

## Steps

1. `nrecon/eval/metrics.py`:
   - Primitive recovery: type average precision; plane normal (deg) and
     offset (m) errors; extent IoU (rectangle overlap in-plane); surfel
     center error and covariance Frobenius error; capsule center/size
     errors. Matching via the Phase 6 Hungarian cost.
   - Physical consistency: held-out-link protocol — render the predicted
     scene, compare on links withheld from the network input mask; complex
     and envelope errors; path-delay error against privileged path tables;
     residual energy by delay bin; cross-link consistency.
   - Scene utility: surface Chamfer distance (sampled from primitives vs
     ground-truth surfaces); voxel occupancy IoU at 10 cm; person (capsule)
     localization error; uncertainty calibration — reliability diagrams and
     empirical-vs-predicted sigma ratios per parameter group.
2. `nrecon/eval/protocol.py`: one entry point that evaluates a system
   ("network", "backprojection", "voting", "optimizer-random",
   "optimizer-voting", "hybrid" = network init -> `fit_scene` refinement
   with type freeze + presence pruning) over a named test set and emits a
   stratified table: node-geometry condition number, SNR band, missing-link
   count, path order, seen/unseen room topology (paper Sec. IX).
   Also record runtime per snapshot and, for optimizer modes, failure rate
   across restarts and primitive-count stability.
3. Test sets (built with Phase 3 tooling, seeds disjoint from training):
   - `test-fixed`: fixed live geometry, in-distribution scenes.
   - `test-rooms`: held-out room-topology families.
   - `test-geom`: held-out node layouts (stage-4 style, unseen).
   - `test-hard`: high nuisance, missing links, low SNR.
4. Decisive experiment D1 (paper Sec. XI): 2-4 planes + 1 surfel at the
   exact fixed live geometry, then with small geometry perturbations.
   Compare network, dense backprojection, per-scene optimization, hybrid.
   The paper's feasibility table (Table III) is the prior: dominant
   reflectors should be good, full wall recovery moderate. Report per-system
   tables and per-scene visual overlays.
5. Ablations (paper Sec. IX): no geometry conditioning, no metadata, no
   uncertainty heads, no CIR reconstruction loss — run 4 config minus one
   component each, on a reduced budget; report deltas.
6. Write `reports/N7-evaluation.md`:
   - All tables, stratified; hybrid-vs-parts analysis; calibration verdict
     ("probabilities are conditional predictions, calibration must be
     checked on held-out scenes" — verified conclusion).
   - Explicit go/no-go recommendation for Phase 8 with reasons, and the
     list of model deficiencies that must be fixed first if no-go.
7. Do not proceed to Phase 8 without user approval of the report.

## Exit Gate N7

- [ ] Metrics module unit-tested (hand-built scenes with known errors).
- [ ] All systems evaluated on all four test sets; D1 executed.
- [ ] `reports/N7-evaluation.md` committed; user decision on Phase 8
      obtained and recorded.
