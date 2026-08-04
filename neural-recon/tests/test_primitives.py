"""Primitive type-view tests (plan Phase 2)."""

from __future__ import annotations

import torch

from nrecon.seeding import seed_all
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


def _random_so3() -> torch.Tensor:
    m = torch.randn(3, 3, dtype=torch.float64)
    q, _ = torch.linalg.qr(m)
    if torch.det(q) < 0:
        q[:, 0] = -q[:, 0]
    return q


def _to_rot6d(r: torch.Tensor) -> torch.Tensor:
    return torch.cat([r[:, 0], r[:, 1]])  # column-major: c1 then c2


def test_rot6d_recovers_random_rotations():
    seed_all(21)
    for _ in range(5):
        r = _random_so3()
        rec = rot6d_to_matrix(_to_rot6d(r))
        assert torch.allclose(rec, r, atol=1e-6)
        assert torch.allclose(rec.T @ rec, torch.eye(3, dtype=torch.float64), atol=1e-9)
        assert abs(float(torch.det(rec)) - 1.0) < 1e-9


def test_plane_axes_view():
    seed_all(22)
    scene = SceneTensors.empty(1)
    r = _random_so3()
    scene.type_id[0] = PLANE
    scene.rot6d[0] = _to_rot6d(r)
    scene.scale_log[0, :2] = torch.log(torch.tensor([2.0, 1.5], dtype=torch.float64))
    normal, tangent, half = plane_axes(scene, 0)
    assert torch.allclose(normal, r[:, 2], atol=1e-12)
    assert torch.allclose(half, torch.tensor([2.0, 1.5], dtype=torch.float64))
    assert torch.allclose(tangent[:, 0], r[:, 0], atol=1e-12)
    assert torch.allclose(tangent[:, 1], r[:, 1], atol=1e-12)


def test_surfel_covariance_view():
    seed_all(23)
    scene = SceneTensors.empty(1)
    r = _random_so3()
    scene.type_id[0] = SURFEL
    scene.rot6d[0] = _to_rot6d(r)
    scene.scale_log[0] = torch.log(torch.tensor([0.5, 0.3, 0.2], dtype=torch.float64))
    cov = surfel_covariance(scene, 0)
    assert torch.allclose(cov, r @ torch.diag(torch.tensor([0.25, 0.09, 0.04], dtype=torch.float64)) @ r.T, atol=1e-12)
    assert torch.allclose(cov, cov.T, atol=1e-12)
    assert torch.all(torch.linalg.eigvalsh(cov) > 0)


def test_capsule_axes_view():
    seed_all(24)
    scene = SceneTensors.empty(1)
    r = _random_so3()
    scene.type_id[0] = CAPSULE
    scene.rot6d[0] = _to_rot6d(r)
    scene.scale_log[0, :2] = torch.log(torch.tensor([0.8, 0.25], dtype=torch.float64))
    axis, half_len, radius = capsule_axes(scene, 0)
    assert torch.allclose(axis, r[:, 2], atol=1e-12)
    assert float(half_len) == 0.8
    assert float(radius) == 0.25
