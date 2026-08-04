# Phase 8 — Real-Data Transfer (Leaves Subfolder-Only Scope)

Objective: replace assumed simulator contracts with measured hardware
reality and fine-tune on controlled real scenes (Algorithm 1 steps 5-6;
paper Sec. VIII-B/E, Sec. X). This phase requires explicit user approval,
physical access to the deployment, and coordination with the live system.

Prerequisites: Gate N7 with a "go" decision; user approval.

## Hard prerequisites before any metric claim (from paper and STATUS.md)

- Per-board antenna delay and phase-center calibration (STATUS.md records
  antenna calibration as still outstanding; the bring-up value 16385 DTU is
  explicitly uncalibrated).
- Surveyed node geometry: tape/laser-surveyed antenna phase centres in a
  documented frame. Browser-solver fit consistency is not a substitute
  (radar-map README warning).

## Steps

1. Hardware statistics fitting (`nrecon/real/stats.py`, read-only replay):
   - Replay protected clips (e.g., clip 6) through the existing host
     tooling to canonical observations; fit distributions for DGC,
     accumulation count, complex noise covariance, first-path jitter, link
     gain, false-first-path rate, missing-link rate (paper Sec. VIII-B).
   - Prefer the live CIR tab's per-link fit estimates (see below) as the
     direct source for link gain, phase coherence, and first-path jitter
     statistics; reserve raw-CIR-derived fits for quantities the live fit
     does not expose (noise covariance, DGC/accumulation distributions).
   - Regenerate stage-3/4 datasets with fitted parameters; record deltas
     against the assumed defaults in `DECISIONS.md`.
2. Live aligned/fitted CIR as the real-data input contract:
   - The UNO Q CIR tab fits per-link gain/phase/timing (`linear_ls` or
     `robust_grid`, fallback base-aligned) and publishes one "display" CIR
     per frame to the CIR, waterfall, and FFT products. The network input
     for real data is that fitted display signal plus its fit metadata
     (marker `f_ij` from `marker_aligned`, per-link gain/phase/timing
     estimates, DGC, accumulation count, fit algorithm), consumed via the
     Phase 5 preprocessor's real-data source — not a host-side re-fit of
     raw CIRs.
   - Captures must record the fit mode and fitted metadata alongside the
     CIRs so the input is reproducible across replays (the live pipeline's
     `processing_epoch` semantics mean products are consistent only within
     one processing configuration).
3. Measured link kernels:
   - From open-LOS captures at surveyed distances, estimate per-link
     accumulator kernels; fit the residual FIR `b_ij` against template v1
     (paper Eq. (14) discussion, Sec. X "Pulse mismatch").
   - Replace/augment the synthetic kernel: retrain or fine-tune run 4 with
     measured-kernel randomization.
4. Calibration campaign (paper Sec. VIII-E) — each item is a scheduled
   physical session with a capture checklist and surveyed ground truth:
   a. open LOS at surveyed distances/orientations;
   b. empty surveyed room;
   c. one reflective panel at known poses;
   d. human-sized absorber on a marked grid;
   e. one person at known positions;
   f. incrementally added furniture;
   g. node rotations/replacements.
   Store captures under the existing protected-capture mechanism; label
   files (primitive ground truth per capture) live in this subfolder.
5. Fine-tuning:
   - Stage 5: supervised fine-tune on labeled controlled scenes with
     primitive labels + CIR reconstruction.
   - Stage 6: conservative unlabeled adaptation on operational captures
     with CIR reconstruction only, anchored to the synthetic prior
     (small lr, strong regularization; watch for degenerate scene drift).
6. Real evaluation: repeat the Phase 7 protocol on held-out real sessions
   (held-out scenes and held-out links); compare against dense
   backprojection on identical (fitted) inputs; report in
   `reports/N8-real-evaluation.md`.
7. Only after N8: consider deployment integration questions (UNO Q vs
   off-board inference, cadence, static/dynamic decomposition). These are
   out of scope for this plan and require a new plan document.

## Exit Gate N8

- [ ] Calibration prerequisites completed and documented.
- [ ] Fitted hardware statistics and measured kernels committed (manifests).
- [ ] Stage 5/6 fine-tunes evaluated on held-out real data;
      `reports/N8-real-evaluation.md` committed.
