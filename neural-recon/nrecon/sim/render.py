"""Differentiable UWBRender forward model (paper Sec. VI).

Pure functions batched over links. LOS is always rendered; scene paths are
LOS-relative delays (Eq. (4), (21)); sparse evaluation only touches the
pulse support around each path delay; nuisance gain, phase, and noise are
applied at assembly (Eq. (21)).
"""

from __future__ import annotations

from dataclasses import dataclass

import torch
import torch.nn.functional as F

from nrecon.constants import (
    C_AIR,
    FC_HZ,
    METRES_PER_TAP,
    OVERSAMPLE,
    S_TAPS,
    TS_NS,
    directed_links,
)
from nrecon.sim.delay import sample_kernel, windowed_sinc
from nrecon.sim.primitives import (
    CAPSULE,
    PLANE,
    SCALE_LOG_MIN,
    SURFEL,
    SceneTensors,
    capsule_axes,
    plane_axes,
    rot6d_to_matrix,
    surfel_covariance,
)

GAMMA = 2.0  # free-space path loss exponent (PROVISIONAL; randomized per link in datasets)
EPS_V = 0.05  # finite-patch gate softness, metres (PROVISIONAL)
DEN_EPS = 1e-6  # plane denominator guard threshold
NEAR_NODE_E1 = 0.02  # smooth near-node gate start, m (PROVISIONAL)
NEAR_NODE_E2 = 0.10  # smooth near-node gate end, m (PROVISIONAL)
CAPSULE_K = 12  # quadrature points per capsule (PROVISIONAL)
CAPSULE_QUAD = 16  # chord quadrature points per segment (PROVISIONAL)
CAPSULE_CHORD_KAPPA = 4.0  # chord attenuation exponent per metre (PROVISIONAL)
INC_SLOPE = 10.0  # sigmoid slope for incidence/self-visibility weights (PROVISIONAL)
DIFFUSE_SPREAD = 0.5  # surfel specular-lobe angular spread (PROVISIONAL)
SURFEL_SIGMA_MAX_NS = 15.0


@dataclass(frozen=True)
class SurfelPulseLookup:
    """Detached pulse lookup built once for one kernel/device/dtype."""

    backend: str  # "bank-16x" or "cache-1x-phase"
    table: torch.Tensor
    sigma_max_sec: float
    peak_taps: float
    phase_bins: int = 0
    relative_start: int = 0


@dataclass(frozen=True)
class CapsuleGeometry:
    center: torch.Tensor  # [B,C,3]
    axis: torch.Tensor  # [B,C,3]
    half_len: torch.Tensor  # [B,C]
    radius: torch.Tensor  # [B,C]
    valid: torch.Tensor  # [B,C]


def build_surfel_pulse_lookup(kernel: torch.Tensor, backend: str,
                              sigma_bins: int = 128, phase_bins: int = 128,
                              sigma_max_ns: float = SURFEL_SIGMA_MAX_NS
                              ) -> SurfelPulseLookup:
    """Precompute broadened pulses for a fixed renderer kernel."""
    if backend not in ("bank-16x", "cache-1x-phase"):
        raise ValueError(f"unknown surfel pulse backend {backend!r}")
    if sigma_bins < 2:
        raise ValueError("sigma_bins must be at least 2")
    if backend == "cache-1x-phase" and phase_bins < 2:
        raise ValueError("phase_bins must be at least 2")
    if sigma_max_ns <= 0.0:
        raise ValueError("sigma_max_ns must be positive")

    sigma_max_sec = sigma_max_ns * 1e-9
    with torch.no_grad():
        sigma = torch.linspace(0.0, sigma_max_sec, sigma_bins,
                               dtype=kernel.dtype, device=kernel.device)
        fine_bank = _gauss_broadened_batch(kernel, sigma).detach()
        peak = kernel_peak_taps(kernel)
        if backend == "bank-16x":
            table = fine_bank
            relative_start = 0
        else:
            relative_start = -int(round(peak))
            relative = torch.arange(relative_start, relative_start + S_TAPS,
                                    dtype=kernel.dtype, device=kernel.device)
            offsets = relative[None, :].expand(sigma_bins, -1) + peak
            coarse = sample_kernel(fine_bank, offsets)
            phases = torch.linspace(0.0, 1.0, phase_bins + 1,
                                    dtype=kernel.dtype, device=kernel.device)
            support = torch.arange(-8, 9, dtype=torch.long, device=kernel.device)
            shifted = []
            for phase in phases:
                source = relative - phase
                source_index = source - relative_start
                base = torch.floor(source_index).long()
                index = base[:, None] + support[None, :]
                valid = (index >= 0) & (index < S_TAPS)
                gathered = coarse[:, index.clamp(0, S_TAPS - 1)]
                weights = windowed_sinc(
                    source_index[:, None] - index.to(kernel.dtype))
                shifted.append((gathered * weights[None, :, :] * valid).sum(dim=-1))
            table = torch.stack(shifted, dim=1).detach()
    return SurfelPulseLookup(backend, table, sigma_max_sec, peak,
                             phase_bins if backend == "cache-1x-phase" else 0,
                             relative_start)


def _lookup_sigma_coordinates(lookup: SurfelPulseLookup,
                              sigma_tau: torch.Tensor) -> tuple:
    sigma = sigma_tau.clamp(0.0, lookup.sigma_max_sec)
    coordinate = sigma / lookup.sigma_max_sec * (lookup.table.shape[0] - 1)
    lower = torch.floor(coordinate).long().clamp(0, lookup.table.shape[0] - 2)
    upper = lower + 1
    weight = coordinate - lower.to(coordinate.dtype)
    return lower, upper, weight


def sample_surfel_pulse_lookup(lookup: SurfelPulseLookup,
                               sigma_tau: torch.Tensor,
                               delta_taps: torch.Tensor) -> torch.Tensor:
    """Evaluate cached pulses for arbitrary leading path dimensions -> [...,64]."""
    original_shape = sigma_tau.shape
    sigma_flat = sigma_tau.reshape(-1)
    delta_flat = delta_taps.reshape(-1)
    sigma_lo, sigma_hi, sigma_weight = _lookup_sigma_coordinates(lookup, sigma_flat)
    taps = torch.arange(S_TAPS, dtype=delta_flat.dtype, device=delta_flat.device)

    if lookup.backend == "bank-16x":
        fine = (taps[None, :] - delta_flat[:, None] + lookup.peak_taps) * OVERSAMPLE
        valid = (fine >= 0.0) & (fine <= lookup.table.shape[-1] - 1)
        time_lo = torch.floor(fine).long().clamp(0, lookup.table.shape[-1] - 1)
        time_hi = (time_lo + 1).clamp(max=lookup.table.shape[-1] - 1)
        time_weight = fine - time_lo.to(fine.dtype)
        lo0 = lookup.table[sigma_lo[:, None], time_lo]
        lo1 = lookup.table[sigma_lo[:, None], time_hi]
        hi0 = lookup.table[sigma_hi[:, None], time_lo]
        hi1 = lookup.table[sigma_hi[:, None], time_hi]
        pulse_lo = lo0 + time_weight * (lo1 - lo0)
        pulse_hi = hi0 + time_weight * (hi1 - hi0)
        pulse = pulse_lo + sigma_weight[:, None] * (pulse_hi - pulse_lo)
        pulse = torch.where(valid, pulse, torch.zeros_like(pulse))
    elif lookup.backend == "cache-1x-phase":
        integer = torch.floor(delta_flat).long()
        phase = delta_flat - integer.to(delta_flat.dtype)
        phase_coordinate = phase * lookup.phase_bins
        phase_lo = torch.floor(phase_coordinate).long().clamp(0, lookup.phase_bins - 1)
        phase_hi = phase_lo + 1
        phase_weight = phase_coordinate - phase_lo.to(phase_coordinate.dtype)
        relative = taps.long()[None, :] - integer[:, None] - lookup.relative_start
        valid = (relative >= 0) & (relative < S_TAPS)
        relative = relative.clamp(0, S_TAPS - 1)

        def phase_sample(sigma_index: torch.Tensor) -> torch.Tensor:
            lower = lookup.table[sigma_index[:, None], phase_lo[:, None], relative]
            upper = lookup.table[sigma_index[:, None], phase_hi[:, None], relative]
            return lower + phase_weight[:, None] * (upper - lower)

        pulse_lo = phase_sample(sigma_lo)
        pulse_hi = phase_sample(sigma_hi)
        pulse = pulse_lo + sigma_weight[:, None] * (pulse_hi - pulse_lo)
        pulse = torch.where(valid, pulse, torch.zeros_like(pulse))
    else:
        raise ValueError(f"unknown surfel pulse backend {lookup.backend!r}")
    return pulse.reshape(original_shape + (S_TAPS,))


def _link_endpoints(nodes: torch.Tensor, links) -> tuple:
    tx = torch.as_tensor([a for a, _ in links], dtype=torch.long)
    rx = torch.as_tensor([b for _, b in links], dtype=torch.long)
    return nodes[tx], nodes[rx]  # [L, 3] each


def _carrier_phase(path_len: torch.Tensor) -> torch.Tensor:
    return torch.exp(-2j * torch.pi * FC_HZ / C_AIR * path_len)


def _excess_taps(path_len: torch.Tensor, los_len: torch.Tensor) -> torch.Tensor:
    return (path_len - los_len) / METRES_PER_TAP


def _place(kernel: torch.Tensor, n_taps: torch.Tensor, delta_taps: torch.Tensor,
           peak_taps: float) -> torch.Tensor:
    """Pulse of `kernel` with its peak at tap `delta` for every row."""
    if delta_taps.ndim == 0:
        return sample_kernel(kernel, n_taps - delta_taps + peak_taps)
    offsets = n_taps[None, :] - delta_taps[:, None] + peak_taps
    return sample_kernel(kernel, offsets)


def _smooth_gate(x: torch.Tensor, e1: float, e2: float) -> torch.Tensor:
    """Smooth 0->1 ramp between e1 and e2 (smoothstep)."""
    t = ((x - e1) / (e2 - e1)).clamp(0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _near_node_gate(d1: torch.Tensor, d2: torch.Tensor) -> torch.Tensor:
    """Suppress paths whose specular/quadrature point is degenerate-close to
    a node (image-source singularity): 0 below 0.02 m, 1 above 0.10 m."""
    return _smooth_gate(torch.minimum(d1, d2), NEAR_NODE_E1, NEAR_NODE_E2)


def kernel_peak_taps(kernel: torch.Tensor) -> float:
    return float(torch.argmax(kernel)) / OVERSAMPLE


def render_los(nodes: torch.Tensor, links, kernel: torch.Tensor,
               gamma: float = GAMMA) -> torch.Tensor:
    """LOS term at delay 0 with free-space amplitude and carrier phase."""
    p_tx, p_rx = _link_endpoints(nodes, links)
    los = torch.linalg.vector_norm(p_tx - p_rx, dim=-1)
    alpha = torch.pow(los, -gamma / 2.0) * _carrier_phase(los)
    n = torch.arange(S_TAPS, dtype=nodes.dtype, device=nodes.device)
    peak = kernel_peak_taps(kernel)
    h = alpha[:, None] * _place(kernel, n, torch.zeros_like(los), peak)
    return h  # [L, 64] complex


def render_surfel_slot(scene: SceneTensors, g: int, nodes: torch.Tensor, links,
                       kernel: torch.Tensor, gamma: float = GAMMA,
                       surfel_lookup: SurfelPulseLookup = None) -> tuple:
    """Gaussian-broadened localized scatterer (paper Sec. VI-B, Eq. (15)-(16))."""
    mu = scene.center[g]
    p_tx, p_rx = _link_endpoints(nodes, links)
    v_i = mu - p_tx
    v_j = mu - p_rx
    d_i = torch.linalg.vector_norm(v_i, dim=-1).clamp(min=1e-6)
    d_j = torch.linalg.vector_norm(v_j, dim=-1).clamp(min=1e-6)
    u_i = v_i / d_i[:, None]
    u_j = v_j / d_j[:, None]
    sigma = surfel_covariance(scene, g)
    s = u_i + u_j
    var = torch.einsum("li,ij,lj->l", s, sigma, s) / (C_AIR**2)  # Eq. (16) [s^2]
    # clamp keeps the sqrt gradient finite at the degenerate zero-covariance
    # init (voting candidates start with rot6d = 0 -> Sigma = 0)
    sigma_tau = torch.sqrt(var.clamp(min=1e-24))
    los = torch.linalg.vector_norm(p_tx - p_rx, dim=-1)
    path = d_i + d_j
    delta = _excess_taps(path, los)

    normal = rot6d_to_matrix(scene.rot6d[g])[:, 2]
    refl = 2.0 * (u_i * normal[None, :]).sum(dim=-1, keepdim=True) * normal[None, :] - u_i
    cos_dev = (u_j * refl).sum(dim=-1)
    lobe = torch.exp((cos_dev - 1.0) / DIFFUSE_SPREAD)
    r = scene.roughness[g]
    b_g = (1.0 - r) * lobe + r  # PROVISIONAL v1 form
    alpha = (
        scene.rho[g]
        * _near_node_gate(d_i, d_j)
        * b_g
        * torch.pow((d_i * d_j).clamp(min=1e-12), -gamma / 2.0)
        * _carrier_phase(path)
    )

    if surfel_lookup is None:
        n = torch.arange(S_TAPS, dtype=nodes.dtype, device=nodes.device)
        peak = kernel_peak_taps(kernel)
        kc = _gauss_broadened_batch(kernel, sigma_tau)  # [L, K]
        pulse = _place(kc, n, delta, peak)
    else:
        pulse = sample_surfel_pulse_lookup(surfel_lookup, sigma_tau, delta)
    h = alpha[:, None] * pulse
    return h, mu


def render_surfel_slots_compact(scene: SceneTensors, slots, nodes: torch.Tensor,
                                links, kernel: torch.Tensor, gamma: float = GAMMA,
                                surfel_lookup: SurfelPulseLookup = None) -> tuple:
    """Evaluate only selected surfel slots jointly -> [G,L,64], [G,3]."""
    index = torch.as_tensor(slots, dtype=torch.long, device=scene.center.device)
    mu = scene.center.index_select(0, index)
    p_tx, p_rx = _link_endpoints(nodes, links)
    v_i = mu[:, None, :] - p_tx[None, :, :]
    v_j = mu[:, None, :] - p_rx[None, :, :]
    d_i = torch.linalg.vector_norm(v_i, dim=-1).clamp(min=1e-6)
    d_j = torch.linalg.vector_norm(v_j, dim=-1).clamp(min=1e-6)
    u_i = v_i / d_i[..., None]
    u_j = v_j / d_j[..., None]

    rotation = rot6d_to_matrix(scene.rot6d.index_select(0, index))
    scale2 = torch.exp(2.0 * scene.scale_log.index_select(0, index).clamp(
        min=SCALE_LOG_MIN))
    covariance = (rotation * scale2[:, None, :]) @ rotation.transpose(-1, -2)
    direction = u_i + u_j
    variance = torch.einsum(
        "gli,gij,glj->gl", direction, covariance, direction) / (C_AIR**2)
    sigma_tau = torch.sqrt(variance.clamp(min=1e-24))
    los = torch.linalg.vector_norm(p_tx - p_rx, dim=-1)
    path = d_i + d_j
    delta = _excess_taps(path, los[None, :])

    normal = rotation[..., :, 2]
    reflection = 2.0 * (u_i * normal[:, None, :]).sum(
        dim=-1, keepdim=True) * normal[:, None, :] - u_i
    cos_deviation = (u_j * reflection).sum(dim=-1)
    lobe = torch.exp((cos_deviation - 1.0) / DIFFUSE_SPREAD)
    roughness = scene.roughness.index_select(0, index)[:, None]
    scattering = (1.0 - roughness) * lobe + roughness
    rho = scene.rho.index_select(0, index)[:, None]
    alpha = (
        rho
        * _near_node_gate(d_i, d_j)
        * scattering
        * torch.pow((d_i * d_j).clamp(min=1e-12), -gamma / 2.0)
        * _carrier_phase(path)
    )
    if surfel_lookup is None:
        raise ValueError("compact surfel slots require a pulse lookup backend")
    pulse = sample_surfel_pulse_lookup(surfel_lookup, sigma_tau, delta)
    return alpha[..., None] * pulse, mu


def _gauss_broadened_batch(kernel: torch.Tensor, sigma_tau_sec: torch.Tensor,
                           step_ns: float = TS_NS / OVERSAMPLE) -> torch.Tensor:
    """Kernel convolved with per-link analytic Gaussians of std sigma_tau
    (Eq. (16)), batched over links: [L] sigma -> [L, K] kernels."""
    sigma_fine = sigma_tau_sec / (step_ns * 1e-9)
    sigma_fine = sigma_fine.clamp(min=1e-3)
    m = int(torch.ceil(4.0 * sigma_fine.max()).item())
    idx = torch.arange(-m, m + 1, dtype=kernel.dtype, device=kernel.device)
    g = torch.exp(-(idx[None, :] ** 2) / (2.0 * sigma_fine[:, None] ** 2))
    g = g / g.sum(dim=-1, keepdim=True)
    xp = F.pad(kernel[None, None, :], (m, m))
    kc = F.conv1d(xp, g[:, None, :])  # [1, L, K]; one Gaussian per channel
    return kc.squeeze(0)


def _gauss_broadened_scene_batch(kernel: torch.Tensor, sigma_tau_sec: torch.Tensor,
                                  step_ns: float = TS_NS / OVERSAMPLE) -> torch.Tensor:
    """Broaden [B,G,L] paths while matching each scalar slot's truncation."""
    sigma_fine = (sigma_tau_sec / (step_ns * 1e-9)).clamp(min=1e-3)
    slot_radius = torch.ceil(4.0 * sigma_fine.max(dim=-1).values).long()
    max_radius = int(slot_radius.max().item())
    idx = torch.arange(-max_radius, max_radius + 1, dtype=kernel.dtype,
                       device=kernel.device)
    flat_sigma = sigma_fine.reshape(-1)
    flat_radius = slot_radius[..., None].expand_as(sigma_fine).reshape(-1)
    gaussian = torch.exp(-(idx[None, :] ** 2) / (2.0 * flat_sigma[:, None] ** 2))
    gaussian = torch.where(idx.abs()[None, :] <= flat_radius[:, None], gaussian,
                           torch.zeros_like(gaussian))
    gaussian = gaussian / gaussian.sum(dim=-1, keepdim=True)
    padded = F.pad(kernel[None, None, :], (max_radius, max_radius))
    return F.conv1d(padded, gaussian[:, None, :]).squeeze(0)


def _gauss_broadened(kernel: torch.Tensor, sigma_tau_sec: torch.Tensor,
                     step_ns: float = TS_NS / OVERSAMPLE) -> torch.Tensor:
    """Single-sigma variant (scalar sigma -> 1-D kernel)."""
    return _gauss_broadened_batch(kernel, sigma_tau_sec.reshape(1), step_ns).squeeze(0)


def render_plane_slot(scene: SceneTensors, g: int, nodes: torch.Tensor, links,
                      kernel: torch.Tensor, gamma: float = GAMMA) -> tuple:
    """Image-source plane reflection (Eq. (17)-(19))."""
    c = scene.center[g]
    normal, tangent, half = plane_axes(scene, g)
    p_tx, p_rx = _link_endpoints(nodes, links)
    rel = p_tx - c
    p_mir = p_tx - 2.0 * normal * (rel @ normal)[:, None]  # Eq. (17)
    den = (p_mir - p_rx) @ normal  # [L]
    num = (c - p_rx) @ normal  # [L]
    sign_safe = torch.where(den >= 0, torch.ones_like(den), -torch.ones_like(den))
    den_safe = torch.where(den.abs() > DEN_EPS, den, sign_safe * DEN_EPS)
    lam = num / den_safe  # Eq. (18)
    x = p_rx + lam[:, None] * (p_mir - p_rx)  # specular point [L, 3]
    v_den = _smooth_gate(den.abs(), DEN_EPS, 2.0 * DEN_EPS)  # guarded denominator
    rel_x = x - c
    v_patch = torch.sigmoid((half[0] - (rel_x @ tangent[:, 0]).abs()) / EPS_V) * \
        torch.sigmoid((half[1] - (rel_x @ tangent[:, 1]).abs()) / EPS_V)  # Eq. (19)

    d1 = torch.linalg.vector_norm(p_tx - x, dim=-1)
    d2 = torch.linalg.vector_norm(x - p_rx, dim=-1)
    path = d1 + d2
    los = torch.linalg.vector_norm(p_tx - p_rx, dim=-1)
    delta = _excess_taps(path, los)

    incidence = ((p_tx - x) * normal[None, :]).sum(dim=-1).abs() / d1.clamp(min=1e-12)
    alpha = (
        scene.rho[g]
        * v_den
        * v_patch
        * _near_node_gate(d1, d2)
        * incidence
        * torch.pow((d1 * d2).clamp(min=1e-12), -gamma / 2.0)
        * _carrier_phase(path)
    )
    n = torch.arange(S_TAPS, dtype=nodes.dtype, device=nodes.device)
    peak = kernel_peak_taps(kernel)
    h = alpha[:, None] * _place(kernel, n, delta, peak)
    return h, x


def _capsule_quadrature(scene: SceneTensors, g: int) -> tuple:
    """Deterministic surface quadrature in world frame: points [K,3], normals [K,3]."""
    r = rot6d_to_matrix(scene.rot6d[g])
    axis, half_len, radius = capsule_axes(scene, g)
    z_bar = torch.tensor([-1.0, 0.0, 1.0], dtype=scene.center.dtype, device=scene.center.device)
    phi = torch.tensor([0.0, 0.5 * torch.pi, torch.pi, 1.5 * torch.pi],
                       dtype=scene.center.dtype, device=scene.center.device)
    zz, pp = torch.meshgrid(z_bar, phi, indexing="ij")
    zz = zz.reshape(-1)
    pp = pp.reshape(-1)
    local = torch.stack([radius * torch.cos(pp), radius * torch.sin(pp), half_len * zz], dim=-1)
    local_n = torch.stack([torch.cos(pp), torch.sin(pp), torch.zeros_like(pp)], dim=-1)
    pts = scene.center[g] + (r @ local.T).T  # [K, 3]
    nrm = (r @ local_n.T).T
    return pts, nrm


def render_capsule_slot(scene: SceneTensors, g: int, nodes: torch.Tensor, links,
                        kernel: torch.Tensor, gamma: float = GAMMA) -> torch.Tensor:
    """Capsule as K_c weighted localized scatterers (paper Sec. VI-D)."""
    pts, nrm = _capsule_quadrature(scene, g)
    p_tx, p_rx = _link_endpoints(nodes, links)
    los = torch.linalg.vector_norm(p_tx - p_rx, dim=-1)
    n = torch.arange(S_TAPS, dtype=nodes.dtype, device=nodes.device)
    peak = kernel_peak_taps(kernel)
    h = torch.zeros(len(links), S_TAPS,
                    dtype=torch.complex128 if nodes.dtype == torch.float64 else torch.complex64,
                    device=nodes.device)
    weight = 1.0 / CAPSULE_K
    for p, nrm_p in zip(pts, nrm):
        v_i = p - p_tx
        v_j = p - p_rx
        d_i = torch.linalg.vector_norm(v_i, dim=-1).clamp(min=1e-6)
        d_j = torch.linalg.vector_norm(v_j, dim=-1).clamp(min=1e-6)
        u_i = v_i / d_i[:, None]
        u_j = v_j / d_j[:, None]
        w = torch.sigmoid(INC_SLOPE * (u_i * nrm_p).sum(dim=-1)) * \
            torch.sigmoid(INC_SLOPE * (u_j * nrm_p).sum(dim=-1))
        path = d_i + d_j
        alpha = (
            scene.rho[g]
            * weight
            * w
            * _near_node_gate(d_i, d_j)
            * torch.pow((d_i * d_j).clamp(min=1e-12), -gamma / 2.0)
            * _carrier_phase(path)
        )
        delta = _excess_taps(path, los)
        h += alpha[:, None] * _place(kernel, n, delta, peak)
    return h


def chord_attenuation(a: torch.Tensor, b: torch.Tensor, scene: SceneTensors,
                      g: int) -> torch.Tensor:
    """Smooth multiplicative ray-capsule chord attenuation for segments a->b.

    `a`, `b` broadcast to [..., 3]; returns [...] in (0, 1], 1 when the
    segment misses the capsule.
    """
    c = scene.center[g]
    axis, half_len, radius = capsule_axes(scene, g)
    seg = b - a
    length = torch.linalg.vector_norm(seg, dim=-1)
    unit = seg / length.clamp(min=1e-12)[..., None]
    ts = (torch.arange(CAPSULE_QUAD, dtype=a.dtype, device=a.device) + 0.5) / CAPSULE_QUAD
    pts = a[..., None, :] + unit[..., None, :] * (ts * length[..., None])[..., None]
    rel = pts - c
    along = (rel * axis).sum(dim=-1)
    along_c = along.clamp(-half_len, half_len)
    rho = torch.linalg.vector_norm(rel - along_c[..., None] * axis, dim=-1)
    inside = torch.sigmoid((radius - rho) * (8.0 / radius.clamp(min=1e-3)))
    chord = (inside * (length / CAPSULE_QUAD)[..., None]).sum(dim=-1)
    return torch.exp(-CAPSULE_CHORD_KAPPA * chord)


def _compact_capsule_geometry(scene: SceneTensors) -> CapsuleGeometry | None:
    """Gather only capsule-typed slots into a padded [B,C,...] representation."""
    scalar = scene.type_id.ndim == 1
    type_id = scene.type_id[None, :] if scalar else scene.type_id
    mask = type_id == CAPSULE
    max_capsules = int(mask.sum(dim=-1).max().item())
    if max_capsules == 0:
        return None
    order = torch.argsort(mask.to(torch.int8), dim=-1, descending=True)[:, :max_capsules]
    valid = torch.gather(mask, 1, order)

    def batched(value: torch.Tensor) -> torch.Tensor:
        return value[None, ...] if scalar else value

    def gather(value: torch.Tensor) -> torch.Tensor:
        value = batched(value)
        index = order.to(value.device)
        index = index[(...,) + (None,) * (value.ndim - 2)].expand(
            value.shape[0], max_capsules, *value.shape[2:])
        return torch.gather(value, 1, index)

    center = gather(scene.center)
    rotation = rot6d_to_matrix(gather(scene.rot6d))
    scale = torch.exp(gather(scene.scale_log).clamp(min=SCALE_LOG_MIN))
    return CapsuleGeometry(
        center=center,
        axis=rotation[..., :, 2],
        half_len=scale[..., 0],
        radius=scale[..., 1],
        valid=valid.to(center.device),
    )


def _capsule_attenuation_compact(a: torch.Tensor, b: torch.Tensor,
                                 geometry: CapsuleGeometry | None,
                                 backend: str = "compact") -> torch.Tensor:
    """Combined attenuation of [B,T,3] segments by compacted capsules."""
    if geometry is None:
        return torch.ones(a.shape[:2], dtype=a.dtype, device=a.device)
    seg = b - a
    length = torch.linalg.vector_norm(seg, dim=-1)
    center = geometry.center[:, None, :, :]
    axis = geometry.axis[:, None, :, :]
    half_len = geometry.half_len[:, None, :]
    radius = geometry.radius[:, None, :]

    if backend == "compact":
        ts = (torch.arange(CAPSULE_QUAD, dtype=a.dtype, device=a.device) + 0.5) / \
            CAPSULE_QUAD
        points = a[:, :, None, None, :] + seg[:, :, None, None, :] * \
            ts[None, None, None, :, None]
        relative = points - center[:, :, :, None, :]
        along = (relative * axis[:, :, :, None, :]).sum(dim=-1)
        along_c = torch.maximum(torch.minimum(along, half_len[..., None]),
                                -half_len[..., None])
        radial = torch.linalg.vector_norm(
            relative - along_c[..., None] * axis[:, :, :, None, :], dim=-1)
        inside = torch.sigmoid(
            (radius[..., None] - radial)
            * (8.0 / radius.clamp(min=1e-3)[..., None]))
        chord = inside.sum(dim=-1) * (length[:, :, None] / CAPSULE_QUAD)
    elif backend == "gaussian":
        relative = a[:, :, None, :] - center
        axial_scale = half_len + radius
        radius2 = radius.clamp(min=1e-3).square()
        axial2 = axial_scale.clamp(min=1e-3).square()

        def metric(left: torch.Tensor, right: torch.Tensor) -> torch.Tensor:
            left_axial = (left * axis).sum(dim=-1)
            right_axial = (right * axis).sum(dim=-1)
            dot = (left * right).sum(dim=-1)
            radial_dot = dot - left_axial * right_axial
            return radial_dot / radius2 + left_axial * right_axial / axial2

        direction = seg[:, :, None, :]
        aa = metric(direction, direction).clamp(min=1e-12)
        bb = metric(direction, relative)
        cc = metric(relative, relative)
        closest = -bb / aa
        q_min = (cc - bb.square() / aa).clamp(min=0.0)
        root = torch.sqrt(aa / 2.0)
        erf_span = torch.erf(root * (1.0 - closest)) - torch.erf(root * (-closest))
        chord = length[:, :, None] * torch.exp(-0.5 * q_min) / torch.sqrt(aa) * erf_span
        chord = chord.clamp(min=0.0)
    else:
        raise ValueError(f"unknown capsule attenuation backend {backend!r}")

    factor = torch.exp(-CAPSULE_CHORD_KAPPA * chord)
    factor = torch.where(geometry.valid[:, None, :], factor, torch.ones_like(factor))
    return factor.prod(dim=-1)


def _batched_link_endpoints(nodes: torch.Tensor, links) -> tuple:
    tx = torch.as_tensor([a for a, _ in links], dtype=torch.long, device=nodes.device)
    rx = torch.as_tensor([b for _, b in links], dtype=torch.long, device=nodes.device)
    return nodes[:, tx], nodes[:, rx]  # [B, L, 3] each


def _chord_attenuation_batched(a: torch.Tensor, b: torch.Tensor,
                               scene: SceneTensors) -> torch.Tensor:
    """Attenuation of segments [B,T,3] by every scene slot -> [B,T,G]."""
    rotation = rot6d_to_matrix(scene.rot6d)
    axis = rotation[..., :, 2]
    scale = torch.exp(scene.scale_log.clamp(min=SCALE_LOG_MIN))
    half_len = scale[..., 0]
    radius = scale[..., 1]

    seg = b - a
    length = torch.linalg.vector_norm(seg, dim=-1)
    unit = seg / length.clamp(min=1e-12)[..., None]
    ts = (torch.arange(CAPSULE_QUAD, dtype=a.dtype, device=a.device) + 0.5) / CAPSULE_QUAD
    pts = a[:, :, None, None, :] + unit[:, :, None, None, :] * (
        ts[None, None, None, :, None] * length[:, :, None, None, None])
    rel = pts - scene.center[:, None, :, None, :]
    along = (rel * axis[:, None, :, None, :]).sum(dim=-1)
    along_c = torch.maximum(torch.minimum(along, half_len[:, None, :, None]),
                            -half_len[:, None, :, None])
    radial = torch.linalg.vector_norm(
        rel - along_c[..., None] * axis[:, None, :, None, :], dim=-1)
    inside = torch.sigmoid(
        (radius[:, None, :, None] - radial)
        * (8.0 / radius.clamp(min=1e-3)[:, None, :, None]))
    chord = (inside * (length[:, :, None, None] / CAPSULE_QUAD)).sum(dim=-1)
    return torch.exp(-CAPSULE_CHORD_KAPPA * chord)


def render_scene_batched(scene: SceneTensors, nodes: torch.Tensor, kernel: torch.Tensor,
                         nuis_gain: torch.Tensor = None,
                         nuis_phase: torch.Tensor = None,
                         gamma: float = GAMMA,
                         surfel_lookup: SurfelPulseLookup = None,
                         capsule_attenuation_backend: str = "legacy",
                         links=None) -> torch.Tensor:
    """Vectorized scene assembly -> complex CIR [B,L,S_TAPS].

    Scene fields carry a leading batch dimension, for example `center` is
    [B,G,3]. All three primitive formulas are evaluated for every slot and
    selected with `type_id`; the scalar `render_scene` remains the reference
    and fallback path.
    """
    if nodes.ndim != 3 or scene.center.ndim != 3:
        raise ValueError("batched renderer expects nodes [B,N,3] and scene fields [B,G,...]")
    bsz, slots = scene.type_id.shape
    links = directed_links(nodes.shape[1]) if links is None else links
    p_tx, p_rx = _batched_link_endpoints(nodes, links)
    link_count = len(links)
    los = torch.linalg.vector_norm(p_tx - p_rx, dim=-1)
    n = torch.arange(S_TAPS, dtype=nodes.dtype, device=nodes.device)
    peak = kernel_peak_taps(kernel)

    los_alpha = torch.pow(los, -gamma / 2.0) * _carrier_phase(los)
    h = los_alpha[..., None] * sample_kernel(kernel, n[None, None, :] + peak)

    capsule_mask = scene.type_id == CAPSULE
    capsule_geometry = None
    if capsule_attenuation_backend == "legacy":
        los_capsule_att = _chord_attenuation_batched(p_tx, p_rx, scene)
        los_capsule_att = torch.where(capsule_mask[:, None, :], los_capsule_att,
                                      torch.ones_like(los_capsule_att)).prod(dim=-1)
    else:
        capsule_geometry = _compact_capsule_geometry(scene)
        los_capsule_att = _capsule_attenuation_compact(
            p_tx, p_rx, capsule_geometry, capsule_attenuation_backend)
    h = h * los_capsule_att[..., None]

    rotation = rot6d_to_matrix(scene.rot6d)  # [B,G,3,3]
    center = scene.center
    ptx = p_tx[:, None, :, :]
    prx = p_rx[:, None, :, :]
    los_g = los[:, None, :]

    # Plane paths [B,G,L,S].
    normal = rotation[..., :, 2]
    tangent = rotation[..., :, :2]
    half = torch.exp(scene.scale_log[..., :2].clamp(min=SCALE_LOG_MIN))
    rel = ptx - center[:, :, None, :]
    p_mir = ptx - 2.0 * normal[:, :, None, :] * (
        rel * normal[:, :, None, :]).sum(dim=-1, keepdim=True)
    den = ((p_mir - prx) * normal[:, :, None, :]).sum(dim=-1)
    num = ((center[:, :, None, :] - prx) * normal[:, :, None, :]).sum(dim=-1)
    sign_safe = torch.where(den >= 0, torch.ones_like(den), -torch.ones_like(den))
    den_safe = torch.where(den.abs() > DEN_EPS, den, sign_safe * DEN_EPS)
    lam = num / den_safe
    specular = prx + lam[..., None] * (p_mir - prx)
    rel_x = specular - center[:, :, None, :]
    patch_u = (rel_x * tangent[..., 0][:, :, None, :]).sum(dim=-1).abs()
    patch_v = (rel_x * tangent[..., 1][:, :, None, :]).sum(dim=-1).abs()
    visibility = _smooth_gate(den.abs(), DEN_EPS, 2.0 * DEN_EPS)
    visibility = visibility * torch.sigmoid((half[..., 0, None] - patch_u) / EPS_V)
    visibility = visibility * torch.sigmoid((half[..., 1, None] - patch_v) / EPS_V)
    plane_d1 = torch.linalg.vector_norm(ptx - specular, dim=-1)
    plane_d2 = torch.linalg.vector_norm(specular - prx, dim=-1)
    plane_path = plane_d1 + plane_d2
    incidence = ((ptx - specular) * normal[:, :, None, :]).sum(dim=-1).abs()
    incidence = incidence / plane_d1.clamp(min=1e-12)
    plane_alpha = scene.rho[..., None] * visibility * _near_node_gate(plane_d1, plane_d2)
    plane_alpha = plane_alpha * incidence * torch.pow(
        (plane_d1 * plane_d2).clamp(min=1e-12), -gamma / 2.0)
    plane_alpha = plane_alpha * _carrier_phase(plane_path)
    plane_delta = _excess_taps(plane_path, los_g)
    plane_h = plane_alpha[..., None] * sample_kernel(
        kernel, n[None, None, None, :] - plane_delta[..., None] + peak)

    # Surfel paths [B,G,L,S].
    vi = center[:, :, None, :] - ptx
    vj = center[:, :, None, :] - prx
    surfel_di = torch.linalg.vector_norm(vi, dim=-1).clamp(min=1e-6)
    surfel_dj = torch.linalg.vector_norm(vj, dim=-1).clamp(min=1e-6)
    ui = vi / surfel_di[..., None]
    uj = vj / surfel_dj[..., None]
    scale2 = torch.exp(2.0 * scene.scale_log.clamp(min=SCALE_LOG_MIN))
    sigma = rotation @ torch.diag_embed(scale2) @ rotation.transpose(-1, -2)
    direction_sum = ui + uj
    var = torch.einsum("bgli,bgij,bglj->bgl", direction_sum, sigma,
                       direction_sum) / (C_AIR**2)
    sigma_tau = torch.sqrt(var.clamp(min=1e-24))
    surfel_path = surfel_di + surfel_dj
    surfel_delta = _excess_taps(surfel_path, los_g)
    reflected = 2.0 * (ui * normal[:, :, None, :]).sum(
        dim=-1, keepdim=True) * normal[:, :, None, :] - ui
    cos_dev = (uj * reflected).sum(dim=-1)
    lobe = torch.exp((cos_dev - 1.0) / DIFFUSE_SPREAD)
    roughness = scene.roughness[..., None]
    bounce = (1.0 - roughness) * lobe + roughness
    surfel_alpha = scene.rho[..., None] * _near_node_gate(surfel_di, surfel_dj) * bounce
    surfel_alpha = surfel_alpha * torch.pow(
        (surfel_di * surfel_dj).clamp(min=1e-12), -gamma / 2.0)
    surfel_alpha = surfel_alpha * _carrier_phase(surfel_path)
    if surfel_lookup is None:
        broadened = _gauss_broadened_scene_batch(kernel, sigma_tau)
        surfel_offsets = n[None, :] - surfel_delta.reshape(-1, 1) + peak
        surfel_pulse = sample_kernel(broadened, surfel_offsets).reshape(
            bsz, slots, link_count, S_TAPS)
    else:
        surfel_pulse = sample_surfel_pulse_lookup(
            surfel_lookup, sigma_tau, surfel_delta)
    surfel_h = surfel_alpha[..., None] * surfel_pulse

    # Capsule quadrature paths [B,G,L,K,S], reduced over K.
    z_bar = torch.tensor([-1.0, 0.0, 1.0], dtype=nodes.dtype, device=nodes.device)
    phi = torch.tensor([0.0, 0.5 * torch.pi, torch.pi, 1.5 * torch.pi],
                       dtype=nodes.dtype, device=nodes.device)
    zz, pp = torch.meshgrid(z_bar, phi, indexing="ij")
    zz, pp = zz.reshape(-1), pp.reshape(-1)
    scale = torch.exp(scene.scale_log.clamp(min=SCALE_LOG_MIN))
    radius = scale[..., 1, None]
    half_len = scale[..., 0, None]
    local = torch.stack([radius * torch.cos(pp), radius * torch.sin(pp),
                         half_len * zz], dim=-1)
    local_n = torch.stack([
        torch.cos(pp).expand(bsz, slots, -1),
        torch.sin(pp).expand(bsz, slots, -1),
        torch.zeros(bsz, slots, CAPSULE_K, dtype=nodes.dtype, device=nodes.device),
    ], dim=-1)
    points = center[:, :, None, :] + torch.einsum("bgij,bgkj->bgki", rotation, local)
    quad_normal = torch.einsum("bgij,bgkj->bgki", rotation, local_n)
    cap_vi = points[:, :, None, :, :] - ptx[:, :, :, None, :]
    cap_vj = points[:, :, None, :, :] - prx[:, :, :, None, :]
    cap_di = torch.linalg.vector_norm(cap_vi, dim=-1).clamp(min=1e-6)
    cap_dj = torch.linalg.vector_norm(cap_vj, dim=-1).clamp(min=1e-6)
    cap_ui = cap_vi / cap_di[..., None]
    cap_uj = cap_vj / cap_dj[..., None]
    quad_n = quad_normal[:, :, None, :, :]
    quad_weight = torch.sigmoid(INC_SLOPE * (cap_ui * quad_n).sum(dim=-1))
    quad_weight = quad_weight * torch.sigmoid(INC_SLOPE * (cap_uj * quad_n).sum(dim=-1))
    cap_path = cap_di + cap_dj
    cap_alpha = scene.rho[..., None, None] * (quad_weight / CAPSULE_K)
    cap_alpha = cap_alpha * _near_node_gate(cap_di, cap_dj) * torch.pow(
        (cap_di * cap_dj).clamp(min=1e-12), -gamma / 2.0)
    cap_alpha = cap_alpha * _carrier_phase(cap_path)
    cap_delta = _excess_taps(cap_path, los_g[..., None])
    capsule_h = (cap_alpha[..., None] * sample_kernel(
        kernel, n[None, None, None, None, :] - cap_delta[..., None] + peak)).sum(dim=-2)

    # Plane and surfel paths are attenuated by every slot typed as a capsule.
    def path_attenuation(midpoint: torch.Tensor) -> torch.Tensor:
        flat_mid = midpoint.reshape(bsz, slots * link_count, 3)
        flat_tx = ptx.expand(-1, slots, -1, -1).reshape(bsz, slots * link_count, 3)
        flat_rx = prx.expand(-1, slots, -1, -1).reshape(bsz, slots * link_count, 3)
        if capsule_attenuation_backend == "legacy":
            first = _chord_attenuation_batched(flat_tx, flat_mid, scene)
            second = _chord_attenuation_batched(flat_mid, flat_rx, scene)
            attenuation = torch.where(capsule_mask[:, None, :], first * second,
                                      torch.ones_like(first)).prod(dim=-1)
        else:
            attenuation = _capsule_attenuation_compact(
                flat_tx, flat_mid, capsule_geometry, capsule_attenuation_backend)
            attenuation = attenuation * _capsule_attenuation_compact(
                flat_mid, flat_rx, capsule_geometry, capsule_attenuation_backend)
        return attenuation.reshape(bsz, slots, link_count)

    plane_h = plane_h * path_attenuation(specular)[..., None]
    surfel_midpoint = center[:, :, None, :].expand(-1, -1, link_count, -1)
    surfel_h = surfel_h * path_attenuation(surfel_midpoint)[..., None]

    slot_h = torch.where((scene.type_id == PLANE)[..., None, None], plane_h,
                         torch.where((scene.type_id == SURFEL)[..., None, None], surfel_h,
                                     torch.where((scene.type_id == CAPSULE)[..., None, None],
                                                 capsule_h, torch.zeros_like(capsule_h))))
    h = h + (scene.presence[..., None, None] * slot_h).sum(dim=1)
    if nuis_gain is not None:
        h = h * nuis_gain[..., None]
    if nuis_phase is not None:
        h = h * torch.exp(1j * nuis_phase)[..., None]
    return h


def render_scene(scene: SceneTensors, nodes: torch.Tensor, kernel: torch.Tensor,
                 nuis_gain: torch.Tensor = None, nuis_phase: torch.Tensor = None,
                 noise_std: float = 0.0, gamma: float = GAMMA,
                 noise_seed: int = 0,
                 surfel_lookup: SurfelPulseLookup = None,
                  capsule_attenuation_backend: str = "legacy",
                  compact_surfel_slots: bool = False,
                  skip_zero_presence: bool = True,
                 links=None) -> torch.Tensor:
    """Assemble Eq. (21): all slots -> complex CIR [L, S_TAPS].

    `noise_std` is a scalar or broadcastable to [L]; the per-link complex
    AWGN is reproducible for a given `noise_seed` (CPU).
    """
    links = directed_links(nodes.shape[0]) if links is None else links
    p_tx, p_rx = _link_endpoints(nodes, links)
    los = torch.linalg.vector_norm(p_tx - p_rx, dim=-1)
    h = render_los(nodes, links, kernel, gamma=gamma)

    capsule_slots = [g for g in range(scene.type_id.numel()) if scene.type_id[g] == CAPSULE]
    capsule_geometry = None
    if capsule_attenuation_backend == "legacy":
        for g in capsule_slots:
            att = chord_attenuation(p_tx, p_rx, scene, g)
            h = h * att[:, None]
    else:
        capsule_geometry = _compact_capsule_geometry(scene)
        att = _capsule_attenuation_compact(
            p_tx[None], p_rx[None], capsule_geometry,
            capsule_attenuation_backend).squeeze(0)
        h = h * att[:, None]

    def path_attenuation(midpoint: torch.Tensor) -> torch.Tensor:
        if capsule_attenuation_backend == "legacy":
            attenuation = torch.ones_like(los)
            for gc in capsule_slots:
                attenuation = attenuation * chord_attenuation(p_tx, midpoint, scene, gc) * \
                    chord_attenuation(midpoint, p_rx, scene, gc)
            return attenuation
        first = _capsule_attenuation_compact(
            p_tx[None], midpoint[None], capsule_geometry,
            capsule_attenuation_backend).squeeze(0)
        second = _capsule_attenuation_compact(
            midpoint[None], p_rx[None], capsule_geometry,
            capsule_attenuation_backend).squeeze(0)
        return first * second

    surfel_slots = [g for g in range(scene.type_id.numel()) if scene.type_id[g] == SURFEL]
    if compact_surfel_slots and surfel_slots:
        surfel_h, surfel_midpoints = render_surfel_slots_compact(
            scene, surfel_slots, nodes, links, kernel, gamma, surfel_lookup)
        midpoint = surfel_midpoints[:, None, :].expand(-1, len(links), -1)
        if capsule_attenuation_backend == "legacy":
            attenuation = torch.ones(
                len(surfel_slots), len(links), dtype=nodes.dtype, device=nodes.device)
            for gc in capsule_slots:
                attenuation = attenuation * chord_attenuation(
                    p_tx[None], midpoint, scene, gc) * chord_attenuation(
                    midpoint, p_rx[None], scene, gc)
        else:
            flat_midpoint = midpoint.reshape(1, -1, 3)
            flat_tx = p_tx[None, None, :, :].expand(
                1, len(surfel_slots), -1, -1).reshape(1, -1, 3)
            flat_rx = p_rx[None, None, :, :].expand_as(
                p_tx[None, None, :, :].expand(
                    1, len(surfel_slots), -1, -1)).reshape(1, -1, 3)
            attenuation = (_capsule_attenuation_compact(
                flat_tx, flat_midpoint, capsule_geometry,
                capsule_attenuation_backend) * _capsule_attenuation_compact(
                    flat_midpoint, flat_rx, capsule_geometry,
                    capsule_attenuation_backend)).reshape(len(surfel_slots), -1)
        surfel_index = torch.as_tensor(
            surfel_slots, dtype=torch.long, device=scene.presence.device)
        surfel_presence = scene.presence.index_select(0, surfel_index)
        h = h + (surfel_presence[:, None, None] * attenuation[..., None] * surfel_h).sum(0)

    for g in range(scene.type_id.numel()):
        t = int(scene.type_id[g])
        presence = scene.presence[g]
        if skip_zero_presence and presence == 0:
            continue
        if t == PLANE:
            hg, x = render_plane_slot(scene, g, nodes, links, kernel, gamma=gamma)
            att = path_attenuation(x)
            h = h + presence * att[:, None] * hg
        elif t == SURFEL and not compact_surfel_slots:
            hg, mu = render_surfel_slot(scene, g, nodes, links, kernel, gamma=gamma,
                                        surfel_lookup=surfel_lookup)
            att = path_attenuation(mu.expand_as(p_tx))
            h = h + presence * att[:, None] * hg
        elif t == CAPSULE:
            hg = render_capsule_slot(scene, g, nodes, links, kernel, gamma=gamma)
            h = h + presence * hg

    if nuis_gain is not None:
        h = h * nuis_gain[:, None]
    if nuis_phase is not None:
        h = h * torch.exp(1j * nuis_phase)[:, None]
    if noise_std is not None and float(torch.as_tensor(noise_std).abs().max()) > 0.0:
        rng = torch.Generator(device=nodes.device) if nodes.is_cuda else torch.Generator()
        rng.manual_seed(noise_seed)
        real = torch.randn(h.shape, generator=rng, dtype=nodes.dtype, device=nodes.device)
        imag = torch.randn(h.shape, generator=rng, dtype=nodes.dtype, device=nodes.device)
        std = torch.as_tensor(noise_std, dtype=nodes.dtype, device=nodes.device)
        if std.ndim == 0:
            std_t = std
        else:
            std_t = std.reshape(-1, 1)
        h = h + (std_t / 2.0**0.5) * (real + 1j * imag)
    return h
