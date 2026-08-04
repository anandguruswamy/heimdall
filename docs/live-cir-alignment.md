# Live CIR Alignment

Live CIR can display each incoming complex channel impulse response (CIR) either
as received or after removing small frame-to-frame timing, gain, and phase
differences. Alignment is a display operation. It does not replace the archived
raw CIR or change the radio measurement.

## Display modes

Live CIR has three relevant alignment choices:

- **Off:** use the normal reference-correlation or first-path alignment, but do
  not apply either additional gain/phase/delay fitting method.
- **Linear complex tangent-space LS:** use the existing fast, local
  least-squares estimate described below.
- **Robust grid:** use the full hierarchical search described below. This is
  the preferred method when a stable visual alignment is more important than
  the lowest compute cost.

Both fitting methods operate on complex samples, not magnitude-only CIRs. The
linear method displays its fitted static model. The robust grid method applies
one physical correction to the current frame, so taps changed by motion remain
changed.

## Linear complex tangent-space LS

The existing method treats a small delay as a first-order displacement in the
complex CIR's tangent space. With reference `x`, current CIR `y`, and a complex
sample derivative `Dx`, it fits the local model

```text
y ~= a x + b Dx
```

by complex least squares. The coefficient `a` represents complex gain and
phase; the ratio between `b` and `a` represents the small delay under the
derivative convention used by the implementation. Live CIR displays the fitted
model `a x + b Dx`, which suppresses changes that the model cannot explain.

This method is inexpensive and gives sub-tap estimates without a grid search.
It is nevertheless a local linear approximation. Strong changed taps,
outliers, low-reference-energy taps, or a delay outside the useful linear
neighborhood can bias the least-squares solution. The robust grid method avoids
the tangent approximation and limits scoring to reference taps with adequate
signal-to-noise ratio.

## Robust grid alignment

### Complex reference

The backend constructs one complex reference CIR `x` independently for each
aligned stream. The available reference modes are:

- **Frozen Board Positions:** the default. While Board Positions is frozen,
  use its frozen complex reference. A frozen complex reference exists only for
  the duration of that frozen Board Positions state.
- **Rolling medoid:** choose a representative complex CIR from the rolling
  window. The medoid is an observed frame, selected to minimize its aggregate
  complex distance to the other frames in the window, so an outlier does not
  pull the reference as readily as it pulls a mean.
- **Rolling complex mean:** take the tap-by-tap arithmetic mean of the complex
  CIRs in the rolling window.

If Frozen Board Positions is selected but Board Positions is not frozen, or no
usable frozen complex reference is available, the backend falls back to the
rolling medoid. Unfreezing Board Positions ends the lifetime of that frozen
reference; a later freeze establishes a new one.

The rolling reference window is configurable and defaults to 32 frames. The
service may constrain the accepted range to protect memory and frame latency.
Reference construction retains real and imaginary components. Averaging CIR
magnitudes is not equivalent and is not used.
Rolling mean and medoid references are refreshed every eight link frames; the
complete robust grid still runs on every incoming CIR.

### Noise gate

Let `x[n]` be the reference and `y[n]` the current CIR. Estimate the reference
noise level from the first 14 taps of the original, unshifted, non-interpolated
reference:

```text
mu    = (1 / 14) sum(n=0..13) x[n]
sigma = sqrt((1 / 14) sum(n=0..13) |x[n] - mu|^2)
```

After computing `sigma`, resample both complex CIRs by `P`. Every resampled
reference point satisfying

```text
|x[n]| >= 3 sigma
```

participates in candidate scoring. The threshold is fixed from the first 14
original taps for the entire search; it is not re-estimated from interpolated
points.

### Candidate correction and residual

For a candidate delay `tau`, linear amplitude gain `g > 0`, and phase `theta`,
correct the current CIR as

```text
yhat[n] = y[n + tau] / g * exp(-j theta)
```

Fractional values of `y[n + tau]` come from `P`-times Kaiser-windowed sinc
interpolation. Interpolation is non-circular: it never wraps samples from one
end of the CIR to the other.

For every noise-gated reference tap, form the normalized complex ratio and its
error:

```text
z[n] = yhat[n] conj(x[n]) / |x[n]|^2
e[n] = |z[n] - 1|
```

Thus a perfectly corrected tap has `z = 1` and `e = 0`. Normalization makes the
score measure relative complex disagreement rather than allowing the largest
reference taps alone to dominate.

### Robust scores

Let `V` be the set of valid taps that pass the `3 sigma` gate and have an
in-range interpolated current sample. Let `eta > 0` be the robust error
threshold. Two scores are selectable:

```text
count_score = sum(n in V) I(e[n] < eta)                  (maximize)

soft_score  = sum(n in V) max(0, 1 - e[n] / eta)        (maximize)
```

`I(condition)` is 1 when the condition is true and 0 otherwise. The count score
gives every inlier equal weight and completely rejects errors beyond `eta`.
The soft score preserves ordering among inliers while giving every outlier zero
weight. Candidates with no valid scoring taps are invalid.

The default score is **soft**, with `eta = 0.25`.

### Reference peak level

After fitting, Robust Grid applies one additional reference-derived gain. The
reference peak is the maximum complex magnitude on the configured `P`-resampled
grid. With target level `L` dB,

```text
target_raw = 11.2 * 10^((L + 10) / 20)
level_gain = target_raw / reference_peak
```

The displayed robust CIR is `level_gain * yhat`. The reported `fit_gain_db` is
the total amplitude gain actually applied to the input:

```text
fit_gain_db = 20 log10(level_gain) - grid_gain_db
```

The setpoint defaults to `-10 dB` and is configurable from `-30` through `0 dB`
in 1 dB steps. Movement-changed current taps do not alter this normalization.

### Hierarchical grid

Every current frame runs a new complete hierarchical search. A previous
frame's solution does not narrow the coarse grid.

The coarse grid searches:

- Delay over plus or minus the configured delay limit, at 0.5-tap spacing, or
  one tap when `P = 1` because a half tap is not representable.
- Gain over the configured dB interval, default `[-10, +10] dB`, at 2 dB
  spacing. Convert each gain candidate to the correction formula's linear
  amplitude with `g = 10^(gain_dB / 20)`.
- Phase over the full circle, at 5 degree spacing. Phase is periodic; `-180`
  degrees and `+180` degrees represent the same candidate and must not be
  duplicated.

After selecting the winning coarse candidate, search its coarse cell again at:

- `1/P` tap delay spacing.
- 0.5 dB gain spacing.
- 1 degree phase spacing.

The refined search covers the winning coarse cell: plus or minus 0.25 tap, 1 dB,
and 2 degrees around its centre, bounded by the configured global limits. The
phase cell wraps across the full-circle boundary.

This hierarchy is intentionally not claimed to equal an exhaustive fine-grid
search. Count and clipped soft scores are robust but non-convex; the globally
best fine candidate can lie in a coarse cell that did not win at the coarse
sample points. The hierarchy trades that possibility for bounded per-frame
work.

## Configuration defaults

| Setting | Default | Meaning |
| --- | ---: | --- |
| Alignment method | Off | Existing Live CIR behavior until a fit is selected |
| Reference mode | Frozen Board Positions | Falls back to rolling medoid when no frozen reference exists |
| Reference window | 32 frames | Rolling medoid or rolling complex mean history; service may enforce a range |
| Delay limit | `+/-2` taps | Coarse delay search extent |
| Interpolation factor `P` | 16 | Fine delay spacing is `1/16` tap |
| Gain range | `[-10, +10] dB` | Coarse gain search extent |
| Robust threshold `eta` | 0.25 | Inlier threshold and soft-score clipping scale |
| Reference peak | `-10 dB` | P-resampled reference target; configurable `-30..0 dB` |
| Score | Soft | Maximize bounded inlier weights |

The coarse delay, gain, and phase spacings are fixed at 0.5 tap, 2 dB, and 5
degrees respectively. Refined spacings are `1/P` tap, 0.5 dB, and 1 degree.

## Edge and performance behavior

Kaiser-windowed sinc interpolation has finite support. Near either CIR edge,
part of that support or the requested `n + tau` position can fall outside the
captured samples. The interpolator must treat the CIR as non-circular and must
not wrap opposite-edge data into the result. Taps without a valid interpolated
sample are excluded from `V`; implementations may equivalently restrict the
scoring index range in advance. This means different delay candidates can have
different valid edge taps. Scores are the requested sums, so candidates with
more matching in-range points naturally receive more support; an empty valid
set is always invalid.

The robust grid is substantially more expensive than tangent-space LS. Its
work scales with the number of coarse and refined delay/gain/phase candidates,
the number of valid scoring taps, and the interpolation cost. `P = 16`, a
bounded delay range, two-stage search, a precomputed Kaiser-sinc interpolation
table, and reuse of the fixed noise gate keep the cost predictable. Reference
window size also affects medoid construction cost and memory, which is why the
service may limit its configurable range.

Delay hypotheses are scored in parallel using a persistent Rayon thread pool.
Candidate ordering and final tie resolution remain deterministic.
Within each delay, a resampled point votes only for gain/phase cells that can
possibly satisfy `|z - 1| < eta`. The admissible radial and angular intervals
are derived analytically, then every retained cell is evaluated with the exact
complex-distance equation. This sparse voting changes computation cost, not the
grid or objective.

Radio/USB ingestion remains decoupled through the service queue, but grid work
adds latency to the single processing path and can cause stale processing work
to be dropped under overload. Live deployment must therefore be checked using
the queue depth, queue wait, processing time, and drop counters in `/api/health`.

## Live CIR display scale

All Live CIR modes use normalized display amplitude. The existing calibration
maps a raw magnitude of 11.2 to `-10 dB`, so normalized linear amplitude is

```text
a_normalized = a_raw * 10^((-10 - 20 log10(11.2)) / 20)
```

and `a_normalized = 1` is `0 dB`. With `LOCK Y SCALE` enabled every link uses
fixed common limits: `0..1` in linear mode and `-60..0 dB` in dB mode. Unlocked
plots auto-range in the same normalized units. Waterfall and FFT units are not
changed by this display conversion.
