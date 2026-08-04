# Phase 3 — Procedural Scenes and Curriculum Datasets

Objective: deterministic procedural scene generation, hardware nuisance
randomization, and export of curriculum datasets (paper Sec. VIII) with a
validated storage schema.

Prerequisites: Gate N2.

## Steps

1. `nrecon/sim/scenes.py` — samplers, all driven by one `np.random.Generator`:
   - `sample_room(rng, cfg)`: shoebox of 4 walls + floor + ceiling as finite
     planes (paper: rooms contain 4-8 finite architectural planes), sizes
     3-8 m x 3-8 m x 2.2-3.2 m (`PROVISIONAL`); optional 0-2 interior
     partition planes. Reflectivity/roughness drawn from broad ranges.
   - `sample_furniture(rng, cfg)`: 0-4 items as small plane assemblies plus
     anisotropic surfels with common furniture dimensions.
   - `sample_people(rng, cfg)`: 0-2 capsules, height 1.5-1.95 m, radius
     0.12-0.22 m, randomized attenuation, `dynamic_p = 1`.
   - `sample_nodes(rng, cfg)`: N=5 non-coplanar perimeter layouts spanning
     multiple heights, minimum pairwise separation 0.6 m (`PROVISIONAL`);
     rejection-sample against coplanarity (smallest singular value of
     centered positions above threshold). Mode `fixed_live` loads
     `deployment/radar-geometry.live-20260728.json` and applies optional
     1-5 cm jitter (curriculum stage 2, Algorithm 1).
   - Scene assembly caps occupied slots at `G_MAX` and returns
     `SceneTensors` plus a ground-truth primitive list.
2. `nrecon/sim/hardware.py` — nuisance model (paper Sec. VIII-B), all
   configurable distributions with defaults anchored to the published real
   statistics: first-path marker median 16.109 taps, peak offset median
   1.69 taps, accumulation count median 108, dominant DGC 3-6:
   - per-link gain `a_ij` (log-normal), phase `psi_ij` (uniform), complex
     AWGN with per-link std, DGC state, accumulation count, CFO, first-path
     jitter, residual FIR `b_ij` (short, near-delta, small taps),
     missing-link mask, false-first-path event injection (large marker jump
     mimicking the known ~72-sample CIA anomaly).
   - Stage configs may disable any subset (stage 1 is noiseless).
3. `nrecon/sim/export.py` — shard writer/reader:
   - Shard = one `.npz` per 256 scenes + one `manifest.jsonl` line per scene.
   - Arrays per shard (B = scenes in shard):
     `cir_i16 [B,L,64,2] i16`, `link_valid [B,L] bool`,
     `fp_q10_6 [B,L] i32`, `cir_start [B,L] i32`, `dgc [B,L] i8`,
     `accum [B,L] i16`, `cfo [B,L] f32`, `t_in_cycle [B,L] f32`,
     `node_pos [B,N,3] f32`,
     labels: `prim_type [B,G] i8`, `prim_present [B,G] f32`,
     `prim_center [B,G,3] f32`, `prim_rot [B,G,3,3] f32`,
     `prim_scale [B,G,3] f32`, `prim_rho [B,G,2] f32`,
     `prim_rough [B,G] f32`, `prim_atten [B,G] f32`,
     `prim_dynamic [B,G] f32`,
     nuisance: `link_gain`, `link_phase`, `noise_std [B,L] f32`,
     `resid_fir [B,L,K_b,2] f32`.
   - Manifest line: scene seed, room id, layout id, stage, config hash,
     generator git rev, pulse manifest hash, split tag.
   - Split policy: assignment to train/val/test by hash of (room seed) —
     never by adjacent snapshots (paper Sec. VIII-D). Test additionally
     holds out entire room-topology families.
4. CLI `python -m nrecon.sim.build --config configs/dataset-<stage>.yaml
   --out datasets/<stage>` with configs:
   - `dataset-stage1.yaml`: 100 scenes, fixed live geometry, 1-3 surfels,
     noiseless, no nuisance (Algorithm 1 step 1).
   - `dataset-stage2.yaml`: 10,000 scenes, fixed geometry + 1-5 cm jitter,
     rooms with finite planes + surfels (step 2).
   - `dataset-stage3.yaml`: stage 2 plus capsules, occlusion, residual
     kernels, gain/phase/DGC/noise/missing-link randomization (step 3).
   - `dataset-stage4.yaml`: randomized non-degenerate N=5 layouts, multiple
     layouts rendering the same scene (step 4; paper Sec. VIII-A).
   - Each config declares scene counts, and the builder prints scenes/sec
     and ETA up front; warn the user before builds expected to exceed 30 s.
5. Validation tool `python -m nrecon.sim.validate datasets/<stage>`:
   - Schema check every array/dtype/shape; manifest completeness.
   - Determinism: rebuild 5 random scenes by seed, require bit-identical
     `cir_i16`.
   - Consistency: re-render from stored labels + stored nuisance, quantize,
     require bit-identical `cir_i16` for 5 random scenes.
   - Split disjointness by room seed.
6. Tests (`tests/test_scenes.py`, `tests/test_export.py`): sampler bounds
   and non-coplanarity; capsule/person parameter ranges; shard round-trip;
   determinism; the validator passing on a 8-scene mini-build of each stage
   (mini-builds run in CI-time, seconds).

## Exit Gate N3

- [ ] Mini-builds of all four stages pass `nrecon.sim.validate`.
- [ ] Full `stage1` (100 scenes) built and validated; larger stages built
      only when Phase 6 needs them (they are minutes-long operations —
      announce duration first).
- [ ] Determinism and re-render consistency demonstrated bit-exactly.
- [ ] All tests pass.
