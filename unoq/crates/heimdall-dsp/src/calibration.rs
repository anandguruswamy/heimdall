use std::collections::{HashMap, HashSet};

#[derive(Clone, Copy, Debug)]
pub struct PairCalibrationObservation {
    pub board_a: u32,
    pub board_b: u32,
    pub measured_bias: f64,
    pub variance: f64,
}

#[derive(Clone, Debug)]
pub struct MatrixDiagnostics {
    pub rank: usize,
    pub columns: usize,
    pub condition_number: f64,
    pub has_full_rank: bool,
}

#[derive(Clone, Debug)]
pub struct CalibrationResult {
    pub board_offsets: Vec<(u32, f64)>,
    pub residuals: Vec<f64>,
    pub diagnostics: MatrixDiagnostics,
    pub recommended_next_pair: Option<(u32, u32)>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CalibrationError {
    Empty,
    InvalidObservation,
    Singular,
}

/// Fits pair bias = effective_offset[a] + effective_offset[b].
pub fn calibrate_offsets(
    observations: &[PairCalibrationObservation],
    regularization: f64,
) -> Result<CalibrationResult, CalibrationError> {
    if observations.is_empty() {
        return Err(CalibrationError::Empty);
    }
    if regularization < 0.0
        || !regularization.is_finite()
        || observations.iter().any(|o| {
            o.board_a == o.board_b
                || !o.measured_bias.is_finite()
                || !o.variance.is_finite()
                || o.variance <= 0.0
        })
    {
        return Err(CalibrationError::InvalidObservation);
    }
    let mut boards: Vec<u32> = observations
        .iter()
        .flat_map(|o| [o.board_a, o.board_b])
        .collect();
    boards.sort_unstable();
    boards.dedup();
    let index: HashMap<u32, usize> = boards.iter().enumerate().map(|(i, &b)| (b, i)).collect();
    let normal = normal_matrix(observations, &index, 0.0);
    let diagnostics = diagnostics(&normal);
    let mut system = normal.clone();
    let mut rhs = vec![0.0; boards.len()];
    for o in observations {
        let w = 1.0 / o.variance;
        let i = index[&o.board_a];
        let j = index[&o.board_b];
        rhs[i] += w * o.measured_bias;
        rhs[j] += w * o.measured_bias;
    }
    for (i, row) in system.iter_mut().enumerate() {
        row[i] += regularization;
    }
    let x = solve(system, rhs).ok_or(CalibrationError::Singular)?;
    let residuals = observations
        .iter()
        .map(|o| o.measured_bias - x[index[&o.board_a]] - x[index[&o.board_b]])
        .collect();
    let recommended_next_pair = recommend_pair(&boards, observations, &index, &normal);
    Ok(CalibrationResult {
        board_offsets: boards.into_iter().zip(x).collect(),
        residuals,
        diagnostics,
        recommended_next_pair,
    })
}

fn normal_matrix(
    obs: &[PairCalibrationObservation],
    idx: &HashMap<u32, usize>,
    ridge: f64,
) -> Vec<Vec<f64>> {
    let n = idx.len();
    let mut m = vec![vec![0.; n]; n];
    for o in obs {
        let w = 1. / o.variance;
        let i = idx[&o.board_a];
        let j = idx[&o.board_b];
        m[i][i] += w;
        m[j][j] += w;
        m[i][j] += w;
        m[j][i] += w;
    }
    for (i, r) in m.iter_mut().enumerate() {
        r[i] += ridge;
    }
    m
}
fn diagnostics(m: &[Vec<f64>]) -> MatrixDiagnostics {
    let mut e = eigenvalues(m);
    e.sort_by(f64::total_cmp);
    let max = e.last().copied().unwrap_or(0.).max(0.);
    let tol = max * (m.len() as f64) * 1e-10;
    let rank = e.iter().filter(|&&x| x > tol).count();
    let min = e.iter().copied().filter(|&x| x > tol).next();
    MatrixDiagnostics {
        rank,
        columns: m.len(),
        condition_number: min.map_or(f64::INFINITY, |x| max / x),
        has_full_rank: rank == m.len(),
    }
}
fn eigenvalues(a: &[Vec<f64>]) -> Vec<f64> {
    let n = a.len();
    let mut x = a.to_vec();
    for _ in 0..(50 * n * n).max(1) {
        let mut p = 0;
        let mut q = 0;
        let mut largest = 0.;
        for i in 0..n {
            for j in i + 1..n {
                if x[i][j].abs() > largest {
                    largest = x[i][j].abs();
                    p = i;
                    q = j;
                }
            }
        }
        if largest < 1e-12 {
            break;
        }
        let phi = 0.5 * (2. * x[p][q]).atan2(x[q][q] - x[p][p]);
        let (c, s) = (phi.cos(), phi.sin());
        for k in 0..n {
            if k != p && k != q {
                let kp = x[k][p];
                let kq = x[k][q];
                x[k][p] = c * kp - s * kq;
                x[p][k] = x[k][p];
                x[k][q] = s * kp + c * kq;
                x[q][k] = x[k][q];
            }
        }
        let pp = x[p][p];
        let qq = x[q][q];
        let pq = x[p][q];
        x[p][p] = c * c * pp - 2. * s * c * pq + s * s * qq;
        x[q][q] = s * s * pp + 2. * s * c * pq + c * c * qq;
        x[p][q] = 0.;
        x[q][p] = 0.;
    }
    (0..n).map(|i| x[i][i]).collect()
}
fn solve(mut a: Vec<Vec<f64>>, mut b: Vec<f64>) -> Option<Vec<f64>> {
    let n = b.len();
    for c in 0..n {
        let p = (c..n).max_by(|&i, &j| a[i][c].abs().total_cmp(&a[j][c].abs()))?;
        if a[p][c].abs() < 1e-14 {
            return None;
        }
        a.swap(c, p);
        b.swap(c, p);
        let d = a[c][c];
        for j in c..n {
            a[c][j] /= d;
        }
        b[c] /= d;
        for i in 0..n {
            if i == c {
                continue;
            }
            let f = a[i][c];
            for j in c..n {
                a[i][j] -= f * a[c][j];
            }
            b[i] -= f * b[c];
        }
    }
    Some(b)
}
fn recommend_pair(
    boards: &[u32],
    obs: &[PairCalibrationObservation],
    idx: &HashMap<u32, usize>,
    base: &[Vec<f64>],
) -> Option<(u32, u32)> {
    let existing: HashSet<(u32, u32)> = obs
        .iter()
        .map(|o| {
            if o.board_a < o.board_b {
                (o.board_a, o.board_b)
            } else {
                (o.board_b, o.board_a)
            }
        })
        .collect();
    let mut best = None;
    for i in 0..boards.len() {
        for j in i + 1..boards.len() {
            if existing.contains(&(boards[i], boards[j])) {
                continue;
            }
            let mut m = base.to_vec();
            let (a, b) = (idx[&boards[i]], idx[&boards[j]]);
            m[a][a] += 1.;
            m[b][b] += 1.;
            m[a][b] += 1.;
            m[b][a] += 1.;
            let mut e = eigenvalues(&m);
            e.sort_by(f64::total_cmp);
            let score = e[0];
            if best.is_none_or(|x: (f64, u32, u32)| score > x.0) {
                best = Some((score, boards[i], boards[j]));
            }
        }
    }
    best.map(|x| (x.1, x.2))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn o(a: u32, b: u32, y: f64) -> PairCalibrationObservation {
        PairCalibrationObservation {
            board_a: a,
            board_b: b,
            measured_bias: y,
            variance: 1.,
        }
    }
    #[test]
    fn rank_requires_odd_cycle() {
        let path = calibrate_offsets(&[o(1, 2, 3.), o(2, 3, 5.)], 1e-6).unwrap();
        assert_eq!(path.diagnostics.rank, 2);
        assert_eq!(path.recommended_next_pair, Some((1, 3)));
        let tri = calibrate_offsets(&[o(1, 2, 3.), o(2, 3, 5.), o(1, 3, 4.)], 0.).unwrap();
        assert!(tri.diagnostics.has_full_rank);
        for ((_, got), want) in tri.board_offsets.iter().zip([1., 2., 3.]) {
            assert!((got - want).abs() < 1e-10);
        }
        assert!(tri.residuals.iter().all(|x| x.abs() < 1e-10));
    }
    #[test]
    fn variance_weighted_regularized_ls() {
        let r = calibrate_offsets(
            &[
                o(1, 2, 3.),
                PairCalibrationObservation {
                    variance: 0.01,
                    ..o(1, 2, 5.)
                },
            ],
            1.,
        )
        .unwrap();
        let sum = r.board_offsets.iter().map(|x| x.1).sum::<f64>();
        assert!((sum - 503.0 / 101.5).abs() < 1e-10);
    }
}
