"""Numerical and gradient equivalence for the additive batched renderer."""

from __future__ import annotations

import torch

from nrecon.sim.primitives import CAPSULE, EMPTY, PLANE, SURFEL, SceneTensors
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.sim.render import (
    build_surfel_pulse_lookup,
    render_scene,
    render_scene_batched,
    render_surfel_slots_compact,
)
from nrecon.train.loop import render_predicted


def _kernel():
    template = make_template_v1()
    return torch.as_tensor(correlation_kernel(template, template).samples)


def _scene_batch(requires_grad: bool = False) -> SceneTensors:
    dtype = torch.float64
    center = torch.tensor([
        [[1.5, 1.2, 0.9], [1.2, 0.9, 0.8], [2.0, 1.0, 0.6]],
        [[1.1, 1.4, 0.7], [1.7, 0.8, 1.0], [1.8, 1.3, 0.5]],
    ], dtype=dtype)
    rot6d = torch.tensor([
        [[1.0, 0.0, 0.0, 0.0, 0.5, 0.8],
         [1.0, 0.1, 0.0, 1.0, 0.0, 0.2],
         [0.9, 0.2, 0.1, 1.0, 0.0, 0.3]],
        [[0.9, 0.1, 0.0, 0.0, 0.7, 0.9],
         [1.0, 0.0, 0.2, 0.8, 0.1, 0.3],
         [0.8, 0.3, 0.1, 0.9, 0.1, 0.2]],
    ], dtype=dtype)
    scale_log = torch.log(torch.tensor([
        [[2.0, 1.5, 0.2], [0.4, 0.3, 0.2], [0.5, 0.2, 0.1]],
        [[1.8, 1.2, 0.2], [0.3, 0.5, 0.2], [0.4, 0.15, 0.1]],
    ], dtype=dtype))
    rho = torch.tensor([
        [0.5 + 0.1j, 0.6 - 0.2j, 0.4 + 0.2j],
        [0.4 - 0.1j, 0.7 + 0.1j, 0.3 + 0.2j],
    ], dtype=torch.complex128)
    presence = torch.tensor([[0.9, 0.8, 0.7], [0.85, 0.75, 0.65]], dtype=dtype)
    roughness = torch.tensor([[0.1, 0.3, 0.2], [0.2, 0.4, 0.1]], dtype=dtype)
    for value in (center, rot6d, scale_log, rho, presence, roughness):
        value.requires_grad_(requires_grad)
    return SceneTensors(
        type_id=torch.tensor([[PLANE, SURFEL, CAPSULE],
                              [CAPSULE, PLANE, SURFEL]]),
        presence=presence,
        center=center,
        rot6d=rot6d,
        scale_log=scale_log,
        rho=rho,
        roughness=roughness,
        atten=torch.ones(2, 3, dtype=dtype),
        dynamic_p=torch.zeros(2, 3, dtype=dtype),
    )


def _nodes():
    return torch.tensor([
        [[0.0, 0.0, 0.0], [3.0, 0.0, 0.0], [1.5, 2.5, 0.0]],
        [[0.2, 0.1, 0.0], [2.8, 0.2, 0.1], [1.4, 2.3, 0.2]],
    ], dtype=torch.float64)


def _slice_scene(scene: SceneTensors, bi: int) -> SceneTensors:
    return SceneTensors(**{
        name: getattr(scene, name)[bi]
        for name in SceneTensors.__dataclass_fields__
    })


def test_batched_renderer_matches_scalar_reference():
    scene = _scene_batch()
    nodes = _nodes()
    kernel = _kernel()
    expected = torch.stack([
        render_scene(_slice_scene(scene, bi), nodes[bi], kernel)
        for bi in range(nodes.shape[0])
    ])
    actual = render_scene_batched(scene, nodes, kernel)
    assert torch.allclose(actual, expected, rtol=1e-10, atol=1e-11)


def test_batched_renderer_gradients_match_scalar_reference():
    kernel = _kernel()
    nodes = _nodes()
    scalar_scene = _scene_batch(requires_grad=True)
    scalar_h = torch.stack([
        render_scene(_slice_scene(scalar_scene, bi), nodes[bi], kernel)
        for bi in range(nodes.shape[0])
    ])
    (scalar_h.abs().square().sum()).backward()

    batched_scene = _scene_batch(requires_grad=True)
    batched_h = render_scene_batched(batched_scene, nodes, kernel)
    (batched_h.abs().square().sum()).backward()

    for name in ("presence", "center", "rot6d", "scale_log", "rho", "roughness"):
        expected = getattr(scalar_scene, name).grad
        actual = getattr(batched_scene, name).grad
        assert expected is not None and actual is not None, name
        assert torch.allclose(actual, expected, rtol=2e-8, atol=1e-9), name


def test_compact_surfel_slots_match_scalar_outputs_and_gradients():
    kernel = _kernel()
    lookup = build_surfel_pulse_lookup(kernel, "bank-16x")
    nodes = _nodes()[0]
    scalar_batch = _scene_batch(requires_grad=True)
    compact_batch = _scene_batch(requires_grad=True)
    scalar_scene = _slice_scene(scalar_batch, 0)
    compact_scene = _slice_scene(compact_batch, 0)
    scalar = render_scene(scalar_scene, nodes, kernel, surfel_lookup=lookup)
    compact = render_scene(compact_scene, nodes, kernel, surfel_lookup=lookup,
                           compact_surfel_slots=True)
    assert torch.allclose(compact, scalar, rtol=2e-10, atol=2e-11)
    scalar.abs().square().sum().backward()
    compact.abs().square().sum().backward()
    for name in ("presence", "center", "rot6d", "scale_log", "rho", "roughness"):
        expected = getattr(scalar_batch, name).grad
        actual = getattr(compact_batch, name).grad
        assert expected is not None and actual is not None, name
        assert torch.allclose(actual, expected, rtol=2e-8, atol=1e-9), name


def test_render_predicted_batched_switch_matches_fallback():
    scene = _scene_batch()
    logits = torch.full((2, 3, 4), -5.0, dtype=torch.float64)
    logits.scatter_(-1, scene.type_id[..., None], 5.0)
    pred = {
        "type_logits": logits,
        "presence": scene.presence[..., None],
        "center": scene.center,
        "rot6d": scene.rot6d,
        "scale_log": scene.scale_log,
        "rho": torch.view_as_real(scene.rho),
        "roughness": scene.roughness[..., None],
        "atten": scene.atten[..., None],
        "dynamic": scene.dynamic_p[..., None],
    }
    batch = {"node_pos": _nodes()}
    kernel = _kernel()
    expected = render_predicted(pred, batch, kernel, dtype=torch.float64)
    actual = render_predicted(pred, batch, kernel, dtype=torch.float64, batched=True)
    assert torch.allclose(actual, expected, rtol=1e-10, atol=1e-11)


def test_render_predicted_presence_threshold_omits_low_confidence_slots():
    scene = _scene_batch()
    logits = torch.full((2, 3, 4), -5.0, dtype=torch.float64)
    logits.scatter_(-1, scene.type_id[..., None], 5.0)
    pred = {
        "type_logits": logits,
        "presence": scene.presence[..., None].clone(),
        "center": scene.center,
        "rot6d": scene.rot6d,
        "scale_log": scene.scale_log,
        "rho": torch.view_as_real(scene.rho),
        "roughness": scene.roughness[..., None],
        "atten": scene.atten[..., None],
        "dynamic": scene.dynamic_p[..., None],
    }
    pred["presence"][..., 1, 0] = 0.01
    omitted = pred["type_logits"].clone()
    omitted[..., 1, :] = -5.0
    omitted[..., 1, EMPTY] = 5.0
    expected = render_predicted(
        {**pred, "type_logits": omitted}, {"node_pos": _nodes()}, _kernel(),
        dtype=torch.float64)
    actual = render_predicted(
        pred, {"node_pos": _nodes()}, _kernel(), dtype=torch.float64,
        presence_threshold=0.1)
    assert torch.equal(actual, expected)


@torch.no_grad()
def test_cached_backends_match_between_scalar_and_batched_renderers():
    scene = _scene_batch()
    nodes = _nodes()
    kernel = _kernel()
    exact = render_scene_batched(scene, nodes, kernel)
    for backend in ("bank-16x", "cache-1x-phase"):
        lookup = build_surfel_pulse_lookup(kernel, backend)
        expected = torch.stack([
            render_scene(_slice_scene(scene, bi), nodes[bi], kernel,
                         surfel_lookup=lookup)
            for bi in range(nodes.shape[0])
        ])
        actual = render_scene_batched(scene, nodes, kernel, surfel_lookup=lookup)
        assert torch.allclose(actual, expected, rtol=1e-10, atol=1e-11), backend
        nrmse = torch.linalg.vector_norm(actual - exact) / torch.linalg.vector_norm(exact)
        assert float(nrmse) < 1e-3, backend


def test_cached_scene_gradients_track_analytic_reference():
    nodes = _nodes()
    kernel = _kernel()
    exact_scene = _scene_batch(requires_grad=True)
    render_scene_batched(exact_scene, nodes, kernel).abs().square().sum().backward()
    names = ("center", "rot6d", "scale_log")
    exact_grad = torch.cat([getattr(exact_scene, name).grad.flatten() for name in names])

    for backend in ("bank-16x", "cache-1x-phase"):
        scene = _scene_batch(requires_grad=True)
        lookup = build_surfel_pulse_lookup(kernel, backend)
        render_scene_batched(
            scene, nodes, kernel, surfel_lookup=lookup).abs().square().sum().backward()
        grad = torch.cat([getattr(scene, name).grad.flatten() for name in names])
        cosine = torch.nn.functional.cosine_similarity(grad, exact_grad, dim=0)
        relative = torch.linalg.vector_norm(grad - exact_grad) / torch.linalg.vector_norm(
            exact_grad)
        assert float(cosine) > 0.999
        assert float(relative) < 5e-3


def test_compact_capsule_attenuation_matches_legacy_output_and_gradients():
    nodes = _nodes()
    kernel = _kernel()
    legacy_scene = _scene_batch(requires_grad=True)
    legacy = render_scene_batched(legacy_scene, nodes, kernel)
    legacy.abs().square().sum().backward()

    compact_scene = _scene_batch(requires_grad=True)
    compact = render_scene_batched(
        compact_scene, nodes, kernel, capsule_attenuation_backend="compact")
    compact.abs().square().sum().backward()
    assert torch.allclose(compact, legacy, rtol=1e-12, atol=1e-12)
    for name in ("center", "rot6d", "scale_log"):
        assert torch.allclose(getattr(compact_scene, name).grad,
                              getattr(legacy_scene, name).grad,
                              rtol=1e-9, atol=1e-10), name


def test_gaussian_capsule_approximation_is_finite_and_bounded():
    nodes = _nodes()
    kernel = _kernel()
    scene = _scene_batch(requires_grad=True)
    exact = render_scene_batched(
        scene, nodes, kernel, capsule_attenuation_backend="compact").detach()
    approximate = render_scene_batched(
        scene, nodes, kernel, capsule_attenuation_backend="gaussian")
    nrmse = torch.linalg.vector_norm(approximate - exact) / torch.linalg.vector_norm(exact)
    assert float(nrmse.detach()) < 0.2
    approximate.abs().square().sum().backward()
    for name in ("center", "rot6d", "scale_log"):
        assert torch.isfinite(getattr(scene, name).grad).all(), name


def test_explicit_link_subset_matches_full_render():
    scene = _scene_batch()
    nodes = _nodes()
    kernel = _kernel()
    from nrecon.constants import directed_links

    all_links = directed_links(nodes.shape[1])
    indices = [0, 2, 5]
    subset = [all_links[index] for index in indices]
    full = render_scene_batched(scene, nodes, kernel)
    selected = render_scene_batched(scene, nodes, kernel, links=subset)
    # Exact broadening chooses its negligible +/-4-sigma tail truncation from
    # the largest link in the call, so changing the link set perturbs tails at
    # roughly 1e-10 relative even though the selected physics is unchanged.
    assert torch.allclose(selected, full[:, indices], rtol=1e-9, atol=1e-10)
