# Master Plan and Gate Checklist

Execute phases strictly in order. Each phase document in `plan/` contains the
numbered steps; this file tracks completion. A gate box may be checked only
after every item in that phase's Exit Gate checklist was verified by an
observed command result.

| Phase | Document | Purpose | Gate |
|---|---|---|---|
| 0 | `plan/phase-0-environment.md` | Pinned Python environment, package skeleton, CI-style test entry | N0 |
| 1 | `plan/phase-1-pulse-kernel.md` | MP-SRRC template contract, correlation kernel, fractional delay | N1 |
| 2 | `plan/phase-2-uwbrender.md` | Differentiable UWBRender with verified gradients | N2 |
| 3 | `plan/phase-3-dataset.md` | Procedural scenes, hardware randomization, curriculum datasets | N3 |
| 4 | `plan/phase-4-baselines.md` | Backprojection, ellipsoid voting, per-scene optimization; renderer observability verdict | N4 |
| 5 | `plan/phase-5-network.md` | Geometry-conditioned set network implementation | N5 |
| 6 | `plan/phase-6-training.md` | Losses and curriculum training runs 1-4 | N6 |
| 7 | `plan/phase-7-evaluation.md` | Metrics, stratified protocol, decisive experiment, go/no-go | N7 |
| 8 | `plan/phase-8-real-transfer.md` | Calibration campaign, real-data fine-tuning (leaves subfolder-only scope; user approval required) | N8 |

## Gate checklist

- [x] **N0** Environment pinned, `nrecon` package imports, empty test suite passes.
- [ ] **N1** Pulse template and correlation kernel frozen with hashed manifest; fractional delay verified to <0.01 tap; differentiable.
- [ ] **N2** UWBRender renders LOS + surfels + planes + capsules; analytic geometry tests pass; autograd matches finite differences (<1e-3 rel, float64); full 48-slot/20-link render completes on CPU.
- [ ] **N3** Stage 1-4 dataset builders deterministic and schema-validated; re-render-from-labels reproduces stored CIRs bit-exactly; splits are disjoint by scene seed.
- [ ] **N4** Per-scene optimizer recovers a noiseless surfel from random init; the 2-4 plane + 1 surfel fixed-geometry experiment is run and its verdict (including failure modes) is written to `reports/N4-observability.md`.
- [ ] **N5** Network builds at 5-8M params; node-relabel/link-permutation invariance verified; missing-link masking verified.
- [ ] **N6** Curriculum runs 1-4 complete with logged metrics; run 1 overfits to near-zero loss proving end-to-end gradient flow; compute-host decision recorded.
- [ ] **N7** Full evaluation report in `reports/N7-evaluation.md` comparing network, backprojection, ellipsoid voting, optimizer, and hybrid on held-out rooms and held-out links; explicit go/no-go for real transfer.
- [ ] **N8** (post-approval) Hardware statistics fitted from real captures, measured link kernels installed, controlled real fine-tune evaluated.

## Standing constraints

- Long operations (>30 s) require telling the user the operation and expected
  duration first; full training runs additionally require the Phase 6
  compute-host decision.
- All numeric targets marked `PROVISIONAL` in phase docs are hypotheses to
  falsify, not requirements to force; a miss requires analysis in
  `DECISIONS.md`, not silent threshold edits.
- Real `.husb` captures and live-system access are prohibited before Phase 8
  except the read-only geometry JSON noted in `README.md`.
