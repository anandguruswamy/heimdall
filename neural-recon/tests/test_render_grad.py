"""UWBRender autograd vs finite-difference tests (plan Phase 2), float64."""

from __future__ import annotations

import torch

from nrecon.constants import S_TAPS
from nrecon.seeding import seed_all
from nrecon.sim.primitives import CAPSULE, PLANE, SURFEL, SceneTensors
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.sim.render import render_scene


def _kernel():
    t = make_template_v1()
    return torch.as_tensor(correlation_kernel(t, t).samples)


def _build_scene() -> SceneTensors:
    seed_all(51)
    scene = SceneTensors.empty(3)
    scene.type_id = torch.as_tensor([PLANE, SURFEL, CAPSULE])
    scene.presence = torch.full((3,), 0.9, dtype=torch.float64, requires_grad=True)
    scene.center = torch.tensor(
        [[1.5, 1.2, 0.9], [1.2, 0.9, 0.8], [2.0, 1.0, 0.6]], dtype=torch.float64,
        requires_grad=True,
    )
    scene.rot6d = torch.tensor(
        [[1.0, 0.0, 0.0, 0.0, 0.5, 0.8], [1.0, 0.1, 0.0, 1.0, 0.0, 0.2],
         [0.9, 0.2, 0.1, 1.0, 0.0, 0.3]],
        dtype=torch.float64, requires_grad=True,
    )
    scene.scale_log = torch.log(torch.tensor(
        [[2.0, 1.5, 0.2], [0.4, 0.3, 0.2], [0.5, 0.2, 0.1]], dtype=torch.float64
    )).clone().requires_grad_(True)
    rho = torch.zeros(3, dtype=torch.complex128, requires_grad=True)
    with torch.no_grad():
        rho.real = torch.tensor([0.5, 0.6, 0.4])
        rho.imag = torch.tensor([0.1, -0.2, 0.2])
    scene.rho = rho
    scene.roughness = torch.tensor([0.0, 0.3, 0.0], dtype=torch.float64, requires_grad=True)
    return scene


def _loss(h: torch.Tensor) -> torch.Tensor:
    return 0.5 * (h.conj() * h).real.sum()


def _finite_difference_grad(scene: SceneTensors, nodes: torch.Tensor, kernel: torch.Tensor,
                            param, eps: float = 1e-5) -> torch.Tensor:
    grad = torch.zeros_like(param)
    flat = param.detach().flatten()
    with torch.no_grad():
        for i in range(flat.numel()):
            old = flat[i].item()
            flat[i] = old + eps
            h_p = render_scene(scene, nodes, kernel)
            loss_p = _loss(h_p).item()
            flat[i] = old - eps
            h_m = render_scene(scene, nodes, kernel)
            loss_m = _loss(h_m).item()
            flat[i] = old
            grad.flatten()[i] = (loss_p - loss_m) / (2.0 * eps)
    return grad


def test_gradients_match_finite_differences():
    kernel = _kernel()
    nodes = torch.tensor(
        [[0.0, 0.0, 0.0], [3.0, 0.0, 0.0], [1.5, 2.5, 0.0], [0.5, 1.2, 1.0]],
        dtype=torch.float64,
    )
    scene = _build_scene()
    h = render_scene(scene, nodes, kernel)
    loss = _loss(h)
    loss.backward()

    params = [
        ("surfel center", scene.center[1]), ("surfel scale", scene.scale_log[1]),
        ("surfel rot6d", scene.rot6d[1]), ("surfel rho.re", scene.rho.real[1]),
        ("surfel rho.im", scene.rho.imag[1]), ("surfel rough", scene.roughness[1]),
        ("plane center", scene.center[0]), ("plane rot6d", scene.rot6d[0]),
        ("plane extents", scene.scale_log[0]), ("plane rho.re", scene.rho.real[0]),
        ("capsule center", scene.center[2]), ("capsule rot6d", scene.rot6d[2]),
        ("capsule len", scene.scale_log[2, 0]), ("capsule radius", scene.scale_log[2, 1]),
        ("capsule rho.re", scene.rho.real[2]),
        ("presence", scene.presence),
    ]
    leaves = [
        ("center", scene.center), ("rot6d", scene.rot6d), ("scale_log", scene.scale_log),
        ("rho.re", scene.rho.real, scene.rho.grad.real),
        ("rho.im", scene.rho.imag, scene.rho.grad.imag),
        ("roughness", scene.roughness), ("presence", scene.presence),
    ]
    for entry in leaves:
        name, param = entry[0], entry[1]
        assert param.grad is not None or len(entry) == 3, name
        fd = _finite_difference_grad(scene, nodes, kernel, param)
        g = entry[2] if len(entry) == 3 else param.grad.detach()
        mask = g.abs() > 1e-8
        if not mask.any():
            continue
        rel = (fd - g).abs() / g.abs()
        assert float(rel[mask].max()) < 1e-3, (name, rel[mask].max())


def test_no_nan_gradients_in_degenerate_cases():
    kernel = _kernel()
    nodes = torch.tensor([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]], dtype=torch.float64)

    def check(scene: SceneTensors, label: str):
        h = render_scene(scene, nodes, kernel)
        _loss(h).backward(retain_graph=False)
        for p in (scene.center, scene.rot6d, scene.scale_log, scene.rho,
                  scene.presence):
            if p.grad is not None:
                assert torch.isfinite(p.grad).all(), (label, p.grad)

    # plane exactly edge-on: LOS lies in the plane (den == 0)
    s1 = SceneTensors.empty(1)
    s1.type_id[0] = PLANE
    s1.presence[0] = torch.tensor(1.0, dtype=torch.float64, requires_grad=True)
    s1.center = torch.tensor([[1.0, 0.3, 0.0]], dtype=torch.float64, requires_grad=True)
    s1.rot6d = torch.tensor([[1.0, 0.0, 0.0, 0.0, 0.0, 1.0]], dtype=torch.float64, requires_grad=True)
    s1.scale_log = torch.log(torch.tensor([[2.0, 2.0, 0.2]], dtype=torch.float64)).requires_grad_(True)
    s1.rho = torch.tensor([0.5 + 0.1j], dtype=torch.complex128, requires_grad=True)
    check(s1, "edge-on")

    # near-degenerate denominator (slight tilt)
    s2 = SceneTensors.empty(1)
    s2.type_id[0] = PLANE
    s2.presence[0] = torch.tensor(1.0, dtype=torch.float64, requires_grad=True)
    s2.center = torch.tensor([[1.0, 0.3, 0.0]], dtype=torch.float64, requires_grad=True)
    s2.rot6d = torch.tensor([[1.0, 0.0, 0.0, 1e-7, 0.0, 1.0]], dtype=torch.float64, requires_grad=True)
    s2.scale_log = torch.log(torch.tensor([[2.0, 2.0, 0.2]], dtype=torch.float64)).requires_grad_(True)
    s2.rho = torch.tensor([0.5 + 0.1j], dtype=torch.complex128, requires_grad=True)
    check(s2, "near-degenerate")

    # scale_log at the -6 clamp
    s3 = SceneTensors.empty(1)
    s3.type_id[0] = SURFEL
    s3.presence[0] = torch.tensor(1.0, dtype=torch.float64, requires_grad=True)
    s3.center = torch.tensor([[1.0, 0.4, 0.5]], dtype=torch.float64, requires_grad=True)
    s3.rot6d = torch.tensor([[1.0, 0.0, 0.0, 0.0, 1.0, 0.0]], dtype=torch.float64, requires_grad=True)
    s3.scale_log = torch.tensor([[-6.0, -6.0, -6.0]], dtype=torch.float64, requires_grad=True)
    s3.rho = torch.tensor([0.5 + 0.1j], dtype=torch.complex128, requires_grad=True)
    s3.roughness = torch.tensor([0.2], dtype=torch.float64, requires_grad=True)
    check(s3, "scale-underflow")

    # zero rho and zero presence
    s4 = SceneTensors.empty(1)
    s4.type_id[0] = CAPSULE
    s4.presence[0] = torch.tensor(0.0, dtype=torch.float64, requires_grad=True)
    s4.center = torch.tensor([[1.0, 0.4, 0.5]], dtype=torch.float64, requires_grad=True)
    s4.rot6d = torch.tensor([[1.0, 0.0, 0.0, 0.0, 1.0, 0.0]], dtype=torch.float64, requires_grad=True)
    s4.scale_log = torch.tensor([[0.0, -1.0, -1.0]], dtype=torch.float64, requires_grad=True)
    s4.rho = torch.tensor([0.0 + 0.0j], dtype=torch.complex128, requires_grad=True)
    check(s4, "zero-rho-presence")
