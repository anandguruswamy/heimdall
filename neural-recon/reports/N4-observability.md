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

| scene | best restart err [m] | success |
|---|---|---|
| 0 | 1.52 | no |
| 1 | 2.09 | no |
| 2 | 1.84 | no |
| 3 | 2.09 | no |
| 4 | 1.66 | no |
| 5 | 1.32 | no |
| 6 | 1.13 | no |
| 7 | 2.22 | no |

- Success rate: 0/8 (target 1/8+); median best-restart error 1.75 m.
- Finding (paper-anticipated pathology): the log-envelope loss is flat
  outside the echo-overlap region (basin ~0.2-0.3 m); random restarts over
  the ~26 m^3 search volume essentially never land inside a basin. Best
  restarts do show the lowest final loss (loss correlates with proximity),
  confirming the optimizer works inside the basin; the failure is
  initialization, not optimization.
- Fix direction (Phase 5+): initialize from the DAS/voting candidates or
  a coarse-to-fine schedule; the voting init failed too (below), so the
  initializer itself needs the finer evidence.

## 3. V2 — decisive experiment (2-4 planes + 1 surfel)

gt_perturbed (5 cm / 5 deg), 20 scenes, 350 iterations, 4 held-out links:

| metric | median | <= target | worst |
|---|---|---|---|
| surfel center err [m] | 0.124 | 7/20 < 0.10 | 0.234 |
| plane normal err [deg] | 10.9 | 10/53 <= 5; 24/53 <= 10 | 48.7 |
| plane offset err [m] | 0.136 | 22/53 <= 0.10 | 1.39 |
| held-out link env residual | 0.0026 | below the 1-unit quantization step | 0.0032 |

- The surfel is reliably refined to ~0.12 m in the presence of planes.
- A substantial fraction of planes are recovered accurately (10/53 <= 5
  deg, 22/53 offset <= 10 cm), but ~1/6 fail badly (> 25 deg): the
  plane-rotation local minima anticipated as "no-valid-reflection basins"
  and delay-association pathologies. Extending to 1200 iterations or
  lowering the envelope weight did not escape these basins.
- voting init: surfel candidates 1.0-3.8 m from truth (median 2.0 m) — the
  coarse quantized shells place every candidate outside the basin. No
  voting-init success; the finding is that the voting initializer needs
  the 16x display envelope or peak-refined shells to be useful.

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
   0.04-0.13 m (Phase 4 test) and 0.05-0.23 m with planes present.
2. The decisive experiment's PROVISIONAL targets are NOT met: plane
   recovery is unreliable (median normal 10.9 deg vs 5 deg target), and
   ~1/6 planes trap in rotation basins. This is a loss-landscape /
   initialization pathology, not a renderer defect (renderer gradients are
   validated to <1e-3 in Gate N2 and the surfel evidence fits cleanly).
3. Random multistart and coarse voting cannot initialize the optimizer for
   this evidence quality; the network (Phase 5) is expected to supply the
   type/cardinality/coarse-pose search that the optimizer lacks.

Per the plan, proceeding to Phase 5 requires user approval under this
conditional verdict (see DECISIONS.md 2026-08-04 entry).
