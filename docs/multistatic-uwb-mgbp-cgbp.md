# Robust Multistatic UWB Backprojection for Heimdall

## Median-Gated Backprojection (MGBP) and Consensus-Gated Backprojection (CGBP)

**Adaptation target:** Heimdall Radar Map dashboard tab
**Input contract:** the same aligned `instantaneous-cir` data already consumed by the existing Radar Map
**System profile:** five fixed DWM3001 nodes, 20 directed links, 64 complex CIR taps per observation

## 1. Purpose

Heimdall's current Radar Map constructs a three-dimensional volume by sampling each directed link's aligned CIR magnitude at the bistatic excess delay predicted for every voxel, weighting that evidence by link quality, and averaging across links.

This document proposes two additional reconstruction modes:

1. **Median-Gated Backprojection (MGBP):** rejects directed-link evidence that deviates excessively from the per-voxel median before averaging.
2. **Consensus-Gated Backprojection (CGBP):** retains a voxel only when a sufficient fraction of usable directed links report evidence above their noise floors, then averages the active evidence.

Both modes reuse the current Radar Map data path, geometry, grid, temporal profile construction, interpolation, direct-path guard, confidence output, point-cloud conversion, and visualization. They change only the final fusion of per-link evidence at each voxel.

These are experimental sensing modes. They do not remove Heimdall's need for surveyed antenna phase centres, per-board antenna-delay calibration, or real-scene validation.

## 2. Existing Radar Map Input

The dashboard receives aligned CIR frames from the `instantaneous-cir` stream. Depending on the selected Live CIR fit mode, the stream supplies:

- the base-aligned CIR (`Off`),
- the gain/phase/timing-fitted CIR (`Linear LS`), or
- the robust gain/phase/timing-fitted CIR (`Robust Grid`).

The Radar Map uses the already-produced display CIR. It must not repeat raw CIR decoding, DGC scaling, accumulation normalization, alignment, or gain/phase/timing fitting.

For a directed link $\ell=(i,j)$ from transmitter node $i$ to receiver node $j$, each accepted frame provides:

- transmitter and receiver IDs,
- `marker_aligned`,
- `match_score` or correlation,
- a 16-times-resampled magnitude vector when available, otherwise a magnitude vector at the native tap spacing,
- the selected display CIR produced by the live alignment/fitting pipeline.

The implemented Heimdall constants are:

| Quantity | Value |
|---|---:|
| Number of nodes $N$ | 5 |
| Directed links $L=N(N-1)$ | 20 |
| CIR taps | 64 |
| Accumulator sample rate $f_s$ | 998.4 MHz |
| Tap duration $T_s=1/f_s$ | approximately 1.0016 ns |
| Propagation distance per excess-delay tap $c_{\mathrm{air}}/f_s$ | approximately 0.3002 m |
| UWB centre frequency | 7.9872 GHz |
| UWB bandwidth | 499.2 MHz |

Unlike monostatic radar, there is no round-trip factor of two when converting Heimdall's bistatic excess path to taps.

## 3. Two Different Aggregation Axes

The implementation must distinguish temporal aggregation from multistatic fusion.

### 3.1 Frames within one directed link

The current `buildLinkProfiles` stage processes multiple time-adjacent frames for one link. It:

1. accepts frames meeting the configured match-score or correlation threshold;
2. rejects frames whose aligned first-path marker is too far from the link's median marker;
3. converts each accepted frame to a common excess-tap axis at $1/16$-tap resolution;
4. takes the temporal median magnitude at each excess tap;
5. zeros the direct-path guard interval.

This produces one robust profile $P_\ell(u)$ for directed link $\ell$, where $u$ is excess delay in taps.

MGBP and CGBP should preserve this stage unchanged in their first implementation. Frames are repeated observations of one channel; they are not additional spatial viewpoints.

### 3.2 Directed links at one voxel

The 20 directed links are the multistatic viewpoints. For a voxel $b$, the reconstruction predicts one excess delay for every available link, interpolates each link profile at that delay, and obtains a set of per-link evidence values.

The existing backprojection computes a quality-weighted mean of these values. MGBP and CGBP replace this cross-link mean.

## 4. Multistatic Geometry

Let:

- $\mathbf p_i\in\mathbb R^3$ be the transmitter phase-centre position;
- $\mathbf p_j\in\mathbb R^3$ be the receiver phase-centre position;
- $\mathbf q_b\in\mathbb R^3$ be the centre of voxel $b$;
- $d_{ij}=\|\mathbf p_i-\mathbf p_j\|$ be the direct-path baseline length.

For directed link $\ell=(i,j)$, a single-bounce point scatterer at $\mathbf q_b$ has total bistatic path length

$$
d_{i,b,j}=\|\mathbf q_b-\mathbf p_i\|+\|\mathbf q_b-\mathbf p_j\|.
$$

Because the CIR is aligned to the direct path, the Radar Map uses the excess path

$$
\Delta d_{\ell,b}
=\|\mathbf q_b-\mathbf p_i\|
+\|\mathbf q_b-\mathbf p_j\|
-\|\mathbf p_i-\mathbf p_j\|.
$$

The corresponding excess-delay tap is

$$
u_{\ell,b}=\frac{\Delta d_{\ell,b}}{c_{\mathrm{air}}/f_s}
=\frac{f_s\Delta d_{\ell,b}}{c_{\mathrm{air}}}.
$$

All points sharing the same $u_{\ell,b}$ lie on a prolate ellipsoid whose foci are nodes $i$ and $j$. A reflector becomes spatially localized where evidence from several differently oriented link ellipsoids intersects.

## 5. Per-Link Voxel Evidence

For every usable link $\ell$, interpolate its temporal-median profile at the voxel's predicted excess tap:

$$
x_{\ell,b}=P_\ell(u_{\ell,b}).
$$

The existing dashboard performs linear interpolation on the $1/16$-tap excess axis. The first MGBP/CGBP version should retain that interpolation so mode comparisons isolate the effect of the fusion rule.

A link is geometrically valid for voxel $b$ only if:

- both endpoint positions exist;
- $u_{\ell,b}$ lies inside the link profile's excess-tap support;
- the requested excess delay lies beyond the configured direct-path guard;
- the link profile passed the existing minimum accepted-frame and signal-quality gates.

Let $\mathcal V_b$ be the set of links valid at voxel $b$.

The current link-quality weight is

$$
w_\ell=\max(0.05,\widetilde\rho_\ell),
$$

where $\widetilde\rho_\ell$ is the median correlation of accepted frames for that profile. The current map is therefore

$$
I_b^{\mathrm{BP}}
=\frac{\sum_{\ell\in\mathcal V_b}w_\ell x_{\ell,b}}
{\sum_{\ell\in\mathcal V_b}w_\ell}.
$$

## 6. Noise Floors and Optional Geometric Compensation

### 6.1 Per-link noise floor

CGBP needs an activity threshold expressed in the same units as each link profile. A single global amplitude threshold is unsuitable because link gains, antenna patterns, fitted amplitudes, and residual clutter vary by link.

For each link profile, estimate a robust noise floor from a region expected to be free of the aligned direct-path response:

$$
n_\ell=\operatorname{median}(P_\ell[\mathcal N_\ell])
+\lambda\,1.4826\operatorname{MAD}(P_\ell[\mathcal N_\ell]),
$$

where $\mathcal N_\ell$ is a configured noise-reference region and $\lambda$ controls the guard margin. The region and multiplier must be validated against real `instantaneous-cir` captures; they should not be inferred from the per-profile peak.

The noise estimate should be computed before any path-loss gain so long-range noise is not amplified into apparent detections.

### 6.2 Optional bistatic range compensation

A point-scatterer path includes a transmitter-to-voxel leg and a voxel-to-receiver leg. A first-order amplitude compensation may therefore use

$$
g_{\ell,b}
=\left(\frac{r_{i,b}r_{b,j}}{r_0^2}\right)^\gamma,
$$

where

$$
r_{i,b}=\|\mathbf q_b-\mathbf p_i\|,
\qquad
r_{b,j}=\|\mathbf q_b-\mathbf p_j\|,
$$

$r_0$ is a reference distance, and $\gamma$ is an empirically selected amplitude exponent. The compensated evidence is

$$
y_{\ell,b}=g_{\ell,b}x_{\ell,b}.
$$

This differs from monostatic $d^2$ compensation. It depends on both bistatic legs and can vary across links at the same voxel.

For Heimdall, compensation should initially be **disabled by default** because the live display CIR has already undergone per-link fitting and is not yet an absolutely calibrated field-amplitude measurement. If added experimentally, $g_{\ell,b}$ must be capped, applied only after noise clipping, and exposed identically in all three reconstruction modes.

In the equations below, $y_{\ell,b}=x_{\ell,b}$ when compensation is disabled.

## 7. Median-Gated Backprojection (MGBP)

### 7.1 Principle

At a voxel containing a real scatterer, several link ellipsoids should sample elevated CIR energy at their predicted delays. A link-specific transient, unmodeled multipath component, residual fitting error, or corrupted profile can instead create an unusually large or small contribution from only a subset of links.

MGBP uses a median/MAD gate to suppress such cross-link deviations before computing the quality-weighted mean.

### 7.2 Algorithm

For each voxel $b$:

1. Form the valid evidence set

   $$
   \mathcal Y_b=\{y_{\ell,b}:\ell\in\mathcal V_b\}.
   $$

2. Compute its median:

   $$
   m_b=\operatorname{median}_{\ell\in\mathcal V_b}(y_{\ell,b}).
   $$

3. Compute the median absolute deviation:

   $$
   \operatorname{MAD}_b
   =\operatorname{median}_{\ell\in\mathcal V_b}|y_{\ell,b}-m_b|.
   $$

4. Stabilize the scale for nearly identical or quantized values:

   $$
   s_b=\max(1.4826\operatorname{MAD}_b,\epsilon_b),
   $$

   where $\epsilon_b$ is a small scale derived from the contributing links' noise floors.

5. Retain link $\ell$ when

   $$
   |y_{\ell,b}-m_b|\le k s_b.
   $$

   Define the retained set as $\mathcal R_b$.

6. Require a minimum number of retained links. If that requirement fails, set the voxel to zero or invalid rather than silently falling back to one link.

7. Compute the MGBP intensity:

   $$
   I_b^{\mathrm{MGBP}}
   =\frac{\sum_{\ell\in\mathcal R_b}w_\ell y_{\ell,b}}
   {\sum_{\ell\in\mathcal R_b}w_\ell}.
   $$

8. Report retained support separately:

   $$
   C_b^{\mathrm{MGBP}}=\sum_{\ell\in\mathcal R_b}w_\ell.
   $$

### 7.3 Expected behavior

MGBP can suppress:

- isolated strong multipath contributions;
- one-link alignment or fitting failures that survive profile admission;
- corrupted or temporarily unstable directed-link profiles;
- link-specific sidelobe energy inconsistent with the majority of baselines.

MGBP does **not** guarantee removal of artefacts shared by many links. A coherent false intersection can shift the per-voxel median and survive the gate.

### 7.4 Important limitation

Valid multistatic amplitudes are not expected to be equal across links. Antenna patterns, occlusion, reflection directionality, polarization, material response, bistatic angle, fitted gain, and path length can make a real reflector strong on a few links and weak on others. MGBP may reject those legitimate strong readings.

This risk is more significant in fixed multistatic geometry than in an idealized monostatic circular aperture. MGBP should therefore expose both $k$ and minimum retained-link count and should be evaluated using recall, not merely visual sparsity.

## 8. Consensus-Gated Backprojection (CGBP)

### 8.1 Principle

CGBP does not reject evidence because it is unusually large. It asks whether enough usable links contain signal above their own noise floors at the delay predicted for the voxel.

This turns voxel admission into a multistatic support test. After admission, all active link values remain eligible for the weighted mean.

### 8.2 Link activity

Link $\ell$ is active at voxel $b$ when

$$
a_{\ell,b}
=\mathbf 1[x_{\ell,b}>n_\ell].
$$

Activity is tested on uncompensated evidence $x_{\ell,b}$ against the per-link threshold $n_\ell$. If optional geometric compensation is enabled, it affects the averaged intensity but not the activity decision.

### 8.3 Consensus score

An unweighted consensus fraction is

$$
q_b
=\frac{\sum_{\ell\in\mathcal V_b}a_{\ell,b}}
{|\mathcal V_b|}.
$$

A quality-weighted alternative is

$$
q_b^{(w)}
=\frac{\sum_{\ell\in\mathcal V_b}w_\ell a_{\ell,b}}
{\sum_{\ell\in\mathcal V_b}w_\ell}.
$$

The first implementation should retain both diagnostics but use the unweighted fraction for the acceptance rule. Otherwise a small number of high-correlation links could dominate what is intended to be a cross-baseline consensus test.

### 8.4 Algorithm

For each voxel $b$:

1. Build $\mathcal V_b$ using the existing geometry, support, quality, and direct-path rules.
2. Require at least $L_{\min}$ valid directed links.
3. Evaluate $a_{\ell,b}$ for every valid link.
4. Accept the voxel if both

   $$
   q_b\ge p
   $$

   and

   $$
   \sum_{\ell\in\mathcal V_b}a_{\ell,b}\ge A_{\min}.
   $$

5. Let $\mathcal A_b$ be the active-link set. For an accepted voxel, compute

   $$
   I_b^{\mathrm{CGBP}}
   =\frac{\sum_{\ell\in\mathcal A_b}w_\ell y_{\ell,b}}
   {\sum_{\ell\in\mathcal A_b}w_\ell}.
   $$

6. Hard-zero a rejected voxel.
7. Store consensus and confidence independently:

   $$
   C_b^{\mathrm{CGBP}}=\sum_{\ell\in\mathcal A_b}w_\ell,
   \qquad Q_b=q_b.
   $$

### 8.5 Expected behavior

CGBP can:

- suppress voxels supported by only one or a few link ellipsoids;
- preserve legitimately large link responses rather than classifying them as outliers;
- produce a sparse volume with an interpretable support fraction;
- tolerate a configured number of weak or destructively interfered links.

### 8.6 Why a fixed 95% threshold is inappropriate here

A 95% rule with 20 directed links effectively requires 19 active links. Real indoor multistatic scattering may be strongly directional or occluded, so that default would likely reject real objects.

Furthermore, the two directions of one physical baseline—$(i,j)$ and $(j,i)$—are reciprocal measurements and may be strongly correlated. Twenty directed links therefore do not necessarily provide 20 independent votes.

The consensus rule should be tuned empirically. Initial evaluation should sweep $p$ rather than assigning it a publication-level default. A plausible exploratory range is 0.35 to 0.75, combined with an absolute minimum active-link count.

## 9. Optional Baseline-Aware Consensus

Heimdall's 20 directed links correspond to 10 unordered node pairs. To avoid double-counting reciprocal directions, CGBP may later support a baseline-aware vote.

For unordered baseline $r=\{i,j\}$, combine its two directed activity flags using one of:

$$
a_{r,b}^{\mathrm{OR}}=a_{(i,j),b}\lor a_{(j,i),b},
$$

$$
a_{r,b}^{\mathrm{AND}}=a_{(i,j),b}\land a_{(j,i),b},
$$

or a reciprocal mean followed by a threshold.

- **OR** tolerates a weak or missing direction but is permissive.
- **AND** demands reciprocal support but may reject real direction-dependent scattering.
- **Mean/threshold** provides a middle ground.

Baseline-aware voting should be a later option, not silently substituted for directed-link voting. The dashboard must label which denominator produced the displayed consensus.

## 10. Relationship Between the Three Modes

| Mode | Cross-link fusion | Main strength | Main risk |
|---|---|---|---|
| Standard BP | Quality-weighted mean of all valid links | Smooth and maximally sensitive | A few strong links can paint ellipsoidal artefacts |
| MGBP | Median/MAD gate, then quality-weighted mean | Rejects anomalous link contributions | Can discard valid directional reflections |
| CGBP | Noise-floor activity vote, then quality-weighted mean of active links | Requires distributed geometric support without rejecting large values | Can miss objects visible on only a few links |

MGBP and CGBP are complementary rather than ordered replacements. MGBP tests amplitude consistency; CGBP tests support prevalence.

## 11. Proposed Radar Map Integration

### 11.1 Mode selector

Add a reconstruction-mode control to the Radar Map tab:

- `Standard BP`
- `MGBP`
- `CGBP`

All modes use the same captured frame window and link profiles. Switching modes should recompute only the voxel-fusion step when possible.

### 11.2 Shared controls

Retain the current controls:

- frame count;
- minimum match score;
- minimum correlation fallback;
- minimum accepted frames per link;
- direct-path guard taps;
- voxel spacing;
- maximum voxel count;
- display percentile and point limit.

### 11.3 MGBP controls

- `MAD multiplier k`
- `Minimum valid links`
- `Minimum retained links`
- optional `Range compensation` toggle and capped gain, if experimentally enabled

Suggested exploratory starting point, not a validated default:

- $k=3$;
- at least six valid directed links;
- at least four retained directed links.

### 11.4 CGBP controls

- `Noise margin λ` or equivalent dB-above-noise margin
- `Consensus fraction p`
- `Minimum valid links L_min`
- `Minimum active links A_min`
- `Vote basis: directed links | unordered baselines` once baseline voting exists
- optional `Range compensation` toggle and capped gain

Suggested exploratory starting point, not a validated default:

- sweep $p$ from 0.35 to 0.75;
- require at least six valid directed links;
- require at least four active directed links.

### 11.5 Outputs and diagnostics

For every mode, preserve:

- intensity volume in `(z, y, x)` order;
- confidence volume;
- grid metadata;
- geometry revision and calibration status;
- per-link accepted-frame counts and median correlations.

Add:

- selected reconstruction mode;
- mode parameters;
- MGBP retained-link count or fraction per voxel;
- CGBP active-link count and consensus fraction per voxel;
- valid-link count per voxel;
- noise-floor estimate per link;
- whether range compensation was enabled and its parameters;
- whether consensus used directed links or unordered baselines.

The UI should allow coloring or filtering by support/consensus separately from intensity. A bright voxel supported by two links is qualitatively different from a moderately bright voxel supported by 14 links.

## 12. Implementation Shape

The existing implementation builds one `LinkProfile` per directed link and then loops over profiles while accumulating directly into `volume` and `confidence`. MGBP and CGBP require access to all link contributions for a voxel before finalizing its value.

A conceptually simple implementation is:

1. Build link profiles exactly as today.
2. For one spatial slab or block of voxels, compute a temporary evidence matrix with shape `[usable_links, block_voxels]`.
3. Compute the selected fusion rule down the link dimension.
4. Write intensity, confidence, and support diagnostics to the output volumes.
5. Reuse the temporary buffer for the next block.

Blocking avoids allocating a full `[20, voxel_count]` array when the grid approaches the configured 200,000-voxel cap. With 20 links, per-voxel median/MAD calculations are small, but repeated sorting can still dominate browser computation. A fixed-size selection routine or typed-array scratch buffer may be preferable after correctness is established.

The standard mode should continue to use its existing streaming accumulation path so the experimental modes do not regress current performance.

## 13. Failure Modes and Interpretation

### 13.1 Calibration and geometry

The excess-delay model assumes accurate node phase-centre coordinates and antenna delays. Heimdall currently warns that per-board antenna-delay and phase-centre calibration remain outstanding. MGBP/CGBP can suppress inconsistent evidence, but they cannot turn biased delays into accurate metric geometry.

### 13.2 Extended surfaces

Walls and other specular planes are not point scatterers. A plane reflection can follow image-source geometry rather than the single-bounce point-ellipsoid model. Link evidence from a real wall may therefore fail to intersect as a compact voxel across many baselines. CGBP may suppress such surfaces more aggressively than localized diffuse scatterers.

### 13.3 Directional visibility

A real object may be visible on only a few baselines because of occlusion, antenna orientation, polarization, or bistatic scattering angle. CGBP's consensus threshold trades false-positive suppression directly against this limited-view recall.

### 13.4 Correlated votes

Reciprocal directions and links sharing a transmitter or receiver are correlated. Consensus fraction is a support diagnostic, not a calibrated probability of occupancy.

### 13.5 Static versus motion products

The existing live Radar Map consumes temporal-median aligned profiles. That favors persistent structure within the selected frame window. Detecting motion requires an explicit clutter-reference or temporal-difference product upstream of backprojection. MGBP/CGBP alone do not create a motion detector.

### 13.6 Weak-reflector claims

Neither method should be described as universally recovering weak reflectors. MGBP may preserve weak but broadly consistent evidence; CGBP may reject a weak object if too few links exceed their noise floors. Performance claims require measured or controlled synthetic evaluation.

## 14. Validation Plan

Evaluate Standard BP, MGBP, and CGBP from identical frozen `instantaneous-cir` windows and identical geometry.

### 14.1 Deterministic unit tests

1. **Bistatic delay:** A known voxel maps to the expected excess tap without a monostatic divide by two.
2. **Fractional interpolation:** Evidence at fractional excess taps matches the current linear interpolator.
3. **MGBP isolated outlier:** One extreme link is rejected while consistent links remain.
4. **MGBP zero MAD:** The stabilized scale produces finite, deterministic output.
5. **MGBP insufficient support:** A voxel with too few retained links is rejected.
6. **CGBP threshold boundary:** Acceptance at exactly the configured fraction is defined and tested.
7. **CGBP missing links:** The denominator uses valid links, while the absolute minimum prevents a deceptively high fraction from a tiny set.
8. **CGBP active mean:** Inactive links do not dilute accepted-voxel intensity.
9. **Reciprocal vote mode:** Directed-link and unordered-baseline consensus produce the documented denominators.
10. **Mode parity:** Standard BP remains bitwise or tolerance-equivalent to the existing implementation.

### 14.2 Controlled synthetic scenes

Test:

- one isolated point scatterer;
- two equal scatterers;
- one strong and one weak scatterer;
- a scatterer visible on only a subset of links;
- one corrupted link profile;
- reciprocal-link correlation;
- missing links;
- geometry perturbations and antenna-delay biases;
- specular planes versus localized diffuse scatterers.

Measure localization error, peak-to-sidelobe ratio, voxel precision/recall, weak-target recall, and sensitivity to each mode parameter.

### 14.3 Real Heimdall captures

Use repeatable surveyed targets and protected capture windows. For each run, record:

- geometry revision and calibration status;
- node roster and directed links present;
- Live CIR fit mode;
- frame-window duration;
- all profile-admission parameters;
- reconstruction mode and parameters;
- queue/drop health;
- target ground truth and environmental changes.

Compare modes on exactly the same data. Do not select a different display percentile independently for each mode when making visual comparisons; use shared physical or percentile scales and report both intensity and support.

## 15. Recommended Initial Scope

The smallest defensible first implementation is:

1. Reuse current `buildLinkProfiles` output without changing temporal preprocessing.
2. Add `Standard BP | MGBP | CGBP` as Radar Map modes.
3. Use directed links as votes.
4. Leave bistatic range compensation disabled.
5. Estimate and expose a robust per-link noise floor for CGBP.
6. Require both fractional and absolute support thresholds.
7. Produce support diagnostics alongside intensity and confidence.
8. Validate on synthetic inputs and frozen real snapshots before assigning production defaults.

This scope tests the central idea—robust cross-link fusion—without conflating it with changes to the existing alignment pipeline, CIR fitting, temporal profile estimator, path-loss model, or viewer.

## 16. Summary

Heimdall's current Radar Map already performs the essential multistatic geometry step: it maps every voxel to a direct-path-referenced bistatic excess delay for each directed link. MGBP and CGBP can therefore be integrated as alternative cross-link fusion rules over the same per-link voxel evidence.

- **MGBP** rejects link contributions that are anomalous relative to the per-voxel median and MAD, then forms a quality-weighted mean.
- **CGBP** requires a configurable fraction and count of links to exceed their individual noise floors, then forms a quality-weighted mean of the active links.

The fixed five-node geometry makes support diversity valuable, but it also makes unequal link amplitudes, directional visibility, and correlated reciprocal measurements unavoidable. The algorithms should consequently expose their support statistics and be presented as tunable experimental modes—not as automatic guarantees of sidelobe removal or weak-target recovery.

---

*Prepared from the Heimdall project's current Radar Map implementation and documentation. This is a design proposal; it does not state that MGBP or CGBP are already implemented or validated.*
