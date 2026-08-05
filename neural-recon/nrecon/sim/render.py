"""Differentiable UWBRender forward model (paper Sec. VI).

Pure functions batched over links. LOS is always rendered; scene paths are
LOS-relative delays (Eq. (4), (21)); sparse evaluation only touches the
pulse support around each path delay; nuisance gain, phase, and noise are
applied at assembly (Eq. (21)).
"""

from __future__ import annotations

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
from nrecon.sim.delay import sample_kernel
from nrecon.sim.primitives import (
    CAPSULE,
    PLANE,
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
                       kernel: torch.Tensor, gamma: float = GAMMA) -> tuple:
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

    n = torch.arange(S_TAPS, dtype=nodes.dtype, device=nodes.device)
    peak = kernel_peak_taps(kernel)
    kc = _gauss_broadened_batch(kernel, sigma_tau)  # [L, K]
    h = alpha[:, None] * _place(kc, n, delta, peak)
    return h, mu


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


def render_scene(scene: SceneTensors, nodes: torch.Tensor, kernel: torch.Tensor,
                 nuis_gain: torch.Tensor = None, nuis_phase: torch.Tensor = None,
                 noise_std: float = 0.0, gamma: float = GAMMA,
                 noise_seed: int = 0) -> torch.Tensor:
    """Assemble Eq. (21): all slots -> complex CIR [L, S_TAPS].

    `noise_std` is a scalar or broadcastable to [L]; the per-link complex
    AWGN is reproducible for a given `noise_seed` (CPU).
    """
    links = directed_links(nodes.shape[0])
    p_tx, p_rx = _link_endpoints(nodes, links)
    los = torch.linalg.vector_norm(p_tx - p_rx, dim=-1)
    h = render_los(nodes, links, kernel, gamma=gamma)

    capsule_slots = [g for g in range(scene.type_id.numel()) if scene.type_id[g] == CAPSULE]
    for g in capsule_slots:
        att = chord_attenuation(p_tx, p_rx, scene, g)
        h = h * att[:, None]

    for g in range(scene.type_id.numel()):
        t = int(scene.type_id[g])
        presence = scene.presence[g]
        if presence == 0:
            continue
        if t == PLANE:
            hg, x = render_plane_slot(scene, g, nodes, links, kernel, gamma=gamma)
            att = torch.ones_like(los)
            for gc in capsule_slots:
                att = att * chord_attenuation(p_tx, x, scene, gc) * \
                    chord_attenuation(x, p_rx, scene, gc)
            h = h + presence * att[:, None] * hg
        elif t == SURFEL:
            hg, mu = render_surfel_slot(scene, g, nodes, links, kernel, gamma=gamma)
            att = torch.ones_like(los)
            for gc in capsule_slots:
                att = att * chord_attenuation(p_tx, mu, scene, gc) * \
                    chord_attenuation(mu, p_rx, scene, gc)
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
