"""Scene tensor slots and differentiable type-specific views (paper Sec. V-C).

A scene is a fixed-size slot set: `G` slots, each with a type in
{empty, plane, surfel, capsule}, a continuous 6D rotation, positive
log-scales, complex reflectivity, and scalar radio parameters. Type-specific
geometry is derived on demand so one decoder population can represent all
three types.
"""

from __future__ import annotations

from dataclasses import dataclass

import torch

EMPTY = 0
PLANE = 1
SURFEL = 2
CAPSULE = 3

SCALE_LOG_MIN = -6.0  # guard against underflowing scales (test contract)


@dataclass
class SceneTensors:
    """Slot tensors over `G` decoder slots (paper Sec. V-C)."""

    type_id: torch.Tensor  # [G] int64 in {0 empty, 1 plane, 2 surfel, 3 capsule}
    presence: torch.Tensor  # [G] in [0, 1]
    center: torch.Tensor  # [G, 3]
    rot6d: torch.Tensor  # [G, 6] continuous 6D rotation
    scale_log: torch.Tensor  # [G, 3] positive log-scales
    rho: torch.Tensor  # [G] complex reflectivity
    roughness: torch.Tensor  # [G] diffuse fraction for B_g
    atten: torch.Tensor  # [G] attenuation strength (chord exponent scaling)
    dynamic_p: torch.Tensor  # [G] dynamic probability (human class prior)

    @staticmethod
    def empty(g: int, dtype: torch.dtype = torch.float64) -> "SceneTensors":
        return SceneTensors(
            type_id=torch.zeros(g, dtype=torch.long),
            presence=torch.zeros(g, dtype=dtype),
            center=torch.zeros(g, 3, dtype=dtype),
            rot6d=torch.zeros(g, 6, dtype=dtype),
            scale_log=torch.zeros(g, 3, dtype=dtype),
            rho=torch.zeros(g, dtype=torch.complex128 if dtype == torch.float64 else torch.complex64),
            roughness=torch.zeros(g, dtype=dtype),
            atten=torch.ones(g, dtype=dtype),
            dynamic_p=torch.zeros(g, dtype=dtype),
        )


def rot6d_to_matrix(r6: torch.Tensor) -> torch.Tensor:
    """Continuous 6D rotation representation (Gram-Schmidt) -> [..., 3, 3]."""
    c1 = r6[..., :3]
    c1 = c1 / torch.linalg.vector_norm(c1, dim=-1, keepdim=True).clamp(min=1e-12)
    c2_raw = r6[..., 3:]
    c2 = c2_raw - (c1 * c2_raw).sum(dim=-1, keepdim=True) * c1
    c2 = c2 / torch.linalg.vector_norm(c2, dim=-1, keepdim=True).clamp(min=1e-12)
    c3 = torch.cross(c1, c2, dim=-1)
    return torch.stack([c1, c2, c3], dim=-1)  # columns are the rotated axes


def plane_axes(scene: SceneTensors, g: int):
    """Plane: normal, tangent axes, half-extents (scale[0:2])."""
    r = rot6d_to_matrix(scene.rot6d[g])
    normal = r[:, 2]
    tangent = r[:, :2]  # [3, 2]
    half = torch.exp(scene.scale_log[g, :2].clamp(min=SCALE_LOG_MIN))
    return normal, tangent, half


def surfel_covariance(scene: SceneTensors, g: int) -> torch.Tensor:
    """Surfel covariance Sigma = R diag(s^2) R^T."""
    r = rot6d_to_matrix(scene.rot6d[g])
    s2 = torch.exp(2.0 * scene.scale_log[g].clamp(min=SCALE_LOG_MIN))
    return r @ torch.diag(s2) @ r.T  # [3, 3]


def capsule_axes(scene: SceneTensors, g: int):
    """Capsule: principal axis, half-length (scale[0]), radius (scale[1])."""
    r = rot6d_to_matrix(scene.rot6d[g])
    s = torch.exp(scene.scale_log[g].clamp(min=SCALE_LOG_MIN))
    return r[:, 2], s[0], s[1]
