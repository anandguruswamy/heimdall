"""Per-scene gradient optimization through UWBRender (plan Phase 4).

Fits `SceneTensors` leaves with Adam through `render_scene`. Losses:
phase-invariant complex Charbonnier (paper Eq. (25)) and log-envelope
(Eq. (26)) with an `envelope_first` curriculum (envelope-only for the
first fraction of iterations to avoid carrier-phase local minima), plus
presence-sparsity, scale-bound, and plane-overlap regularizers
(paper Sec. VII-C subset; weights PROVISIONAL in the experiment configs).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import torch

from nrecon.constants import S_TAPS
from nrecon.sim.delay import fractional_shift
from nrecon.sim.primitives import PLANE, SURFEL, SceneTensors
from nrecon.sim.render import render_scene

ENVELOPE_EPS = 1e-3  # log-envelope floor (PROVISIONAL)
PRUNE_THRESHOLD = 0.05
LOS_EXCLUDE_TAPS = 5  # scene-independent direct-path region excluded from the fit loss


@dataclass
class FitConfig:
    iterations: int = 300
    lr: float = 1e-2
    envelope_first_frac: float = 0.5
    env_weight: float = 1.0
    lambda_presence: float = 1e-2
    lambda_scale: float = 0.1
    lambda_overlap: float = 0.0
    overlap_sigma: float = 0.5
    scale_log_min: float = -4.0
    scale_log_max: float = 1.5
    epsilon_char: float = 1e-3
    dtype: torch.dtype = torch.float64


@dataclass
class FitResult:
    scene: SceneTensors
    loss_trace: list = field(default_factory=list)
    env_trace: list = field(default_factory=list)
    cpx_trace: list = field(default_factory=list)
    per_link_env: np.ndarray = None
    runtime_s: float = 0.0
    converged: bool = True


def _phase_invariant_loss(h_hat: torch.Tensor, h: torch.Tensor,
                          eps: float) -> torch.Tensor:
    phi = torch.angle((h_hat.conj() * h).sum(dim=-1))
    h_hat_al = h_hat * torch.exp(1j * phi)[:, None]
    diff = h - h_hat_al
    return torch.sqrt(diff.abs() ** 2 + eps**2).mean()


def _envelope_loss(h_hat: torch.Tensor, h: torch.Tensor,
                   eps: float = ENVELOPE_EPS,
                   normalize_per_link: bool = True) -> torch.Tensor:
    """Log-envelope loss (paper Eq. (26)).

    With `normalize_per_link` (default), each CIR is divided by its own
    RMS so the loss measures echo *shape/delay* alignment rather than the
    fixed per-link amplitude ratio between render and quantized target
    (paper Eq. (9) amplitude normalization).
    """
    if normalize_per_link:
        h_hat = h_hat / h_hat.abs().pow(2).mean(dim=-1, keepdim=True).sqrt().clamp(min=1e-12)
        h = h / h.abs().pow(2).mean(dim=-1, keepdim=True).sqrt().clamp(min=1e-12)
    return (torch.log(eps + h.abs()) - torch.log(eps + h_hat.abs())).abs().mean()


def _regularizers(scene: SceneTensors, cfg: FitConfig) -> torch.Tensor:
    loss = cfg.lambda_presence * scene.presence.sum()
    s = scene.scale_log
    loss = loss + cfg.lambda_scale * (
        (cfg.scale_log_min - s).relu().sum() + (s - cfg.scale_log_max).relu().sum()
    )
    if cfg.lambda_overlap > 0:
        plane_idx = [g for g in range(scene.type_id.numel())
                     if int(scene.type_id[g]) == PLANE]
        for i in range(len(plane_idx)):
            for j in range(i + 1, len(plane_idx)):
                gi, gj = plane_idx[i], plane_idx[j]
                d2 = (scene.center[gi] - scene.center[gj]).pow(2).sum()
                loss = loss + cfg.lambda_overlap * scene.presence[gi] * scene.presence[gj] * \
                    torch.exp(-d2 / (2.0 * cfg.overlap_sigma**2))
    return loss


def fit_scene(target: torch.Tensor, fp_taps: torch.Tensor, nodes: torch.Tensor,
              kernel: torch.Tensor, init_scene: SceneTensors, cfg: FitConfig,
              gain_accum: np.ndarray = None,
              link_mask: np.ndarray = None,
              kernel_scales_taps: list = None) -> FitResult:
    """Fit `init_scene` to `target` ([L, 64] pipeline-domain, LOS at fp).

    `gain_accum` per link = 10^((dgc-3)*2.65/20) / accum, mapping the
    accumulator-domain render onto the pipeline-domain target.
    `kernel_scales_taps`: multiscale schedule — fit for a share of the
    iterations through successively narrower broadened kernels, which
    widens the loss basins at coarse scales (initialization and plane
    rotation-basin escape).
    """
    scene = _clone_trainable(init_scene, cfg.dtype)
    cplx = torch.complex128 if cfg.dtype == torch.float64 else torch.complex64
    t0 = fractional_shift(target.to(cplx), -fp_taps.to(cfg.dtype))
    nodes = nodes.to(cfg.dtype)

    # Pipeline-domain mapping: target = from_i16(to_i16(h_true)) ~= h_true / 4
    # (the accumulator gain/accum factors cancel; the /4 is the transport
    # arithmetic shift). Uniform across links; per-link quantization noise
    # remains in the target.
    scale = torch.ones(target.shape[0], 1, dtype=cfg.dtype) / 4.0

    if link_mask is None:
        mask = torch.ones(target.shape[0], dtype=torch.bool)
    else:
        mask = torch.as_tensor(link_mask, dtype=torch.bool)
    h_masked = t0[mask][:, LOS_EXCLUDE_TAPS:]
    scale_masked = scale[mask]

    params = [scene.center, scene.rot6d, scene.scale_log, scene.rho, scene.presence]
    optim = torch.optim.Adam(params, lr=cfg.lr)
    env_cut = int(cfg.envelope_first_frac * cfg.iterations)

    from nrecon.sim.render import _gauss_broadened
    import torch.nn.functional as F_

    def scale_kernel(s_taps: float) -> torch.Tensor:
        if s_taps <= 0.0:
            return kernel
        return _gauss_broadened(kernel, torch.as_tensor(s_taps / 998.4e6))

    def smooth_target(h: torch.Tensor, s_taps: float) -> torch.Tensor:
        """Smooth the target at the same scale as the render kernel, so the
        coarse stages compare matched-smoothed pulses (wide, strong basins)."""
        if s_taps <= 0.0:
            return h
        g_std = s_taps
        m = int(np.ceil(4.0 * g_std))
        idx = torch.arange(-m, m + 1, dtype=cfg.dtype)
        g = torch.exp(-(idx**2) / (2.0 * g_std**2))
        g = g / g.sum()
        padded = F_.pad(h, (m, m))
        out = F_.conv1d(padded.unsqueeze(1), g.view(1, 1, -1).to(torch.complex128))
        return out.squeeze(1)

    if kernel_scales_taps:
        per_scale = max(1, cfg.iterations // len(kernel_scales_taps))
        schedule = [(s, per_scale) for s in kernel_scales_taps]
        schedule[-1] = (schedule[-1][0], cfg.iterations - per_scale * (len(schedule) - 1))
    else:
        schedule = [(0.0, cfg.iterations)]

    start = time.perf_counter()
    loss_trace = []
    env_trace = []
    cpx_trace = []
    global_it = 0
    for s_taps, n_iters in schedule:
        k_eff = scale_kernel(s_taps)
        excl = max(LOS_EXCLUDE_TAPS, int(np.ceil(s_taps)) + 2)
        t_eff = smooth_target(t0, s_taps)[:, excl:]
        t_masked = t_eff[mask]
        # coarse scales smooth the echo below the fine-scale log floor; drop
        # the floor so the smoothed mismatch stays visible to the loss
        eps_env = 1e-4 if s_taps > 0.5 else ENVELOPE_EPS
        for it in range(n_iters):
            optim.zero_grad()
            h = render_scene(scene, nodes, k_eff)
            h_pipe = h * scale
            h_pipe_m = h_pipe[mask][:, excl:]
            env = _envelope_loss(h_pipe_m, t_masked, eps_env)
            cpx = _phase_invariant_loss(h_pipe_m, t_masked, cfg.epsilon_char)
            reg = _regularizers(scene, cfg)
            if global_it < env_cut:
                loss = env + reg
            else:
                loss = cpx + cfg.env_weight * env + reg
            loss.backward()
            optim.step()
            with torch.no_grad():
                scene.presence.clamp_(0.0, 1.0)  # keep the sparsity penalty bounded
            if it % 25 == 0 or it == n_iters - 1:
                loss_trace.append(float(loss.detach()))
                env_trace.append(float(env.detach()))
                cpx_trace.append(float(cpx.detach()))
            global_it += 1
    elapsed = time.perf_counter() - start

    # final per-link residuals (full links)
    with torch.no_grad():
        h = render_scene(scene, nodes, kernel)
        h_pipe = h * scale
        per_link = torch.sqrt(
            (h_pipe - t0).abs() ** 2 + cfg.epsilon_char**2
        ).mean(dim=-1).detach().numpy()

    return FitResult(
        scene=scene, loss_trace=loss_trace, env_trace=env_trace,
        cpx_trace=cpx_trace, per_link_env=per_link, runtime_s=elapsed,
    )


def _clone_trainable(scene: SceneTensors, dtype: torch.dtype) -> SceneTensors:
    out = SceneTensors(
        type_id=scene.type_id.clone(),
        presence=scene.presence.clone().to(dtype).requires_grad_(True),
        center=scene.center.clone().to(dtype).requires_grad_(True),
        rot6d=scene.rot6d.clone().to(dtype).requires_grad_(True),
        scale_log=scene.scale_log.clone().to(dtype).requires_grad_(True),
        rho=scene.rho.clone().to(
            torch.complex128 if dtype == torch.float64 else torch.complex64
        ).requires_grad_(True),
        roughness=scene.roughness.clone().to(dtype),
        atten=scene.atten.clone().to(dtype),
        dynamic_p=scene.dynamic_p.clone().to(dtype),
    )
    return out


def init_gt_perturbed(truth: SceneTensors, rng: np.random.Generator,
                      pos_m: float = 0.05, rot: float = 0.1,
                      scale: float = 0.1, rho: float = 0.05) -> SceneTensors:
    out = SceneTensors.empty(truth.type_id.numel())
    out.type_id = truth.type_id.clone()
    for g in range(truth.type_id.numel()):
        if int(truth.type_id[g]) == 0:
            continue
        out.presence[g] = 1.0
        out.center[g] = truth.center[g] + torch.as_tensor(
            rng.normal(0.0, pos_m, size=3), dtype=torch.float64)
        out.rot6d[g] = truth.rot6d[g] + torch.as_tensor(
            rng.normal(0.0, rot, size=6), dtype=torch.float64)
        out.scale_log[g] = truth.scale_log[g] + torch.as_tensor(
            rng.normal(0.0, scale, size=3), dtype=torch.float64)
        out.rho[g] = truth.rho[g] + rng.normal(0.0, rho) + 1j * rng.normal(0.0, rho)
        out.roughness[g] = truth.roughness[g]
    return out


def init_random(rng: np.random.Generator, type_ids: list, bounds: np.ndarray,
                g_extra: int = 0) -> SceneTensors:
    """Random init: one slot per `type_ids` plus `g_extra` empty slots.

    `bounds` [2, 3] (min, max) for positions.
    """
    slots = len(type_ids) + g_extra
    scene = SceneTensors.empty(slots)
    for g, t in enumerate(type_ids):
        scene.type_id[g] = t
        scene.presence[g] = 1.0
        scene.center[g] = torch.as_tensor(
            rng.uniform(bounds[0], bounds[1], size=3), dtype=torch.float64)
        scene.rot6d[g] = torch.as_tensor(
            rng.normal(0.0, 1.0, size=6), dtype=torch.float64)
        scene.scale_log[g] = torch.as_tensor(
            rng.uniform(np.log(0.1), np.log(0.8), size=3), dtype=torch.float64)
        scene.rho[g] = 0.5 + 0.2j * rng.standard_normal()
    return scene


def init_from_points(points: np.ndarray, rho: complex = 0.5 + 0.0j) -> SceneTensors:
    """Surfel slots at candidate points (voting init)."""
    n = points.shape[0]
    scene = SceneTensors.empty(max(n, 1))
    for g in range(n):
        scene.type_id[g] = SURFEL
        scene.presence[g] = 1.0
        scene.center[g] = torch.as_tensor(points[g], dtype=torch.float64)
        scene.scale_log[g] = torch.log(torch.tensor([0.25, 0.25, 0.25],
                                                    dtype=torch.float64))
        scene.rho[g] = rho
    return scene
