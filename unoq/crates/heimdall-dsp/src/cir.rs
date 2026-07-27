use crate::QualityFlags;
use num_complex::Complex64;
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

pub fn fractional_align_non_circular(input: &[Complex64], shift_samples: f64) -> Vec<Complex64> {
    let shift = (shift_samples * CIR_INTERPOLATION as f64).round() as i64;
    (0..input.len())
        .map(|k| interpolate_16x(input, k as i64 * CIR_INTERPOLATION as i64 + shift, false))
        .collect()
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
}
