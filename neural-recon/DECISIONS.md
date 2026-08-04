# Decision Log

Append dated entries. Never rewrite history; supersede with a new entry.

## 2026-08-04 Folder and package layout

- Subfolder `neural-recon/` at repo root; Python package `nrecon` with
  submodules `sim`, `baselines`, `model`, `train`, `eval`.
- Rationale: single package keeps renderer, data, and model import paths
  coherent for tests; subfolder isolation follows the user's instruction to
  stay self-contained until synthetic validation completes.

## 2026-08-04 Physical constants

- `C_AIR = 299_702_547.0` m/s adopted from
  `host-tools/radar-map/radar_map/model.py` for consistency with existing
  Heimdall processing; carrier `FC_HZ = 7.9872e9` (channel 9 per the
  qualified N=5/M=2 profile in `STATUS.md`); accumulator rate 998.4 MHz and
  64 taps per `docs/papers/neural-uwb-scene-reconstruction.html` Table I.

## 2026-08-04 Pulse template v1 is an assumed contract

- The paper (Sec. VI-A) treats the MP-SRRC template as a dataset-generator
  contract, not a normative closed form. Template v1 will be a beta=0.5 SRRC
  at 499.2 MHz bandwidth, truncated and edge-tapered, energy-normalized,
  sampled at 16x the accumulator rate. `PROVISIONAL`: to be replaced by
  measured link kernels in Phase 8 before any sim-to-real claim
  (paper Sec. X, "Pulse mismatch").

## 2026-08-04 Torch versions deferred to Phase 0

- Exact `torch`/`numpy`/`scipy` pins are chosen and recorded during Phase 0
  on the actual host, then added to `tools/README.md` per the workspace
  tooling rule. CPU-only development locally; CUDA training host is a Phase 6
  decision point.
