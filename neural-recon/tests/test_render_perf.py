"""UWBRender performance smoke (plan Phase 2): 48 slots x 20 links CPU."""

from __future__ import annotations

import time

import torch

from nrecon.sim.primitives import CAPSULE, PLANE, SURFEL, SceneTensors
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.sim.render import render_scene


def test_full_render_forward_backward_under_30s():
    t = make_template_v1()
    kernel = torch.as_tensor(correlation_kernel(t, t).samples)
    nodes = torch.tensor(
        [[0.0, 0.0, 0.0], [1.7, 0.0, 0.0], [1.8, 2.3, 0.0],
         [0.4, 1.2, 1.3], [2.5, 0.7, 0.4]],
        dtype=torch.float64,
    )
    scene = SceneTensors.empty(48)
    for g in range(48):
        scene.presence[g] = 0.9
        scene.center[g] = torch.rand(3, dtype=torch.float64) * 3.0
        scene.scale_log[g] = torch.log(torch.rand(3, dtype=torch.float64) * 0.5 + 0.1)
        scene.rho[g] = (0.5 + 0.2j) * (torch.rand(1).item() + 0.5)
        if g < 20:
            scene.type_id[g] = PLANE
            scene.rot6d[g] = torch.as_tensor([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
        elif g < 36:
            scene.type_id[g] = SURFEL
            scene.rot6d[g] = torch.as_tensor([1.0, 0.1, 0.0, 1.0, 0.0, 0.1])
            scene.roughness[g] = 0.3
        else:
            scene.type_id[g] = CAPSULE
            scene.rot6d[g] = torch.as_tensor([0.9, 0.2, 0.1, 1.0, 0.0, 0.3])
    scene.center.requires_grad_(True)
    scene.scale_log.requires_grad_(True)
    scene.rho.requires_grad_(True)

    start = time.perf_counter()
    h = render_scene(scene, nodes, kernel)
    loss = (h.conj() * h).real.sum()
    loss.backward()
    elapsed = time.perf_counter() - start
    assert elapsed < 30.0, f"forward+backward took {elapsed:.1f}s"
    print(f"G_MAX=48, 20 links forward+backward: {elapsed:.2f}s")
