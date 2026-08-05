# N7 — Evaluation Report (first pass)

Date: 2026-08-05. Evaluates `runs/train-run4/checkpoint.pt`, the curriculum
checkpoint from the 2026-08-05 overnight training run (see `DECISIONS.md`
for the full curriculum account and the five infrastructure bugs found
completing it).

## Scope of this pass (PROVISIONAL, pragmatic first cut)

`plan/phase-7-evaluation.md` specifies a large protocol: 3 metric families
(primitive recovery, physical consistency, scene utility), 6 systems
(network, backprojection, voting, optimizer-random, optimizer-voting,
hybrid), 4 stratified test sets, a decisive experiment (D1), ablations, and
calibration diagrams. Given the checkpoint under evaluation was trained on
a deliberately slimmed step budget (see "Interpretation" below), a full
academic-grade protocol was not run before getting concrete numbers in
front of the user. **This pass implements:**

- `nrecon/eval/metrics.py`: primitive-recovery metrics (type accuracy,
  plane normal/offset error, surfel center/covariance-Frobenius error,
  capsule center/size error), matched via the Phase 6 Hungarian cost
  (`nrecon.train.losses.match_slots`); a held-out-link physical-consistency
  check (mask additional links from the network's input, render the
  predicted scene, compare against the target only on the withheld links).
  Unit-tested on hand-built scenes with known errors (6 tests,
  `tests/test_eval_metrics.py`).
- `nrecon/eval/protocol.py`: evaluates the **"network" system only** over
  one test set, reporting the above metrics plus per-scene runtime.
- One test set, **`test-fixed`**: `configs/eval-test-fixed.yaml`, 500
  scenes, fixed live geometry, stage-3/4-equivalent scene complexity
  (capsules, full hardware nuisance, the reverb-tail model), seeds
  (`base_seed: 900000`) disjoint from every training stage.

**Deferred** (not run this pass): extent IoU, surface Chamfer distance,
voxel occupancy IoU, path-delay-vs-privileged-tables, cross-link
consistency, uncertainty-calibration reliability diagrams; the other 5
systems; `test-rooms`/`test-geom`/`test-hard`; the decisive experiment D1;
ablations. See "Recommended next steps" below.

## Results (500 scenes, `test-fixed`)

Primitive recovery (Hungarian-matched against ground truth; "recall" here
is trivially 1.0 because the model always has more slots (48) than any
scene has truth primitives, so the assignment problem always finds a
match for every truth primitive — it is not a meaningful detection-recall
number until compared against a `type_accuracy`/`n_pred_unmatched`
reading):

| Metric | Median | Mean | n |
|---|---|---|---|
| Type accuracy (of matched pairs) | -- | 38.9% | 6827 matched |
| Predicted "false positives" (confident, unmatched) | -- | 1124 / 6827 truth | -- |
| Plane normal error | 17.3 deg | 32.2 deg | 4673 |
| Plane offset error | 0.70 m | 0.77 m | 4673 |
| Surfel center error | 0.64 m | 0.68 m | 1634 |
| Surfel covariance Frobenius error | 0.14 | 0.31 | 1634 |
| Capsule center error | 0.64 m | 0.68 m | 520 |
| Capsule half-length error | 0.41 m | 0.43 m | 520 |
| Capsule radius error | 0.12 m | 0.12 m | 520 |

Held-out-link physical consistency (4 additional links masked from the
network's input per scene, predicted scene rendered and compared against
the target only on those links):

| Metric | Median | Mean | n |
|---|---|---|---|
| Complex (phase-invariant Charbonnier) error | 0.0096 | 0.0103 | 500 |
| Log-envelope error | 1.05 | 1.05 | 500 |

## Interpretation

**All primitive-recovery numbers are far from the plan's targets**
(plane normal <5 deg / offset <5 cm; surfel/capsule center <10 cm; see
`plan/phase-4-baselines.md`'s per-scene-optimizer targets, which Phase 6
implicitly inherits): plane normal error is ~3-6x target, plane offset
~14x target, surfel/capsule center ~6-7x target. Type accuracy (38.9%)
is only modestly above the ~33% chance level for 3 non-empty primitive
types. Held-out-link envelope error (~1.05, i.e. roughly a factor-of-e
mismatch in log-magnitude) indicates the model's predicted scenes do not
yet explain unseen links well.

**This is expected, not a surprising failure.** `runs/train-run1..4`
totaled 280+1680+500+300 = 2760 optimizer steps across the whole
curriculum. `plan/phase-6-training.md` (quoting the paper, Sec. VIII-D)
specifies 150k-300k steps *per run* (600k-1.2M total) — this checkpoint
saw roughly 0.2-0.5% of the reference training budget. The step budget
was deliberately slimmed for tonight's run to validate the *pipeline*
(gradient flow, curriculum warm-starting across stage boundaries, and,
as it turned out, five real infrastructure bugs only reachable by many
real GPU optimizer steps — see `DECISIONS.md`) within a practical
overnight time/cost budget, not to produce a converged model. It
succeeded at that: the curriculum completed end-to-end with healthy,
monotonically-plateauing loss curves at every stage and no crashes or
divergence, which is what "the pipeline works" looks like. It was never
expected to be close to the paper's converged numbers.

## Go/no-go recommendation

**No-go for Phase 8 (real-data transfer) with the current checkpoint.**
The primitive-recovery and held-out-link numbers above are not close
enough to the plan's targets to be a meaningful real-data candidate; a
Phase 8 attempt now would mostly be testing "is the model undertrained",
which is already known. This is not a recommendation to revisit the
architecture or the fixes made tonight — the curriculum infrastructure
(warm-starting, NaN/exception/loss-spike guards, the simplified
metadata-free network, the reverb-tail-augmented data) is now validated
and working. The gap is training budget, not a design flaw uncovered by
this evaluation.

## Recommended next steps

1. **Train substantially longer** before re-evaluating: either the full
   150k-300k-steps-per-run reference budget (many more GPU-hours, likely
   the right call now that the pipeline is proven stable across a full
   unattended run) or a deliberately chosen larger-but-still-bounded
   step budget, decided with the user given the cost/time trade-off (see
   `DECISIONS.md`'s 2026-08-05 GPU-sanity entries for the throughput
   numbers this can be projected from: ~0.08-0.28 s/step on the
   Threadripper+RTX 4090 host used tonight).
2. Re-run this same `test-fixed` evaluation against the longer-trained
   checkpoint for a direct before/after comparison (the harness is now
   in place and takes ~2-3 minutes on CPU for 500 scenes).
3. Only after numbers approach the plan's targets, invest in the deferred
   protocol breadth (other systems/baselines for comparison, the
   remaining 3 test sets, ablations, D1, calibration diagrams) — running
   the full protocol against a still-undertrained checkpoint would mostly
   restate "undertrained" in more expensive ways.
