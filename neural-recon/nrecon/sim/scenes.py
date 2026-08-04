"""Procedural scene samplers (paper Sec. VIII-A), driven by one
`np.random.Generator` per scene for bit-exact determinism.

Rooms are shoeboxes (4 walls + floor + ceiling) plus optional partition
planes; furniture is small plane assemblies plus anisotropic surfels; people
are capsules. `sample_nodes` produces non-coplanar N=5 perimeter layouts or
loads the fixed live geometry with optional jitter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import torch

from nrecon.constants import G_MAX
from nrecon.sim.primitives import PLANE, SURFEL, CAPSULE, SceneTensors

LIVE_GEOMETRY = (
    Path(__file__).resolve().parents[3] / "deployment" / "radar-geometry.live-20260728.json"
)

PLANE_T = PLANE
SURFEL_T = SURFEL
CAPSULE_T = CAPSULE

_COPLANAR_SV = 0.1  # smallest singular value threshold for non-coplanarity (PROVISIONAL)
_MIN_SEPARATION = 0.6  # metres (PROVISIONAL)


@dataclass
class PrimitiveSpec:
    type: int  # PLANE_T / SURFEL_T / CAPSULE_T
    center: np.ndarray  # [3]
    rot: np.ndarray  # [3, 3] full rotation matrix (storage convention)
    scale: np.ndarray  # [3] metres (plane half-extents for [0:2])
    rho: complex
    rough: float = 0.0
    atten: float = 1.0
    dynamic: float = 0.0


@dataclass
class SceneSpec:
    room_id: int
    layout_id: int
    stage: int
    seed: int
    nodes: np.ndarray  # [N, 3]
    primitives: list = field(default_factory=list)


def rotation_from_normal(normal: np.ndarray) -> np.ndarray:
    """Orthonormal [a, b, n] columns with `normal` as the third axis."""
    n = np.asarray(normal, dtype=np.float64) / np.linalg.norm(normal)
    ref = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    a = np.cross(ref, n)
    a = a / np.linalg.norm(a)
    b = np.cross(n, a)
    return np.stack([a, b, n], axis=-1)


def _draw(rng: np.random.Generator, lo: float, hi: float) -> float:
    return float(rng.uniform(lo, hi))


def _sample_room(rng: np.random.Generator, cfg: dict) -> list:
    """4-8 finite planes: shoebox + optional partitions (PROVISIONAL sizes)."""
    x = _draw(rng, cfg["x_range"][0], cfg["x_range"][1])
    y = _draw(rng, cfg["y_range"][0], cfg["y_range"][1])
    z = _draw(rng, cfg["z_max"][0], cfg["z_max"][1])
    cx, cy, cz = x / 2.0, y / 2.0, z / 2.0
    planes = [
        PrimitiveSpec(PLANE_T, np.array([0.0, cy, cz]), rotation_from_normal(np.array([1.0, 0.0, 0.0])),
                      np.array([x / 2.0, y / 2.0, z / 2.0]), 0.5 + 0.3j * rng.standard_normal(),
                      _draw(rng, 0.1, 0.5)),
        PrimitiveSpec(PLANE_T, np.array([x, cy, cz]), rotation_from_normal(np.array([-1.0, 0.0, 0.0])),
                      np.array([x / 2.0, y / 2.0, z / 2.0]), 0.5 + 0.3j * rng.standard_normal(),
                      _draw(rng, 0.1, 0.5)),
        PrimitiveSpec(PLANE_T, np.array([cx, 0.0, cz]), rotation_from_normal(np.array([0.0, 1.0, 0.0])),
                      np.array([x / 2.0, y / 2.0, z / 2.0]), 0.5 + 0.3j * rng.standard_normal(),
                      _draw(rng, 0.1, 0.5)),
        PrimitiveSpec(PLANE_T, np.array([cx, y, cz]), rotation_from_normal(np.array([0.0, -1.0, 0.0])),
                      np.array([x / 2.0, y / 2.0, z / 2.0]), 0.5 + 0.3j * rng.standard_normal(),
                      _draw(rng, 0.1, 0.5)),
        PrimitiveSpec(PLANE_T, np.array([cx, cy, 0.0]), rotation_from_normal(np.array([0.0, 0.0, 1.0])),
                      np.array([x / 2.0, y / 2.0, z / 2.0]), 0.6 + 0.3j * rng.standard_normal(),
                      _draw(rng, 0.1, 0.4)),
        PrimitiveSpec(PLANE_T, np.array([cx, cy, z]), rotation_from_normal(np.array([0.0, 0.0, -1.0])),
                      np.array([x / 2.0, y / 2.0, z / 2.0]), 0.5 + 0.3j * rng.standard_normal(),
                      _draw(rng, 0.1, 0.4)),
    ]
    n_parts = int(rng.integers(cfg["partitions"][0], cfg["partitions"][1] + 1))
    for _ in range(n_parts):
        wall = rng.integers(0, 2)  # 0: x-wall, 1: y-wall
        pos = _draw(rng, 0.3 * x, 0.7 * x) if wall == 0 else _draw(rng, 0.3 * y, 0.7 * y)
        if wall == 0:
            normal = np.array([1.0, 0.0, 0.0]) if rng.random() < 0.5 else np.array([-1.0, 0.0, 0.0])
            center = np.array([pos, cy, cz])
            half = np.array([0.02, y / 2.0, z / 2.0])
        else:
            normal = np.array([0.0, 1.0, 0.0]) if rng.random() < 0.5 else np.array([0.0, -1.0, 0.0])
            center = np.array([cx, pos, cz])
            half = np.array([x / 2.0, 0.02, z / 2.0])
        planes.append(PrimitiveSpec(PLANE_T, center, rotation_from_normal(normal), half,
                                    0.4 + 0.3j * rng.standard_normal(), _draw(rng, 0.1, 0.5)))
    return planes


def _sample_furniture(rng: np.random.Generator, cfg: dict, x: float, y: float, z: float) -> list:
    items = []
    for _ in range(int(rng.integers(cfg["count"][0], cfg["count"][1] + 1))):
        kind = rng.random()
        if kind < 0.5:
            # table/desk: top plane assembly (thin box face) + 2 leg planes
            w = _draw(rng, 0.6, 1.6)
            d = _draw(rng, 0.6, 1.6)
            h = _draw(rng, 0.7, 0.9)
            px, py = _draw(rng, 0.15 * x, 0.85 * x), _draw(rng, 0.15 * y, 0.85 * y)
            top_center = np.array([px, py, h])
            items.append(PrimitiveSpec(PLANE_T, top_center,
                                       rotation_from_normal(np.array([0.0, 0.0, 1.0])),
                                       np.array([w / 2.0, d / 2.0, h / 2.0]),
                                       0.4 + 0.2j * rng.standard_normal(), _draw(rng, 0.1, 0.4)))
            for leg in range(2):
                lx = px + (w / 3.0) * (1.0 if leg == 0 else -1.0)
                items.append(PrimitiveSpec(PLANE_T, np.array([lx, py, h / 2.0]),
                                           rotation_from_normal(np.array([0.0, 1.0, 0.0])),
                                           np.array([0.02, w / 3.0, h / 2.0]),
                                           0.3 + 0.2j * rng.standard_normal(), 0.3))
        else:
            # surfel furniture (chair / clutter)
            s = np.exp(rng.uniform(np.log(0.15), np.log(0.6)))
            items.append(PrimitiveSpec(
                SURFEL_T,
                np.array([_draw(rng, 0.2 * x, 0.8 * x), _draw(rng, 0.2 * y, 0.8 * y),
                          _draw(rng, 0.3, z - 0.3)]),
                rotation_from_normal(np.array([0.0, 0.0, 1.0])),
                np.array([s, s * _draw(rng, 0.5, 1.5), s * _draw(rng, 0.3, 0.8)]),
                0.6 + 0.3j * rng.standard_normal(), _draw(rng, 0.1, 0.6)))
    return items


def _sample_people(rng: np.random.Generator, cfg: dict, x: float, y: float, z: float) -> list:
    people = []
    for _ in range(int(rng.integers(cfg["count"][0], cfg["count"][1] + 1))):
        height = _draw(rng, cfg["height"][0], cfg["height"][1])
        radius = _draw(rng, cfg["radius"][0], cfg["radius"][1])
        px, py = _draw(rng, 0.15 * x, 0.85 * x), _draw(rng, 0.15 * y, 0.85 * y)
        people.append(PrimitiveSpec(
            CAPSULE_T,
            np.array([px, py, height / 2.0]),
            rotation_from_normal(np.array([0.0, 0.0, 1.0])),
            np.array([height / 2.0 - radius, radius, radius]),
            0.7 + 0.2j * rng.standard_normal(), _draw(rng, 0.1, 0.3),
            _draw(rng, 2.0, 6.0), 1.0))
    return people


def _sample_surfel_cloud(rng: np.random.Generator, cfg: dict, x: float, y: float, z: float) -> list:
    surfels = []
    for _ in range(int(rng.integers(cfg["count"][0], cfg["count"][1] + 1))):
        s = np.exp(rng.uniform(np.log(0.15), np.log(0.8)))
        surfels.append(PrimitiveSpec(
            SURFEL_T,
            np.array([_draw(rng, 0.2 * x, 0.8 * x), _draw(rng, 0.2 * y, 0.8 * y),
                      _draw(rng, 0.3, z - 0.4)]),
            rotation_from_normal(np.array([0.0, 0.0, 1.0])),
            np.array([s, s * _draw(rng, 0.5, 2.0), s * _draw(rng, 0.4, 1.5)]),
            0.6 + 0.3j * rng.standard_normal(), _draw(rng, 0.1, 0.6)))
    return surfels


def sample_nodes(rng: np.random.Generator, cfg: dict, mode: str = "random",
                 jitter_m: float = 0.0) -> np.ndarray:
    """N=5 non-coplanar perimeter layouts (random) or fixed live geometry."""
    if mode == "fixed_live":
        data = json.loads(LIVE_GEOMETRY.read_text(encoding="utf-8"))
        rows = sorted(data["nodes"], key=lambda r: r["node_id"])
        nodes = np.asarray([r["position_m"] for r in rows], dtype=np.float64)
        if jitter_m > 0.0:
            nodes = nodes + rng.normal(0.0, jitter_m, size=nodes.shape)
        return nodes

    n = 5
    for _ in range(64):
        radius = _draw(rng, 1.5, 3.5)
        angles = np.sort(rng.uniform(0.0, 2.0 * np.pi, size=n))
        nodes = np.stack([radius * np.cos(angles), radius * np.sin(angles),
                          rng.uniform(0.0, 1.5, size=n)], axis=-1)
        nodes = nodes - nodes.mean(axis=0)
        if _min_sep(nodes) < _MIN_SEPARATION:
            continue
        centered = nodes - nodes.mean(axis=0)
        sv = np.linalg.svd(centered, compute_uv=False)
        if sv[-1] > _COPLANAR_SV and sv[1] > 0.5:
            return nodes
    raise RuntimeError("could not sample a non-coplanar layout")


def _min_sep(nodes: np.ndarray) -> float:
    best = np.inf
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            best = min(best, float(np.linalg.norm(nodes[i] - nodes[j])))
    return best


def sample_scene(seed: int, cfg: dict, layout_index: int = 0) -> SceneSpec:
    """Deterministically sample one full scene for the stage config.

    Objects (room, furniture, people, surfels) are drawn from
    `PCG64(seed)`; the node layout comes from a dedicated layout RNG so
    `layouts_per_scene` > 1 renders the same scene under several layouts
    (paper Sec. VIII-A).
    """
    rng = np.random.Generator(np.random.PCG64(seed))
    room_cfg = cfg["room"]
    x = _draw(rng, room_cfg["x_range"][0], room_cfg["x_range"][1])
    y = _draw(rng, room_cfg["y_range"][0], room_cfg["y_range"][1])
    z = _draw(rng, room_cfg["z_max"][0], room_cfg["z_max"][1])

    primitives = []
    if room_cfg.get("planes", True):
        primitives += _sample_room(rng, room_cfg)
    primitives += _sample_furniture(rng, cfg.get("furniture", {"count": [0, 0]}), x, y, z)
    primitives += _sample_people(rng, cfg.get("people", {"count": [0, 0]}), x, y, z)
    primitives += _sample_surfel_cloud(rng, cfg.get("surfels", {"count": [0, 0]}), x, y, z)

    if len(primitives) > G_MAX:
        primitives = primitives[:G_MAX]

    layout_rng = np.random.Generator(
        np.random.PCG64(seed ^ (0x9E3779B97F4A7C15 * (layout_index + 1)))
    )
    node_mode = cfg.get("node_mode", "fixed_live")
    jitter_m = float(cfg.get("jitter_m", 0.0))
    nodes = sample_nodes(layout_rng, cfg, mode=node_mode, jitter_m=jitter_m)
    layout_id = int(layout_rng.integers(0, 1 << 31))

    return SceneSpec(room_id=seed, layout_id=layout_id, stage=cfg["stage"],
                     seed=seed, nodes=nodes, primitives=primitives)


def spec_to_scene(spec: SceneSpec, g_max: int = G_MAX) -> SceneTensors:
    """Assemble `SceneTensors` (G = G_MAX slots) from a scene spec."""
    n = len(spec.primitives)
    if n > g_max:
        raise ValueError(f"scene has {n} primitives, over G_MAX={g_max}")
    dtype = torch.float64
    scene = SceneTensors.empty(g_max, dtype=dtype)
    for g, p in enumerate(spec.primitives):
        scene.type_id[g] = p.type
        scene.presence[g] = 1.0
        scene.center[g] = torch.as_tensor(p.center, dtype=dtype)
        scene.rot6d[g] = torch.as_tensor(p.rot[:, :2].T.reshape(-1), dtype=dtype)
        scene.scale_log[g] = torch.log(torch.as_tensor(p.scale, dtype=dtype).clamp(min=1e-6))
        scene.rho[g] = p.rho
        scene.roughness[g] = p.rough
        scene.atten[g] = p.atten
        scene.dynamic_p[g] = p.dynamic
    return scene
