"""UWBRender analytic geometry tests (plan Phase 2), float64."""

from __future__ import annotations

import torch

from nrecon.constants import F0_MARKER, METRES_PER_TAP, S_TAPS
from nrecon.seeding import seed_all
from nrecon.sim.delay import fractional_shift
from nrecon.sim.primitives import CAPSULE, PLANE, SURFEL, SceneTensors, rot6d_to_matrix
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.sim.render import render_los, render_scene
from nrecon.constants import directed_links


def _kernel():
    t = make_template_v1()
    return torch.as_tensor(correlation_kernel(t, t).samples)


def _parabolic_peak(env: torch.Tensor) -> float:
    m = int(torch.argmax(env))
    if 0 < m < env.numel() - 1:
        d = env[m - 1] - 2 * env[m] + env[m + 1]
        corr = 0.5 * (env[m - 1] - env[m + 1]) / d if d != 0 else 0.0
        return m + float(corr)
    return float(m)


def _echo(h_total: torch.Tensor, h_los: torch.Tensor, link: int) -> torch.Tensor:
    return (h_total[link] - h_los[link]).abs()


def _only_slot(scene: SceneTensors, g: int) -> SceneTensors:
    keep = torch.zeros(scene.type_id.numel(), dtype=torch.bool)
    keep[g] = True
    return SceneTensors(
        type_id=scene.type_id[keep],
        presence=scene.presence[keep],
        center=scene.center[keep],
        rot6d=scene.rot6d[keep],
        scale_log=scene.scale_log[keep],
        rho=scene.rho[keep],
        roughness=scene.roughness[keep],
        atten=scene.atten[keep],
        dynamic_p=scene.dynamic_p[keep],
    )


def test_point_surfel_peak_delay():
    seed_all(41)
    kernel = _kernel()
    for _ in range(10):
        nodes = torch.rand(2, 3, dtype=torch.float64) * 4.0
        mu = torch.rand(3, dtype=torch.float64) * 4.0
        scene = SceneTensors.empty(1)
        scene.type_id[0] = SURFEL
        scene.presence[0] = 1.0
        scene.center[0] = mu
        scene.scale_log[0] = torch.full((3,), -6.0)  # point-like
        scene.rho[0] = 1.0 + 0.0j
        links = directed_links(2)
        h = render_scene(scene, nodes, kernel)
        h_los = render_los(nodes, links, kernel)
        los = float(torch.linalg.vector_norm(nodes[0] - nodes[1]))
        excess = float(
            torch.linalg.vector_norm(mu - nodes[0]) + torch.linalg.vector_norm(mu - nodes[1]) - los
        )
        if excess / METRES_PER_TAP < 4.0:
            continue
        for li, (a, b) in enumerate(links):
            env = _echo(h, h_los, li)
            peak_tap = _parabolic_peak(env)
            expect = excess / METRES_PER_TAP
            assert abs(peak_tap - expect) < 0.05, (a, b, peak_tap, expect)


def test_plane_path_length_and_specular_point():
    seed_all(42)
    kernel = _kernel()
    nodes = torch.as_tensor([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
    scene = SceneTensors.empty(1)
    scene.type_id[0] = PLANE
    scene.presence[0] = 1.0
    scene.center[0] = torch.as_tensor([1.0, 3.0, 0.0])
    n = torch.as_tensor([0.0, 1.0, 0.0], dtype=torch.float64)
    scene.rot6d[0] = torch.as_tensor([1.0, 0.0, 0.0, 0.0, 0.0, 1.0], dtype=torch.float64)
    scene.scale_log[0, :2] = torch.log(torch.tensor([2.0, 2.0], dtype=torch.float64))
    scene.rho[0] = 0.5 + 0.1j
    links = directed_links(2)
    h = render_scene(scene, nodes, kernel)
    h_los = render_los(nodes, links, kernel)
    p_mir = nodes[0] - 2.0 * n * ((nodes[0] - scene.center[0]) @ n)
    path = float(torch.linalg.vector_norm(p_mir - nodes[1]))
    los = float(torch.linalg.vector_norm(nodes[0] - nodes[1]))
    expect = (path - los) / METRES_PER_TAP
    for li in range(2):
        peak_tap = _parabolic_peak(_echo(h, h_los, li))
        assert abs(peak_tap - expect) < 0.05

    # specular point geometry: lies on the plane; Snell angle equality
    rel = nodes[0] - scene.center[0]
    p_mir_t = nodes[0] - 2.0 * n * (rel @ n)
    den = (p_mir_t - nodes[1]) @ n
    num = (scene.center[0] - nodes[1]) @ n
    x = nodes[1] + (num / den) * (p_mir_t - nodes[1])
    assert abs(float((x - scene.center[0]) @ n)) < 1e-9
    v1 = torch.nn.functional.normalize(nodes[0] - x, dim=0)
    v2 = torch.nn.functional.normalize(nodes[1] - x, dim=0)
    assert abs(float(abs(v1 @ n) - abs(v2 @ n))) < 1e-9


def test_plane_patch_gate_sweeps_smoothly():
    seed_all(43)
    kernel = _kernel()
    nodes = torch.as_tensor([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
    links = directed_links(2)
    h_los = render_los(nodes, links, kernel)
    amps = []
    for s in torch.linspace(-0.5, 0.5, 21):
        scene = SceneTensors.empty(1)
        scene.type_id[0] = PLANE
        scene.presence[0] = 1.0
        scene.center[0] = torch.as_tensor([1.0 + float(s), 3.0, 0.0])
        scene.rot6d[0] = torch.as_tensor([1.0, 0.0, 0.0, 0.0, 0.0, 1.0], dtype=torch.float64)
        scene.scale_log[0, :2] = torch.log(torch.tensor([0.1, 2.0], dtype=torch.float64))
        scene.rho[0] = 1.0
        h = render_scene(scene, nodes, kernel)
        env = _echo(h, h_los, 0)
        amps.append(float(env.max()))
    amps = torch.as_tensor(amps)
    mid = amps[10]
    left = amps[:10]
    right = amps[11:]
    assert mid > 0.5 * amps.max()
    assert torch.all(left[1:] >= left[:-1] - 0.02 * mid)  # rising toward center
    assert torch.all(right[1:] <= right[:-1] + 0.02 * mid)  # falling away from center
    assert amps[0] < 0.05 * mid
    assert amps[-1] < 0.05 * mid
    assert torch.all(torch.abs(torch.diff(amps)) < 0.5 * mid)  # smooth sigmoid ramp


def test_surfel_broadening_matches_equation_16():
    from nrecon.sim.delay import sample_kernel
    from nrecon.sim.primitives import surfel_covariance
    from nrecon.sim.render import _gauss_broadened

    seed_all(44)
    kernel = _kernel()
    nodes = torch.as_tensor([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0]])
    links = directed_links(2)
    scene = SceneTensors.empty(1)
    scene.type_id[0] = SURFEL
    scene.presence[0] = 1.0
    scene.center[0] = torch.as_tensor([2.0, 2.5, 1.0])
    scene.rot6d[0] = torch.as_tensor([1.0, 0.0, 0.0, 0.0, 0.0, 1.0], dtype=torch.float64)
    scene.scale_log[0] = torch.log(torch.tensor([0.2, 0.2, 0.5], dtype=torch.float64))
    scene.rho[0] = 1.0
    scene.roughness[0] = 0.3
    h = render_scene(scene, nodes, kernel)
    h_los = render_los(nodes, links, kernel)
    env = _echo(h, h_los, 0).double() ** 2

    def second_moment(e: torch.Tensor) -> float:
        total = e.sum()
        c = (torch.arange(e.numel(), dtype=torch.float64) * e).sum() / total
        return float(((torch.arange(e.numel(), dtype=torch.float64) - c) ** 2 * e).sum() / total)

    # Eq. (16) exactly as the renderer evaluates it
    mu = scene.center[0]
    u_i = torch.nn.functional.normalize(mu - nodes[0], dim=0)
    u_j = torch.nn.functional.normalize(mu - nodes[1], dim=0)
    sigma = surfel_covariance(scene, 0)
    var = (u_i + u_j) @ sigma @ (u_i + u_j) / 299_702_547.0**2
    sigma_tau = torch.sqrt(var)
    # Reference: analytic kernel convolved with N(0, sigma_tau^2), tap-grid,
    # placed at the same LOS-relative delay as the rendered echo.
    kc = _gauss_broadened(kernel, sigma_tau)
    los = float(torch.linalg.vector_norm(nodes[0] - nodes[1]))
    delta = float(
        (torch.linalg.vector_norm(mu - nodes[0]) + torch.linalg.vector_norm(mu - nodes[1]) - los)
        / METRES_PER_TAP
    )
    n_taps = torch.arange(S_TAPS, dtype=torch.float64)
    k_tap = sample_kernel(kernel, n_taps) ** 2
    kc_tap = sample_kernel(kc, n_taps - delta + 8.0) ** 2
    delta_ref = second_moment(kc_tap) - second_moment(k_tap)
    delta_echo = second_moment(env) - second_moment(k_tap)
    assert abs(delta_echo - delta_ref) < 0.10 * abs(delta_ref), (delta_echo, delta_ref)


def test_capsule_occlusion_monotonic():
    seed_all(45)
    kernel = _kernel()
    nodes = torch.as_tensor([[0.0, 0.0, 0.0], [3.0, 0.0, 0.0]])
    links = directed_links(2)
    vals = []
    for radius in torch.linspace(0.05, 0.4, 8):
        scene = SceneTensors.empty(1)
        scene.type_id[0] = CAPSULE
        scene.presence[0] = 1.0
        scene.center[0] = torch.as_tensor([1.5, 0.0, 0.0])
        scene.rot6d[0] = torch.as_tensor([1.0, 0.0, 0.0, 0.0, 0.0, 1.0], dtype=torch.float64)
        scene.scale_log[0, :2] = torch.log(torch.tensor([0.3, float(radius)], dtype=torch.float64))
        scene.rho[0] = 0.0 + 0.0j  # attenuate only; no own echo
        h = render_scene(scene, nodes, kernel)
        h_al = fractional_shift(h, torch.full((2,), F0_MARKER, dtype=torch.float64))
        vals.append(float(h_al[0, 16].abs()))
    vals = torch.as_tensor(vals)
    assert torch.all(vals[1:] <= vals[:-1] + 1e-9)  # decreasing with radius
    assert vals[0] > vals[-1]


def test_render_permutation_invariant():
    seed_all(46)
    kernel = _kernel()
    nodes = torch.rand(4, 3, dtype=torch.float64) * 3.0
    scene = SceneTensors.empty(3)
    scene.type_id = torch.as_tensor([PLANE, SURFEL, CAPSULE])
    scene.presence = torch.ones(3, dtype=torch.float64)
    scene.center = torch.rand(3, 3, dtype=torch.float64) * 3.0
    scene.rot6d = torch.rand(3, 6, dtype=torch.float64)
    scene.rot6d[0] = torch.as_tensor([1.0, 0.0, 0.0, 0.0, 1.0, 0.0], dtype=torch.float64)
    scene.scale_log = torch.log(torch.rand(3, 3, dtype=torch.float64) * 0.5 + 0.1)
    scene.rho = torch.as_tensor([0.5 + 0.1j, 0.6 - 0.2j, 0.4 + 0.2j])
    perm = torch.as_tensor([2, 0, 1])
    shuffled = SceneTensors(
        type_id=scene.type_id[perm],
        presence=scene.presence[perm],
        center=scene.center[perm],
        rot6d=scene.rot6d[perm],
        scale_log=scene.scale_log[perm],
        rho=scene.rho[perm],
        roughness=scene.roughness[perm],
        atten=scene.atten[perm],
        dynamic_p=scene.dynamic_p[perm],
    )
    h1 = render_scene(scene, nodes, kernel)
    h2 = render_scene(shuffled, nodes, kernel)
    assert torch.allclose(h1, h2, rtol=1e-12, atol=1e-12)


def test_empty_scene_and_presence_zero():
    seed_all(47)
    kernel = _kernel()
    nodes = torch.rand(3, 3, dtype=torch.float64) * 3.0
    links = directed_links(3)
    empty = SceneTensors.empty(1)
    h_empty = render_scene(empty, nodes, kernel)
    h_los = render_los(nodes, links, kernel)
    assert torch.equal(h_empty, h_los)

    scene = SceneTensors.empty(1)
    scene.type_id[0] = SURFEL
    scene.presence[0] = 0.0
    scene.center[0] = torch.rand(3, dtype=torch.float64)
    scene.rho[0] = 1.0 + 0.0j
    h_inactive = render_scene(scene, nodes, kernel)
    assert torch.equal(h_inactive, h_empty)
