use crate::QualityFlags;
use num_complex::Complex64;
use rustfft::FftPlanner;
use std::f64::consts::PI;

#[derive(Clone, Copy, Debug)]
pub enum FftWindow {
    Rectangular,
    Hann,
    Hamming,
    Blackman,
}
#[derive(Clone, Debug)]
pub struct Spectrum {
    pub frequencies_hz: Vec<f64>,
    pub bins: Vec<Complex64>,
    pub quality: QualityFlags,
}

fn weight(w: FftWindow, i: usize, n: usize) -> f64 {
    if n <= 1 {
        return 1.;
    }
    let x = 2. * PI * i as f64 / (n - 1) as f64;
    match w {
        FftWindow::Rectangular => 1.,
        FftWindow::Hann => 0.5 - 0.5 * x.cos(),
        FftWindow::Hamming => 0.54 - 0.46 * x.cos(),
        FftWindow::Blackman => 0.42 - 0.5 * x.cos() + 0.08 * (2. * x).cos(),
    }
}

/// Linearly fills only bounded gaps no longer than `max_gap`; longer gaps remain an error.
pub fn interpolate_short_gaps(
    samples: &[Option<f64>],
    max_gap: usize,
) -> Option<(Vec<f64>, QualityFlags)> {
    let mut out: Vec<f64> = samples.iter().map(|x| x.unwrap_or(f64::NAN)).collect();
    let mut quality = QualityFlags::NONE;
    let mut i = 0;
    while i < out.len() {
        if out[i].is_finite() {
            i += 1;
            continue;
        }
        let start = i;
        while i < out.len() && !out[i].is_finite() {
            i += 1;
        }
        let len = i - start;
        if start == 0 || i == out.len() || len > max_gap {
            return None;
        }
        let (a, b) = (out[start - 1], out[i]);
        for k in 0..len {
            out[start + k] = a + (b - a) * (k + 1) as f64 / (len + 1) as f64;
        }
        quality.insert(QualityFlags::INTERPOLATED_GAP);
    }
    Some((out, quality))
}

fn prepared(samples: &[f64], window: FftWindow) -> Vec<Complex64> {
    samples
        .iter()
        .enumerate()
        .map(|(i, &x)| Complex64::new(x * weight(window, i, samples.len()), 0.))
        .collect()
}
fn spectrum(mut bins: Vec<Complex64>, sample_rate_hz: f64, quality: QualityFlags) -> Spectrum {
    let n = bins.len();
    bins.truncate(n / 2 + 1);
    let frequencies_hz = (0..bins.len())
        .map(|k| k as f64 * sample_rate_hz / n as f64)
        .collect();
    Spectrum {
        frequencies_hz,
        bins,
        quality,
    }
}

pub fn fast_fft(
    samples: &[f64],
    sample_rate_hz: f64,
    window: FftWindow,
    quality: QualityFlags,
) -> Spectrum {
    let mut x = prepared(samples, window);
    if !x.is_empty() {
        FftPlanner::<f64>::new()
            .plan_fft_forward(x.len())
            .process(&mut x);
    }
    spectrum(x, sample_rate_hz, quality)
}

pub fn fast_fft_complex(
    samples: &[Complex64],
    sample_rate_hz: f64,
    window: FftWindow,
    quality: QualityFlags,
) -> Spectrum {
    let mut bins = samples
        .iter()
        .enumerate()
        .map(|(index, sample)| *sample * weight(window, index, samples.len()))
        .collect::<Vec<_>>();
    if !bins.is_empty() {
        FftPlanner::<f64>::new()
            .plan_fft_forward(bins.len())
            .process(&mut bins);
    }
    let frequencies_hz = (0..bins.len())
        .map(|index| index as f64 * sample_rate_hz / bins.len() as f64)
        .collect();
    Spectrum {
        frequencies_hz,
        bins,
        quality,
    }
}
pub fn slow_fft(
    samples: &[f64],
    sample_rate_hz: f64,
    window: FftWindow,
    quality: QualityFlags,
) -> Spectrum {
    let x = prepared(samples, window);
    let n = x.len();
    let bins = (0..n)
        .map(|k| {
            (0..n)
                .map(|j| {
                    x[j] * Complex64::from_polar(1., -2. * PI * k as f64 * j as f64 / n as f64)
                })
                .sum()
        })
        .collect();
    spectrum(bins, sample_rate_hz, quality)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fft_tone_fast_matches_slow_and_nyquist() {
        let n = 128;
        let fs = 128.;
        let x: Vec<f64> = (0..n)
            .map(|i| (2. * PI * 11. * i as f64 / fs).sin())
            .collect();
        let a = fast_fft(&x, fs, FftWindow::Rectangular, QualityFlags::NONE);
        let b = slow_fft(&x, fs, FftWindow::Rectangular, QualityFlags::NONE);
        assert_eq!(a.bins.len(), 65);
        assert_eq!(a.frequencies_hz[64], 64.);
        let peak = (1..a.bins.len())
            .max_by(|&i, &j| a.bins[i].norm().total_cmp(&a.bins[j].norm()))
            .unwrap();
        assert_eq!(peak, 11);
        for (i, j) in a.bins.iter().zip(b.bins) {
            assert!((*i - j).norm() < 1e-9);
        }
    }
    #[test]
    fn short_gap_quality_and_rejection() {
        let (x, q) = interpolate_short_gaps(&[Some(0.), None, Some(2.)], 1).unwrap();
        assert_eq!(x, [0., 1., 2.]);
        assert!(q.contains(QualityFlags::INTERPOLATED_GAP));
        assert!(interpolate_short_gaps(&[Some(0.), None, None, Some(3.)], 1).is_none());
    }
}
