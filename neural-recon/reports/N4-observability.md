# N4 — Observability Verdict (Per-Scene Optimization + Baselines)

Date: 2026-08-04. Experiments V1/V2 per `plan/phase-4-baselines.md`;
hardware-faithful targets (i16 quantization at the median accumulation
count 108, marker ~16.1 taps, live geometry).

## 1. Setup

- Targets are stage-1-style records: noiseless render + per-link
  gain/phase/first-path nuisance at fixed live geometry, quantized through
  the exact hardware transport (`to_i16`).
- The fit optimizes `SceneTensors` leaves (center, rot6d, scale_log, rho,
  presence) with Adam through UWBRender; loss = envelope-only for the first
  half, then phase-invariant complex Charbonnier (Eq. 25) + envelope
  (Eq. 26) + presence/scale/plane-overlap regularizers; presence projected
  to [0, 1] each step.
- Scene evidence reality check: at accum = 108 the LOS is ~8-16 i16 units
  and echoes are 1-3 units; distant or specular-unfavourable echoes
  quantize away entirely on some links. Dense DAS and sparse voting are
  therefore bounded by shell-quantization and near-flat evidence plateaus.

## 2. V1 — single surfel, random multistart K=8 (PROVISIONAL success < 10 cm)

Corrected-loss run (see revision note below):

| scene | best restart err [m] | success |
|---|---|---|
| 0 | 0.42 | no |
| 1 | 0.97 | no |
| 2 | 0.21 | no |
| 3 | 0.69 | no |
| 4 | 0.45 | no |
| 5 | 0.34 | no |
| 6 | 0.66 | no |
| 7 | 0.45 | no |

- Success rate: 0/8; best-restart median 0.45 m (was 1.75 m before the
  multiscale/volume-centroid improvements; the volume-centroid restart is
  consistently the best).
- Finding (paper-anticipated pathology): the loss is flat outside the
  echo-overlap basin (~0.2-0.3 m at the fine scale); the multiscale
  schedule pulls from ~1-2 m but stalls at ~0.2-0.5 m (the quantized
  plateau steps stop the pull). The optimizer works inside the basin;
  initialization from scratch remains unsolved with 1-2-unit quantized
  echoes, which is the Phase 5 network's job (amortized search).

## 3. V2 — decisive experiment (2-4 planes + 1 surfel)

Corrected-loss run, gt_perturbed (5 cm / 5 deg), 20 scenes, 350
iterations, 4 held-out links:

| metric | median | <= target | worst |
|---|---|---|---|
| surfel center err [m] | 0.187 | 6/20 < 0.10; 9/20 < 0.15 | 0.371 |
| plane normal err [deg] | 12.3 | 5/53 <= 5; 20/53 <= 10 | 45.5 |
| plane offset err [m] | 0.131 | 21/53 <= 0.10 | 1.29 |
| held-out link env residual | ~0.0025 | below the 1-unit quantization step | 0.0033 |

- The surfel is genuinely refined to ~0.05-0.2 m (6/20 under 0.10 m),
  which is the evidence-limited accuracy: the quantized delay evidence
  (1-3 i16 units, integer-tap plateaus) pins the position to ~0.1-0.2 m.
- Plane recovery is partial: ~1/3 of planes reach <= 10 deg, ~1/8 fail
  badly (> 25 deg): the plane-rotation local minima anticipated as
  "no-valid-reflection basins". Extending to 1200 iterations or lowering
  the envelope weight did not escape these basins.
- Initializers: volume-centroid init (median 0.80 m) beats voting
  (median 2.0 m); both remain outside the convergence basin.

## 4. Envelope baselines on the same evidence

- Dense backprojection (with template LOS subtraction): argmax 0.3-0.5 m
  from the truth on well-conditioned scenes; the volume is near-flat
  (truth voxel within 1-3% of the argmax) because quantized echoes flatten
  the shells. Occasional 1.5+ m outliers on plateau-degenerate volumes.
- Ellipsoid voting: best candidate 0.6-2.3 m (median ~0.9 m).

## 5. Verdict

CONDITIONAL POSITIVE, with a documented limitation:

1. The differentiable UWBRender + per-scene optimizer CAN recover simple
   known scenes from a good initializer: single-surfel fits reach
   0.04-0.13 m (Phase 4 test) and 0.05-0.37 m with planes present
   (median 0.19 m, evidence-limited).
2. The decisive experiment's PROVISIONAL targets are NOT met: surfel
   median 0.187 m vs 0.10 m target (evidence-limited); plane recovery is
   unreliable (median normal 12.3 deg vs 5 deg target), with ~1/8 planes
   trapping in rotation basins. This is a loss-landscape / initialization
   pathology, not a renderer defect (renderer gradients are validated to
   <1e-3 in Gate N2).
3. Random multistart, voting, and volume-centroid initializers cannot
   reach the basin from scratch with this evidence quality; the network
   (Phase 5) is expected to supply the type/cardinality/coarse-pose
   search that the optimizer lacks.

## 6. Revision note (2026-08-04, corrected loss)

The original run used a wrong render-to-pipeline scale
(`h * gain/accum / 4` instead of `h / 4`; the gain/accum factors cancel),
which shrank the rendered CIR ~108x and made the loss amplitude-dominated
and nearly position-independent; those fits "converged" only because the
gt_perturbed inits were already near the truth. After the fix (plus
per-link envelope normalization, LOS-tap exclusion, [0,1] presence
projection, and a matched-smoothing multiscale schedule), the optimizer
genuinely optimizes: V1 best-restart median improved 1.75 -> 0.45 m and
the gt_perturbed fits converge to the evidence limit. All numbers in
sections 2-3 above are from the corrected run; the earlier published
numbers in `runs/fit/*/summary.json` were overwritten.
