use crate::QualityFlags;
use num_complex::Complex64;
use rayon::prelude::*;
use std::{f64::consts::PI, sync::OnceLock};

pub const CIR_INTERPOLATION: usize = 16;

fn i0(x: f64) -> f64 {
    let mut sum = 1.;
    let mut term = 1.;
    for k in 1..30 {
        term *= x * x / (4. * (k * k) as f64);
        sum += term;
        if term < sum * 1e-15 {
            break;
        }
    }
    sum
}
fn sinc(x: f64) -> f64 {
    if x.abs() < 1e-12 {
        1.
    } else {
        (PI * x).sin() / (PI * x)
    }
}
fn kernel(x: f64, radius: f64, beta: f64) -> f64 {
    if x.abs() > radius {
        0.
    } else {
        sinc(x) * i0(beta * (1. - (x / radius).powi(2)).max(0.).sqrt()) / i0(beta)
    }
}

fn interpolation_weights() -> &'static [[f64; 17]; CIR_INTERPOLATION] {
    static WEIGHTS: OnceLock<[[f64; 17]; CIR_INTERPOLATION]> = OnceLock::new();
    WEIGHTS.get_or_init(|| {
        std::array::from_fn(|phase| {
            let fraction = phase as f64 / CIR_INTERPOLATION as f64;
            std::array::from_fn(|index| kernel(fraction - (index as f64 - 8.0), 8.0, 8.6))
        })
    })
}

fn runtime_interpolation_weights(factor: usize) -> Option<&'static [[f64; 17]]> {
    static P1: OnceLock<Vec<[f64; 17]>> = OnceLock::new();
    static P2: OnceLock<Vec<[f64; 17]>> = OnceLock::new();
    static P4: OnceLock<Vec<[f64; 17]>> = OnceLock::new();
    static P8: OnceLock<Vec<[f64; 17]>> = OnceLock::new();

    if factor == CIR_INTERPOLATION {
        return Some(interpolation_weights());
    }
    let cache = match factor {
        1 => &P1,
        2 => &P2,
        4 => &P4,
        8 => &P8,
        _ => return None,
    };
    Some(cache.get_or_init(|| {
        (0..factor)
            .map(|phase| {
                let fraction = phase as f64 / factor as f64;
                std::array::from_fn(|index| kernel(fraction - (index as f64 - 8.0), 8.0, 8.6))
            })
            .collect()
    }))
}

fn interpolate_runtime(
    input: &[Complex64],
    position: i64,
    factor: usize,
    normalize: bool,
) -> Option<Complex64> {
    let weights = runtime_interpolation_weights(factor)?;
    let base = position.div_euclid(factor as i64);
    let phase = position.rem_euclid(factor as i64) as usize;
    let mut value = Complex64::new(0.0, 0.0);
    let mut norm = 0.0;
    for (index, weight) in weights[phase].iter().enumerate() {
        let source = base + index as i64 - 8;
        if source >= 0 && (source as usize) < input.len() {
            value += input[source as usize] * *weight;
            norm += weight;
        }
    }
    if normalize && norm.abs() > 1e-12 {
        value /= norm;
    }
    Some(value)
}

fn interpolate_16x(input: &[Complex64], position: i64, normalize: bool) -> Complex64 {
    let base = position.div_euclid(CIR_INTERPOLATION as i64);
    let phase = position.rem_euclid(CIR_INTERPOLATION as i64) as usize;
    let mut value = Complex64::new(0.0, 0.0);
    let mut norm = 0.0;
    for (index, weight) in interpolation_weights()[phase].iter().enumerate() {
        let source = base + index as i64 - 8;
        if source >= 0 && (source as usize) < input.len() {
            value += input[source as usize] * *weight;
            norm += weight;
        }
    }
    if normalize && norm.abs() > 1e-12 {
        value /= norm;
    }
    value
}

/// Kaiser-windowed sinc interpolation. Samples beyond either edge are zero, never wrapped.
pub fn resample_cir_16x(input: &[Complex64]) -> Vec<Complex64> {
    if input.is_empty() {
        return vec![];
    }
    let n = (input.len() - 1) * CIR_INTERPOLATION + 1;
    let mut out = Vec::with_capacity(n);
    for k in 0..n {
        out.push(interpolate_16x(input, k as i64, true));
    }
    out
}

/// Maximum complex magnitude on the configured non-circular interpolation grid.
pub fn resampled_cir_peak(input: &[Complex64], factor: usize) -> Option<f64> {
    if input.is_empty() {
        return None;
    }
    runtime_interpolation_weights(factor)?;
    (0..=(input.len() - 1) * factor)
        .filter_map(|position| interpolate_runtime(input, position as i64, factor, true))
        .map(|value| value.norm())
        .filter(|value| value.is_finite())
        .max_by(f64::total_cmp)
}

pub fn fractional_align_non_circular(input: &[Complex64], shift_samples: f64) -> Vec<Complex64> {
    let shift = (shift_samples * CIR_INTERPOLATION as f64).round() as i64;
    (0..input.len())
        .map(|k| interpolate_16x(input, k as i64 * CIR_INTERPOLATION as i64 + shift, false))
        .collect()
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CirAlignmentScoreMode {
    Count,
    Soft,
}

#[derive(Clone, Copy, Debug)]
pub struct CirAlignmentConfig {
    pub gain_min_db: f64,
    pub gain_max_db: f64,
    pub delay_range_samples: f64,
    pub resampling_factor: usize,
    pub eta: f64,
    pub score_mode: CirAlignmentScoreMode,
}

impl Default for CirAlignmentConfig {
    fn default() -> Self {
        Self {
            gain_min_db: -10.0,
            gain_max_db: 10.0,
            delay_range_samples: 2.0,
            resampling_factor: 16,
            eta: 0.25,
            score_mode: CirAlignmentScoreMode::Soft,
        }
    }
}

impl CirAlignmentConfig {
    fn is_valid(self) -> bool {
        self.gain_min_db.is_finite()
            && self.gain_max_db.is_finite()
            && self.gain_min_db <= self.gain_max_db
            && self.delay_range_samples.is_finite()
            && self.delay_range_samples >= 0.0
            && matches!(self.resampling_factor, 1 | 2 | 4 | 8 | 16)
            && self.eta.is_finite()
            && self.eta > 0.0
    }
}

#[derive(Clone, Debug)]
pub struct CirAlignmentResult {
    pub delay_samples: f64,
    pub gain_db: f64,
    pub phase_radians: f64,
    pub phase_degrees: f64,
    pub score: f64,
    pub matched_count: usize,
    pub eligible_count: usize,
    pub corrected: Vec<Complex64>,
}

#[derive(Clone, Copy)]
struct AlignmentCandidate {
    delay_units: i64,
    gain_db: f64,
    phase_degrees: f64,
    score: f64,
    tie_score: f64,
    matched_count: usize,
    eligible_count: usize,
}

#[derive(Clone, Copy)]
struct CorrectionCandidate {
    gain_db: f64,
    phase_degrees: f64,
    re: f64,
    im: f64,
    norm_sqr: f64,
}

fn finite_complex(value: Complex64) -> bool {
    value.re.is_finite() && value.im.is_finite()
}

// Maximum error is below one 5-degree coarse cell. Candidate pruning adds a
// full guard cell; retained candidates are still scored with exact arithmetic.
fn approximate_phase_degrees(imaginary: f64, real: f64) -> f64 {
    let abs_imaginary = imaginary.abs() + f64::MIN_POSITIVE;
    let (ratio, angle) = if real >= 0.0 {
        (
            (real - abs_imaginary) / (real + abs_imaginary),
            std::f64::consts::FRAC_PI_4,
        )
    } else {
        (
            (real + abs_imaginary) / (abs_imaginary - real),
            3.0 * std::f64::consts::FRAC_PI_4,
        )
    };
    let angle = angle - std::f64::consts::FRAC_PI_4 * ratio;
    if imaginary < 0.0 { -angle } else { angle }.to_degrees()
}

fn inclusive_grid(minimum: f64, maximum: f64, step: f64) -> Vec<f64> {
    let count = ((maximum - minimum) / step).floor() as usize;
    let mut values = (0..=count)
        .map(|index| minimum + index as f64 * step)
        .collect::<Vec<_>>();
    if values.last().is_none_or(|last| maximum - last > 1e-10) {
        values.push(maximum);
    }
    values
}

fn delay_grid(minimum: f64, maximum: f64, factor: usize, step: f64) -> Vec<i64> {
    let minimum_units = (minimum * factor as f64).ceil() as i64;
    let maximum_units = (maximum * factor as f64).floor() as i64;
    let step_units = (step * factor as f64).round().max(1.0) as i64;
    let mut values = Vec::new();
    let mut value = minimum_units;
    while value <= maximum_units {
        values.push(value);
        value += step_units;
    }
    if values.last().is_none_or(|last| *last != maximum_units) {
        values.push(maximum_units);
    }
    values
}

fn search_alignment_grid(
    current: &[Complex64],
    eligible_samples: &[(i64, Complex64)],
    config: CirAlignmentConfig,
    interpolation_factor: usize,
    delays: &[i64],
    gains_db: &[f64],
    phases_degrees: &[f64],
) -> Option<AlignmentCandidate> {
    let factor = interpolation_factor as i64;
    let gains = gains_db
        .iter()
        .map(|&gain_db| (gain_db, 10f64.powf(-gain_db / 20.0)))
        .collect::<Vec<_>>();
    let phases = phases_degrees
        .iter()
        .map(|&phase_degrees| {
            let value = Complex64::from_polar(1.0, -phase_degrees.to_radians());
            (phase_degrees, value)
        })
        .collect::<Vec<_>>();
    let corrections = gains
        .iter()
        .flat_map(|&(gain_db, inverse_gain)| {
            phases.iter().map(move |&(phase_degrees, phase)| {
                let value = phase * inverse_gain;
                CorrectionCandidate {
                    gain_db,
                    phase_degrees,
                    re: value.re,
                    im: value.im,
                    norm_sqr: value.norm_sqr(),
                }
            })
        })
        .collect::<Vec<_>>();
    let eta_sqr = config.eta * config.eta;
    let full_phase_step = if phases_degrees.len() > 16 {
        Some((phases_degrees[1] - phases_degrees[0]).abs())
    } else {
        None
    };
    let maximum_phase_error = if config.eta < 1.0 {
        config.eta.asin().to_degrees()
    } else {
        180.0
    };
    let per_delay = delays
        .par_iter()
        .map(|&delay_units| {
            let mut samples = Vec::with_capacity(eligible_samples.len());
            for &(reference_position, x) in eligible_samples {
                let position = reference_position + delay_units;
                if position < 0 || position > (current.len() - 1) as i64 * factor {
                    continue;
                }
                let value = interpolate_runtime(current, position, interpolation_factor, true)?;
                let ratio = value * x.conj() / x.norm_sqr();
                samples.push((ratio.re, ratio.im, ratio.norm_sqr()));
            }
            if samples.is_empty() {
                return None;
            }
            let mut soft_scores = vec![0.0; corrections.len()];
            let mut matched_counts = vec![0_usize; corrections.len()];
            for &(ratio_re, ratio_im, ratio_norm_sqr) in &samples {
                let ratio_norm = ratio_norm_sqr.sqrt();
                if ratio_norm <= 0.0 {
                    continue;
                }
                let ratio_phase =
                    full_phase_step.map(|_| approximate_phase_degrees(ratio_im, ratio_re));
                for (gain_index, &(_, inverse_gain)) in gains.iter().enumerate() {
                    let corrected_norm = inverse_gain * ratio_norm;
                    if (corrected_norm - 1.0).abs() >= config.eta {
                        continue;
                    }
                    let mut score_phase = |phase_index: usize| {
                        let candidate_index = gain_index * phases.len() + phase_index;
                        let correction = &corrections[candidate_index];
                        let error_sqr = correction.norm_sqr * ratio_norm_sqr
                            - 2.0 * (correction.re * ratio_re - correction.im * ratio_im)
                            + 1.0;
                        if error_sqr < eta_sqr {
                            matched_counts[candidate_index] += 1;
                            soft_scores[candidate_index] +=
                                1.0 - error_sqr.max(0.0).sqrt() / config.eta;
                        }
                    };
                    if let Some(step) = full_phase_step {
                        let count = phases.len() as isize;
                        let wrapped = (ratio_phase.unwrap() + 180.0).rem_euclid(360.0) - 180.0;
                        let center = ((wrapped - phases_degrees[0]) / step).round() as isize;
                        let radius = (maximum_phase_error / step).ceil() as isize + 1;
                        if radius * 2 + 1 >= count {
                            for phase_index in 0..phases.len() {
                                score_phase(phase_index);
                            }
                        } else {
                            for offset in -radius..=radius {
                                score_phase((center + offset).rem_euclid(count) as usize);
                            }
                        }
                    } else {
                        for phase_index in 0..phases.len() {
                            score_phase(phase_index);
                        }
                    }
                }
            }
            let mut best = None;
            for (index, correction) in corrections.iter().enumerate() {
                let score = match config.score_mode {
                    CirAlignmentScoreMode::Count => matched_counts[index] as f64,
                    CirAlignmentScoreMode::Soft => soft_scores[index],
                };
                let tie_score = soft_scores[index];
                if best.is_none_or(|candidate: AlignmentCandidate| {
                    score > candidate.score
                        || (score == candidate.score && tie_score > candidate.tie_score)
                }) {
                    best = Some(AlignmentCandidate {
                        delay_units,
                        gain_db: correction.gain_db,
                        phase_degrees: correction.phase_degrees,
                        score,
                        tie_score,
                        matched_count: matched_counts[index],
                        eligible_count: samples.len(),
                    });
                }
            }
            best
        })
        .collect::<Vec<_>>();
    per_delay
        .into_iter()
        .flatten()
        .fold(None, |best, candidate| {
            if best.is_none_or(|current: AlignmentCandidate| {
                candidate.score > current.score
                    || (candidate.score == current.score && candidate.tie_score > current.tie_score)
            }) {
                Some(candidate)
            } else {
                best
            }
        })
}

/// Applies `yhat[n] = y[n + tau] / g * exp(-j theta)` without circular wrapping.
pub fn correct_cir_non_circular(
    current: &[Complex64],
    delay_samples: f64,
    gain_db: f64,
    phase_radians: f64,
    resampling_factor: usize,
) -> Option<Vec<Complex64>> {
    if current.is_empty()
        || !delay_samples.is_finite()
        || !gain_db.is_finite()
        || !phase_radians.is_finite()
        || !current.iter().copied().all(finite_complex)
    {
        return None;
    }
    runtime_interpolation_weights(resampling_factor)?;
    let delay_units = (delay_samples * resampling_factor as f64).round() as i64;
    let correction = Complex64::from_polar(10f64.powf(-gain_db / 20.0), -phase_radians);
    (0..current.len())
        .map(|index| {
            interpolate_runtime(
                current,
                index as i64 * resampling_factor as i64 + delay_units,
                resampling_factor,
                false,
            )
            .map(|value| value * correction)
        })
        .collect()
}

/// Robust full-hierarchy delay, gain, and common-phase alignment of two CIRs.
pub fn align_cir_hierarchical(
    reference: &[Complex64],
    current: &[Complex64],
    config: CirAlignmentConfig,
) -> Option<CirAlignmentResult> {
    if reference.is_empty()
        || current.is_empty()
        || !config.is_valid()
        || !reference.iter().copied().all(finite_complex)
        || !current.iter().copied().all(finite_complex)
    {
        return None;
    }

    let noise_taps = &reference[..reference.len().min(14)];
    let noise_mean = noise_taps.iter().sum::<Complex64>() / noise_taps.len() as f64;
    let noise_sigma = (noise_taps
        .iter()
        .map(|value| (*value - noise_mean).norm_sqr())
        .sum::<f64>()
        / noise_taps.len() as f64)
        .sqrt();
    let threshold = 3.0 * noise_sigma;
    // Delay hypotheses are representable on the configured resampling grid.
    // At P=1 the nominal half-tap coarse spacing therefore becomes one tap.
    let search_factor = config.resampling_factor;
    let eligible_samples = (0..=(reference.len() - 1) * search_factor)
        .filter_map(|position| {
            let value = interpolate_runtime(reference, position as i64, search_factor, true)?;
            (value.norm() >= threshold && value.norm_sqr() > 0.0)
                .then_some((position as i64, value))
        })
        .collect::<Vec<_>>();
    if eligible_samples.is_empty() {
        return None;
    }

    let coarse_delays = delay_grid(
        -config.delay_range_samples,
        config.delay_range_samples,
        search_factor,
        0.5,
    );
    let coarse_gains = inclusive_grid(config.gain_min_db, config.gain_max_db, 2.0);
    let coarse_phases = inclusive_grid(-180.0, 175.0, 5.0);
    let coarse = search_alignment_grid(
        current,
        &eligible_samples,
        config,
        search_factor,
        &coarse_delays,
        &coarse_gains,
        &coarse_phases,
    )?;

    let coarse_delay = coarse.delay_units as f64 / search_factor as f64;
    let fine_delays = delay_grid(
        (coarse_delay - 0.25).max(-config.delay_range_samples),
        (coarse_delay + 0.25).min(config.delay_range_samples),
        search_factor,
        1.0 / config.resampling_factor as f64,
    );
    let fine_gains = inclusive_grid(
        (coarse.gain_db - 1.0).max(config.gain_min_db),
        (coarse.gain_db + 1.0).min(config.gain_max_db),
        0.5,
    );
    let fine_phases = (-2..=2)
        .map(|offset| {
            let phase = coarse.phase_degrees + offset as f64;
            if phase >= 180.0 {
                phase - 360.0
            } else if phase < -180.0 {
                phase + 360.0
            } else {
                phase
            }
        })
        .collect::<Vec<_>>();
    let best = search_alignment_grid(
        current,
        &eligible_samples,
        config,
        search_factor,
        &fine_delays,
        &fine_gains,
        &fine_phases,
    )?;
    let delay_samples = best.delay_units as f64 / search_factor as f64;
    let phase_radians = best.phase_degrees.to_radians();
    let corrected = correct_cir_non_circular(
        current,
        delay_samples,
        best.gain_db,
        phase_radians,
        config.resampling_factor,
    )?;
    Some(CirAlignmentResult {
        delay_samples,
        gain_db: best.gain_db,
        phase_radians,
        phase_degrees: best.phase_degrees,
        score: best.score,
        matched_count: best.matched_count,
        eligible_count: best.eligible_count,
        corrected,
    })
}

#[derive(Clone, Copy, Debug)]
pub struct DelayEstimate {
    pub delay_samples: f64,
    pub correlation: f64,
    pub quality: QualityFlags,
}
pub fn normalized_correlation_delay(
    reference: &[Complex64],
    signal: &[Complex64],
    max_lag: usize,
) -> Option<DelayEstimate> {
    if reference.is_empty() || signal.is_empty() {
        return None;
    }
    let max = max_lag as isize;
    let mut coarse = (-1.0, 0isize);
    for lag in -max..=max {
        let mut dot = Complex64::new(0., 0.);
        let (mut er, mut es) = (0., 0.);
        for (i, &rv) in reference.iter().enumerate() {
            let j = i as isize + lag;
            if j >= 0 && (j as usize) < signal.len() {
                let sv = signal[j as usize];
                dot += rv.conj() * sv;
                er += rv.norm_sqr();
                es += sv.norm_sqr();
            }
        }
        let c = if er * es > 0. {
            dot.norm() / (er * es).sqrt()
        } else {
            0.
        };
        if c > coarse.0 {
            coarse = (c, lag);
        }
    }
    // Refine only the winning raw-sample lag. This preserves 1/16-sample
    // resolution without correlating two full 16x-expanded arrays at every lag.
    let mut best = (coarse.0, coarse.1 as f64);
    for step in -8..=8 {
        let lag_16x = coarse.1 as i64 * CIR_INTERPOLATION as i64 + step;
        let lag = lag_16x as f64 / CIR_INTERPOLATION as f64;
        let mut dot = Complex64::new(0.0, 0.0);
        let (mut er, mut es) = (0.0, 0.0);
        for (i, &rv) in reference.iter().enumerate() {
            let x = i as f64 + lag;
            if x < 0.0 || x > signal.len().saturating_sub(1) as f64 {
                continue;
            }
            let sv = interpolate_16x(signal, i as i64 * CIR_INTERPOLATION as i64 + lag_16x, true);
            dot += rv.conj() * sv;
            er += rv.norm_sqr();
            es += sv.norm_sqr();
        }
        let score = if er * es > 0.0 {
            dot.norm() / (er * es).sqrt()
        } else {
            0.0
        };
        if score > best.0 {
            best = (score, lag);
        }
    }
    let mut quality = QualityFlags::NONE;
    if best.0 < 0.5 {
        quality.insert(QualityFlags::LOW_CORRELATION);
    }
    Some(DelayEstimate {
        delay_samples: (best.1 * CIR_INTERPOLATION as f64).round() / CIR_INTERPOLATION as f64,
        correlation: best.0,
        quality,
    })
}
pub fn common_phase(reference: &[Complex64], signal: &[Complex64]) -> Option<f64> {
    if reference.len() != signal.len() || reference.is_empty() {
        return None;
    }
    let z = reference
        .iter()
        .zip(signal)
        .map(|(a, b)| a.conj() * b)
        .sum::<Complex64>();
    (z.norm() > 0.).then(|| z.arg())
}

pub fn scale_cir(
    input: &[Complex64],
    dgc_linear_gain: f64,
    accumulation_count: u32,
) -> (Vec<Complex64>, QualityFlags) {
    let mut q = QualityFlags::NONE;
    if accumulation_count == 0 {
        q.insert(QualityFlags::LOW_ACCUMULATION);
        return (vec![Complex64::new(0., 0.); input.len()], q);
    }
    if accumulation_count < 16 {
        q.insert(QualityFlags::LOW_ACCUMULATION);
    }
    let scale = dgc_linear_gain / accumulation_count as f64;
    (input.iter().map(|x| x * scale).collect(), q)
}

#[derive(Clone, Copy, Debug)]
pub enum ReferenceMode {
    First,
    Qualified {
        minimum_energy: f64,
    },
    Adaptive {
        minimum_energy: f64,
        half_life_s: f64,
    },
}
pub struct CirReference {
    mode: ReferenceMode,
    value: Option<Vec<Complex64>>,
    last_time_s: Option<f64>,
}
impl CirReference {
    pub fn new(mode: ReferenceMode) -> Self {
        if let ReferenceMode::Adaptive { half_life_s, .. } = mode {
            assert!(half_life_s > 0.0);
        }
        Self {
            mode,
            value: None,
            last_time_s: None,
        }
    }
    pub fn update(&mut self, time: f64, cir: &[Complex64]) -> Option<&[Complex64]> {
        if self.last_time_s.is_some_and(|last| time < last) {
            return self.value.as_deref();
        }
        let energy = cir.iter().map(|x| x.norm_sqr()).sum::<f64>();
        let qualified = match self.mode {
            ReferenceMode::First => true,
            ReferenceMode::Qualified { minimum_energy }
            | ReferenceMode::Adaptive { minimum_energy, .. } => energy >= minimum_energy,
        };
        if !qualified {
            return self.value.as_deref();
        }
        match self.mode {
            ReferenceMode::First | ReferenceMode::Qualified { .. } => {
                if self.value.is_none() {
                    self.value = Some(cir.to_vec());
                    self.last_time_s = Some(time);
                }
            }
            ReferenceMode::Adaptive { half_life_s, .. } => {
                if let Some(v) = &mut self.value {
                    if time >= self.last_time_s.unwrap_or(time) && v.len() == cir.len() {
                        let retain =
                            2f64.powf(-(time - self.last_time_s.unwrap_or(time)) / half_life_s);
                        for (a, b) in v.iter_mut().zip(cir) {
                            *a = *a * retain + *b * (1. - retain);
                        }
                    }
                } else {
                    self.value = Some(cir.to_vec());
                }
                self.last_time_s = Some(time);
            }
        }
        self.value.as_deref()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn pulse(n: usize, x: f64, phase: f64) -> Vec<Complex64> {
        (0..n)
            .map(|i| Complex64::from_polar((-((i as f64 - x) / 1.5).powi(2)).exp(), phase))
            .collect()
    }
    #[test]
    fn resampling_delay_alignment_and_phase() {
        let a = pulse(40, 15., 0.);
        let b = pulse(40, 17.25, 0.7);
        let d = normalized_correlation_delay(&a, &b, 5).unwrap();
        assert!((d.delay_samples - 2.25).abs() <= 1. / 16.);
        let aligned = fractional_align_non_circular(&b, 2.25);
        let p = common_phase(&a, &aligned).unwrap();
        assert!((p - 0.7).abs() < 0.02);
        assert_eq!(resample_cir_16x(&a).len(), 625);
    }
    #[test]
    fn non_circular_and_reference_half_life() {
        let x = vec![Complex64::new(1., 0.), Complex64::new(0., 0.)];
        let y = fractional_align_non_circular(&x, -1.);
        assert!(y[0].norm() < 1e-9);
        let mut r = CirReference::new(ReferenceMode::Adaptive {
            minimum_energy: 0.,
            half_life_s: 2.,
        });
        r.update(0., &[Complex64::new(0., 0.)]);
        let v = r.update(2., &[Complex64::new(2., 0.)]).unwrap();
        assert!((v[0].re - 1.).abs() < 1e-12);
    }
    #[test]
    fn dgc_accumulation_scale() {
        let (v, q) = scale_cir(&[Complex64::new(10., 0.)], 2., 10);
        assert_eq!(v[0].re, 2.);
        assert!(q.contains(QualityFlags::LOW_ACCUMULATION));
    }

    fn synthetic_signal(t: f64) -> Complex64 {
        Complex64::from_polar(1.0, 0.30 * t)
            + Complex64::from_polar(0.7, 0.75 * t + 0.4)
            + Complex64::from_polar(0.4, 1.10 * t - 0.2)
    }

    fn synthetic_reference() -> Vec<Complex64> {
        (0..64)
            .map(|index| {
                if index < 14 {
                    let real = ((index * 17 % 11) as f64 - 5.0) * 0.001;
                    let imag = ((index * 7 % 9) as f64 - 4.0) * 0.001;
                    Complex64::new(real, imag)
                } else {
                    synthetic_signal(index as f64)
                }
            })
            .collect()
    }

    fn transformed_current(
        reference: &[Complex64],
        delay: f64,
        gain_db: f64,
        phase: f64,
    ) -> Vec<Complex64> {
        let gain_phase = Complex64::from_polar(10f64.powf(gain_db / 20.0), phase);
        (0..reference.len())
            .map(|index| {
                let source = index as f64 - delay;
                let value = if source >= 14.0 {
                    synthetic_signal(source)
                } else {
                    interpolate_runtime(reference, (source * 16.0).round() as i64, 16, false)
                        .unwrap()
                };
                value * gain_phase
            })
            .collect()
    }

    #[test]
    fn robust_hierarchical_alignment_handles_changed_taps_in_both_modes() {
        let reference = synthetic_reference();
        let mut current = transformed_current(&reference, 1.25, 3.0, 27f64.to_radians());
        current[27] += Complex64::new(0.8, -0.6);
        current[40] += Complex64::new(-1.0, 0.4);
        current[52] += Complex64::new(0.6, 0.8);

        for score_mode in [CirAlignmentScoreMode::Count, CirAlignmentScoreMode::Soft] {
            let result = align_cir_hierarchical(
                &reference,
                &current,
                CirAlignmentConfig {
                    score_mode,
                    ..CirAlignmentConfig::default()
                },
            )
            .unwrap();
            let delay_tolerance = match score_mode {
                CirAlignmentScoreMode::Count => 0.25,
                CirAlignmentScoreMode::Soft => 0.25,
            };
            assert!(
                (result.delay_samples - 1.25).abs() <= delay_tolerance,
                "{score_mode:?}: delay={}, gain={}, phase={}",
                result.delay_samples,
                result.gain_db,
                result.phase_degrees
            );
            assert!((result.gain_db - 3.0).abs() <= 0.5);
            // Robust scores are non-convex and may select an adjacent coarse
            // cell before local refinement.
            let phase_tolerance = 5.0;
            assert!(
                (result.phase_degrees - 27.0).abs() <= phase_tolerance,
                "{score_mode:?}: delay={}, gain={}, phase={}",
                result.delay_samples,
                result.gain_db,
                result.phase_degrees
            );
            assert!((result.phase_radians - result.phase_degrees.to_radians()).abs() < 1e-12);
            assert!(result.matched_count * 2 > result.eligible_count);
            assert!(result.matched_count < result.eligible_count);
            assert_eq!(result.corrected.len(), current.len());
            assert!((result.corrected[39] - reference[39]).norm() > 0.2);
        }
    }

    #[test]
    fn robust_hierarchical_alignment_wraps_phase_grid() {
        let reference = synthetic_reference();
        let current = transformed_current(&reference, 0.0, 0.0, 179f64.to_radians());
        let result =
            align_cir_hierarchical(&reference, &current, CirAlignmentConfig::default()).unwrap();
        let phase_error = (result.phase_degrees - 179.0 + 180.0).rem_euclid(360.0) - 180.0;
        assert!(phase_error.abs() <= 5.0, "phase={}", result.phase_degrees);
    }

    #[test]
    fn approximate_phase_stays_within_coarse_guard_cell() {
        for degrees in -180..180 {
            let value = Complex64::from_polar(1.0, (degrees as f64).to_radians());
            let approximate = approximate_phase_degrees(value.im, value.re);
            let error = (approximate - degrees as f64 + 180.0).rem_euclid(360.0) - 180.0;
            assert!(error.abs() < 5.0, "degrees={degrees}, error={error}");
        }
    }

    #[test]
    fn hierarchical_alignment_uses_original_reference_noise_gate() {
        let mut reference = (0..16)
            .map(|index| Complex64::new(if index % 2 == 0 { 1.0 } else { -1.0 }, 0.0))
            .collect::<Vec<_>>();
        reference[14] = Complex64::new(2.0, 0.0);
        reference[15] = Complex64::new(4.0, 0.0);
        let result = align_cir_hierarchical(
            &reference,
            &reference,
            CirAlignmentConfig {
                gain_min_db: 0.0,
                gain_max_db: 0.0,
                delay_range_samples: 0.0,
                ..CirAlignmentConfig::default()
            },
        )
        .unwrap();
        assert!(result.eligible_count > 1);
        assert_eq!(result.matched_count, result.eligible_count);
        assert_eq!(result.score, result.eligible_count as f64);
    }

    #[test]
    fn hierarchical_alignment_rejects_invalid_inputs_and_config() {
        let cir = vec![Complex64::new(1.0, 0.0); 16];
        for factor in [1, 2, 4, 8, 16] {
            assert_eq!(
                correct_cir_non_circular(&cir, 0.0, 0.0, 0.0, factor).unwrap(),
                cir
            );
        }
        assert!(
            align_cir_hierarchical(
                &cir,
                &cir,
                CirAlignmentConfig {
                    resampling_factor: 3,
                    ..CirAlignmentConfig::default()
                }
            )
            .is_none()
        );
        assert!(
            align_cir_hierarchical(
                &cir,
                &cir,
                CirAlignmentConfig {
                    eta: 0.0,
                    ..CirAlignmentConfig::default()
                }
            )
            .is_none()
        );
        assert!(correct_cir_non_circular(&cir, 0.0, 0.0, 0.0, 3).is_none());
        assert!(align_cir_hierarchical(&[], &cir, CirAlignmentConfig::default()).is_none());

        let reference = synthetic_reference();
        let current = transformed_current(&reference, 1.0, 0.0, 0.0);
        let result = align_cir_hierarchical(
            &reference,
            &current,
            CirAlignmentConfig {
                resampling_factor: 1,
                ..CirAlignmentConfig::default()
            },
        )
        .unwrap();
        assert_eq!(result.delay_samples.fract(), 0.0);
        let expected_peak = resample_cir_16x(&reference)
            .iter()
            .map(|value| value.norm())
            .max_by(f64::total_cmp)
            .unwrap();
        assert!((resampled_cir_peak(&reference, 16).unwrap() - expected_peak).abs() < 1e-12);
        assert!(resampled_cir_peak(&reference, 3).is_none());
    }
}
