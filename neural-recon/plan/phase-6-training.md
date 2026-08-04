# Phase 6 — Losses and Curriculum Training

Objective: implement the hybrid objective (paper Sec. VII) and execute
curriculum runs 1-4 (Algorithm 1 steps 1-4; steps 5-6 belong to Phase 8).

Prerequisites: Gate N5.

## Compute-host decision (blocking)

Local host is CPU-only (Windows ARM64 laptop). Run 1 (overfit 100 scenes)
is feasible on CPU. Runs 2-4 (10k+ scenes, 150k-300k steps, batch 64-256,
mixed precision — paper Sec. VIII-D) require a CUDA GPU host.
Before starting run 2: present the user the measured run-1 step time, an
extrapolated runs-2-4 duration on CPU vs a 24 GB-class GPU, and ask where to
train. Record the decision in `DECISIONS.md`. Do not silently start
multi-hour CPU runs.

## Steps

1. `nrecon/train/losses.py`:
   - Hungarian matching on a detached cost of type, center, extent,
     orientation (Eq. (23)) via `scipy.optimize.linear_sum_assignment`,
     batched.
   - Set loss Eq. (24): type CE, presence BCE (unmatched slots -> empty),
     center Gaussian NLL with predicted sigma (bounded log-variance),
     symmetry-aware rotation distance (ignore tangent-axis flips for
     planes/surfels, axial rotation for capsules), log-scale L1, rho L1.
   - Render losses: phase-invariant complex Charbonnier Eq. (25),
     log-envelope Eq. (26), 64-point FFT magnitude term; pre-first-path
     samples down-weighted for scene terms but kept for noise calibration
     (Sec. VII-B).
   - Regularizers Sec. VII-C: occupied-slot penalty, giant-surfel penalty,
     near-zero-thickness penalty, plane-overlap penalty,
     uncertainty-consistency penalty. Optional surface Chamfer when dense
     surface samples exist in the shard.
   - Initial weights `(set, cpx, env, fft, surf, reg) = (1, 1, 0.5, 0.1,
     0.25, 0.01)` then running-grad-norm normalization (paper values;
     `PROVISIONAL`).
   - Unit tests: matching correctness on hand-built cases; loss decreases
     under a manual step toward truth; rotation symmetry cases give zero
     distance; all losses finite under masked links and empty scenes.
2. `nrecon/train/loop.py`: config-driven trainer — AdamW lr 3e-4, weight
   decay 1e-2, 5k warm-up, cosine decay, grad clip 1.0, AMP on CUDA;
   dataloader over Phase 3 shards with per-epoch node-label permutation
   augmentation (stage 4); checkpoint/resume; CSV + optional TensorBoard
   logging of every loss term, matched-primitive metrics, and val metrics;
   fixed val subset rendered to envelope-overlay plots each eval for human
   inspection.
3. Renderer-in-the-loop: reconstruction losses require rendering predicted
   scenes each step. Profile early (run 1); if rendering dominates step
   time, vectorize path enumeration before scaling up (paper Sec. VIII-D
   warns of exactly this bottleneck).
4. Curriculum configs and runs (`configs/train-run<k>.yaml`):
   - **Run 1** — stage1 dataset (100 noiseless fixed-geometry 1-3-surfel
     scenes), target: overfit. Success: train set loss near zero, median
     matched surfel center error <5 cm, CIR residual visually at noise
     floor, healthy gradient norms through UWBRender (Algorithm 1 step 1's
     purpose: verify gradient flow). CPU-feasible; announce duration.
   - **Run 2** — stage2 (10k scenes, planes added, 1-5 cm geometry jitter).
   - **Run 3** — stage3 (capsules + full hardware randomization). Includes
     ablation flag to confirm nuisance marginalization (phase-invariant
     loss) is actually helping.
   - **Run 4** — stage4 (randomized layouts, label permutation every
     epoch). This is the model evaluated in Phase 7.
   - For runs 2-4 record: final train/val losses, primitive metrics by
     type, and val envelope overlays; store run manifests in `runs/` with
     config hash, dataset hashes, git rev.
5. Failure handling: if a run plateaus with degenerate outputs (all-empty
   scenes, presence collapse, giant surfels absorbing everything), apply
   the documented mitigations in order (re-weight presence penalty,
   longer envelope-only warm-up, lower lr) and log each attempt in
   `DECISIONS.md`; do not iterate silently past 3 attempts without user
   consultation.

## Exit Gate N6

- [ ] Loss unit tests pass.
- [ ] Run 1 overfit criteria met and documented (gradient-flow proof).
- [ ] Compute-host decision recorded; runs 2-4 completed on the chosen
      host with manifests and metrics logged.
- [ ] Run 4 checkpoint tagged as the Phase 7 evaluation candidate.
