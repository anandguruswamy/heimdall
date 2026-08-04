# Phase 5 — Geometry-Conditioned Set Network

Objective: implement the paper's architecture (Sec. IV-V): shared link
encoder, geometry-aware set transformer, and 48-query primitive set decoder,
verified for permutation invariance and masking, at 5-8M parameters.

Prerequisites: Gate N4 (positive or user-approved-conditional verdict).

## Steps

1. `nrecon/model/preprocess.py` — the network-facing input pipeline, shared
   verbatim between synthetic and (later) real data:
   - `from_i16` scaling (Eq. (7)); differentiable fractional alignment of
     the first path to marker `F0 = 16` (Eq. (8), using
     `nrecon.sim.delay.fractional_shift`); common-phase removal against the
     direct-path template and robust amplitude normalization (Eq. (9)) with
     `log a_ij` and removed phase appended to metadata.
   - **Real-data source (Phase 8):** the live UNO Q CIR tab already performs
     the gain/phase/timing fit (`linear_ls` or `robust_grid`; fallback to
     base-aligned when fit is `off`) and publishes a fitted "display" CIR
     plus fit diagnostics to every CIR-derived product. The preprocessor
     must therefore also accept the live fitted CIR *as-is* and map the
     fit's per-link estimates onto the metadata (log `a_ij`, removed phase,
     marker `f_ij` from `marker_aligned`, DGC, accumulation count, CFO),
     instead of re-fitting raw CIRs host-side. Synthetic records remain the
     primary training path; the live fitted input is used for Phase 8
     fine-tuning and evaluation.
   - Channels Eq. (10): `[Re, Im, log(eps + |.|)]` -> `[L, 64, 3]`.
   - Metadata vector Eq. text (Sec. IV-B): marker `f_ij`, `log a_ij`, DGC,
     accumulation count, CFO, quality flags, `t_in_cycle`, missing mask.
   - Geometry features Eq. (11): centered/RMS-scaled positions, baseline
     vector, length, and the scale `s_p` itself in metres.
2. `nrecon/model/encoder.py`: 1D CNN — 4 residual stages, widths
   32/64/96/128, kernels 7/5/5/3, strided downsampling, GroupNorm, GELU,
   attention pooling to `e_cir in R^128` (Sec. V-A). Metadata MLP, Fourier
   features of geometry + MLP, learned direction-role embedding; token sum
   per Eq. (12).
3. `nrecon/model/set_encoder.py`: 6 pre-norm transformer blocks, width 128,
   4 heads, FFN 512; attention-logit bias MLP over pairwise link geometry
   (baseline directions, midpoint separation, shared-node indicators);
   key-padding mask from `link_valid`. No node-ID embeddings (Sec. V-B).
4. `nrecon/model/decoder.py`: `G_MAX = 48` learned queries, 4
   self/cross-attention blocks; heads (Sec. V-C): type logits (4), presence,
   center (3), rot6d (6), log-scales (3), complex reflectivity (2),
   roughness, attenuation, dynamic probability, and log-sigmas for center,
   scales, rotation (bounded log variance).
5. `nrecon/model/net.py`: `HeimdallSetNet(cfg)` assembling 1-4;
   `count_parameters()` must land in 5-8M — if outside, adjust FFN width
   within Table II's spirit and record in `DECISIONS.md`.
6. Tests (`tests/test_model.py`):
   - Shapes for full and partially-masked link sets.
   - Parameter count in [5e6, 8e6].
   - Node-relabel invariance: permute node labels (and therefore link order
     and geometry features consistently); outputs must match up to slot
     permutation — verify by Hungarian-matching predicted slots between the
     two runs with cost = parameter distance; max matched distance <1e-4
     (float64, eval mode).
   - Missing-link mask: zeroing a masked link's CIR content must not change
     outputs (mask correctness).
   - Preprocess round-trip on synthetic shards: no NaN, alignment places
     envelope peak near marker 16 for LOS-dominant links.
7. Torch determinism: seed + `torch.use_deterministic_algorithms(True)` in
   tests; document any op that forces an exception in `DECISIONS.md`.

## Exit Gate N5

- [ ] All model tests pass, including invariance and masking.
- [ ] Parameter count recorded in `DECISIONS.md`.
