# Phase 2 — UWBRender Forward Model

Objective: implement the differentiable renderer of paper Sec. VI mapping a
typed primitive scene plus node geometry to all 20 directed 64-tap complex
CIRs, with verified gradients w.r.t. every continuous scene parameter.

Prerequisites: Gate N1.

## Design constraints (from the paper)

- Sparse evaluation: only `W_SUPPORT = 16` samples around each path delay
  (Eq. (21), Sec. VI-F). Never materialize a dense volume.
- Complexity target O(L * W * (P + Gs + Kc*C)) (Eq. (22)).
- LOS term always rendered; scene paths are LOS-relative delays
  (Eq. (4), (21)).
- Nuisance gain `a_ij`, phase `psi_ij`, and noise are applied at assembly
  (Eq. (21)); carrier phase term `exp(-j 2 pi FC ell / c)` per Eq. (20).

## Steps

1. `nrecon/sim/primitives.py`:
   - `SceneTensors` dataclass of torch tensors over `G` slots: `type_id`
     int64 in {0 empty, 1 plane, 2 surfel, 3 capsule}, `presence` [G],
     `center` [G,3], `rot6d` [G,6], `scale_log` [G,3], `rho` complex [G],
     `roughness` [G], `atten` [G], `dynamic_p` [G].
   - `rot6d_to_matrix` (Gram-Schmidt, differentiable) with tests against
     random rotation matrices.
   - Type-specific views per paper Sec. V-C: plane (normal + tangents +
     half-extents from scale[0:2]), surfel (covariance
     `R diag(s^2) R^T`), capsule (axis, half-length scale[0], radius
     scale[1]).
2. `nrecon/sim/render.py` — pure functions, batch over links:
   - `render_los(nodes, link_index, kernel) -> h_los` at delay 0 with
     free-space amplitude `1/d^(gamma/2)` and carrier phase.
   - `render_surfel(...)`: unit vectors Eq. (15); delay variance Eq. (16);
     amplitude Eq. (20) with isotropic `D_i = D_j = 1` v1 hooks and
     Lambertian-plus-specular `B_g` controlled by `roughness`
     (`PROVISIONAL` v1 form: `B = (1-r)*specular_lobe + r`); pulse =
     kernel convolved with a Gaussian of std `sigma_tau` implemented
     analytically on the fine grid (differentiable in `sigma_tau`); placed
     with `sample_kernel` at the fractional LOS-relative delay.
   - `render_plane(...)`: mirror Eq. (17); intersection Eq. (18) with a
     guarded denominator (`abs(den) < 1e-6` -> path disabled smoothly);
     soft finite-patch gate Eq. (19) with `epsilon_v = 0.05 m`
     (`PROVISIONAL`); path length via the two segments; specular amplitude
     with `rho` and incidence factor.
   - `render_capsule(...)`: fixed `K_c = 12` quadrature points
     (`PROVISIONAL`) generated deterministically on the capsule surface in
     canonical pose then transformed; each point rendered as a localized
     scatterer weighted by area, incidence, and soft self-visibility;
     additionally `chord_attenuation(ray, capsule)` — differentiable
     smooth ray-capsule chord length applied multiplicatively to the LOS
     term and to every plane/surfel path whose segments pass the capsule
     (paper Sec. VI-D).
   - `render_scene(scene: SceneTensors, nodes [N,3], kernel, nuisance,
     noise) -> h_hat complex [L, 64]` assembling Eq. (21). `presence` and
     soft gates multiply amplitudes so gradients flow.
3. `nrecon/sim/quantize.py`:
   - `to_i16(h_float, dgc, accum) -> int16 [L,64,2]` reproducing the
     hardware export exactly: inverse of Eq. (7) scaling to accumulator
     units, saturate to signed 18-bit, arithmetic right shift by 2, cast
     `i16`, I-then-Q.
   - `from_i16` = the live pipeline's Eq. (7) scaling (mirror
     `host-tools/radar-map/radar_map/processing.py::_scaled_cir` semantics).
   - Training path: straight-through estimator or additive uniform noise
     flag (paper Sec. VI-E).
   - Test: `from_i16(to_i16(h))` matches `h` within quantization bound;
     integer path is bit-stable across runs.
4. Analytic geometry tests (`tests/test_render_geometry.py`), float64:
   - Point surfel (scale -> 0): rendered peak delay equals
     `excess_path / METRES_PER_TAP` from Eq. (4) within 0.05 tap, for 10
     random node pairs and positions.
   - Plane: rendered path length equals `|mirror(tx) - rx|`; specular point
     lies on the plane to 1e-9; moving the patch edge across the specular
     point sweeps the gate smoothly from ~1 to ~0.
   - Surfel broadening: measured second moment of the rendered envelope
     minus kernel second moment equals Eq. (16) within 10%.
   - Capsule occlusion: placing a capsule on the LOS segment attenuates
     `h[16]` (the marker tap) monotonically with radius.
   - Permutation: shuffling slot order leaves `render_scene` output
     identical to 1e-12.
   - Empty scene renders LOS only; `presence = 0` slot contributes exactly 0.
5. Gradient tests (`tests/test_render_grad.py`), float64, small scene
   (1 plane + 1 surfel + 1 capsule, 4 nodes):
   - Central finite differences vs autograd for: surfel center (3), scale
     (3), rot6d (6), rho (re/im), plane center/rot/extents, capsule
     center/axis/radius/half-length, presence. Relative error <1e-3
     elementwise where |grad| > 1e-8.
   - No NaN/Inf gradients when a plane is edge-on, a denominator is near
     zero, or a scale underflows (`scale_log` clamped at -6).
6. Performance smoke (`tests/test_render_perf.py`): full `G_MAX = 48` slot
   scene (mixed types), 20 links, forward + backward on CPU completes in
   <30 s (`PROVISIONAL`; record the measured time in the test log).
7. Demo: `python -m nrecon.sim.demo` renders a canned room (4 walls, floor,
   ceiling, 1 surfel, 1 capsule) at the fixed live Heimdall geometry
   (`deployment/radar-geometry.live-20260728.json`, read-only) and writes
   per-link envelope plots to `artifacts/demo/` (ignored).

## Exit Gate N2

- [ ] All geometry, gradient, quantization, and permutation tests pass.
- [ ] Performance smoke passes; measured time recorded in `DECISIONS.md`.
- [ ] Demo runs and plots are visually sane (LOS at marker tap ~16, plane
      echoes at plausible excess delays); note observations in
      `DECISIONS.md`.
