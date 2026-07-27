use std::collections::VecDeque;

#[derive(Clone, Debug)]
pub struct HampelResult {
    pub value: f64,
    pub is_outlier: bool,
    pub median: f64,
    pub sigma: f64,
}

pub fn hampel(
    values: &[f64],
    center: usize,
    radius: usize,
    threshold_sigma: f64,
) -> Option<HampelResult> {
    if center >= values.len() {
        return None;
    }
    let lo = center.saturating_sub(radius);
    let hi = (center + radius + 1).min(values.len());
    let mut window: Vec<f64> = values[lo..hi]
        .iter()
        .copied()
        .filter(|x| x.is_finite())
        .collect();
    if window.is_empty() || !values[center].is_finite() {
        return None;
    }
    let median = med(&mut window);
    let mut dev: Vec<f64> = window.iter().map(|x| (x - median).abs()).collect();
    let sigma = 1.4826 * med(&mut dev);
    let out = if sigma == 0.0 {
        (values[center] - median).abs() > 0.0
    } else {
        (values[center] - median).abs() > threshold_sigma * sigma
    };
    Some(HampelResult {
        value: if out { median } else { values[center] },
        is_outlier: out,
        median,
        sigma,
    })
}
fn med(v: &mut [f64]) -> f64 {
    v.sort_by(f64::total_cmp);
    let n = v.len();
    if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) * 0.5
    }
}

#[derive(Clone, Debug)]
pub struct TimeMovingAverage {
    window_s: f64,
    samples: VecDeque<(f64, f64)>,
    sum: f64,
}
impl TimeMovingAverage {
    pub fn new(window_s: f64) -> Self {
        assert!(window_s >= 0.0);
        Self {
            window_s,
            samples: VecDeque::new(),
            sum: 0.0,
        }
    }
    pub fn push(&mut self, event_time_s: f64, value: f64) -> f64 {
        if self.samples.back().is_some_and(|x| event_time_s < x.0) {
            return self.mean().unwrap_or(value);
        }
        self.samples.push_back((event_time_s, value));
        self.sum += value;
        while self
            .samples
            .front()
            .is_some_and(|x| event_time_s - x.0 > self.window_s)
        {
            self.sum -= self.samples.pop_front().unwrap().1;
        }
        self.sum / self.samples.len() as f64
    }
    pub fn mean(&self) -> Option<f64> {
        (!self.samples.is_empty()).then(|| self.sum / self.samples.len() as f64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hampel_replaces_spike() {
        let x = [1., 1.1, 30., 0.9, 1.];
        let r = hampel(&x, 2, 2, 3.).unwrap();
        assert!(r.is_outlier);
        assert_eq!(r.value, 1.);
    }
    #[test]
    fn causal_window() {
        let mut m = TimeMovingAverage::new(1.);
        assert_eq!(m.push(0., 1.), 1.);
        assert_eq!(m.push(0.5, 3.0), 2.0);
        assert_eq!(m.push(1.1, 5.), 4.);
    }
}
