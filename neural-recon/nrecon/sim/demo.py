"""Canned-room demo: render a room + surfel + capsule at the fixed live
Heimdall geometry and write per-link envelope SVG plots to artifacts/demo/.

Usage: python -m nrecon.sim.demo [geometry.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import torch

from nrecon.constants import F0_MARKER, S_TAPS
from nrecon.seeding import seed_all
from nrecon.sim.delay import fractional_shift
from nrecon.sim.primitives import CAPSULE, PLANE, SURFEL, SceneTensors
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.sim.render import render_scene

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GEOMETRY = REPO_ROOT / "deployment" / "radar-geometry.live-20260728.json"
OUT_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "demo"


def _plane_rot6d(normal: np.ndarray) -> np.ndarray:
    n = normal / np.linalg.norm(normal)
    ref = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    a = np.cross(ref, n)
    a = a / np.linalg.norm(a)
    b = np.cross(n, a)
    r = np.stack([a, b, n], axis=-1)
    return r[:, :2].T.reshape(-1)


def build_demo_scene(nodes: torch.Tensor) -> SceneTensors:
    seed_all(20260728)
    cx = float(nodes[:, 0].mean())
    cy = float(nodes[:, 1].mean())
    hx, hy, hz = 2.5, 2.5, 1.5
    planes = [
        ("wall -x", np.array([-hx, 0.0, 0.0]), np.array([-1.0, 0.0, 0.0]), (hy, hz)),
        ("wall +x", np.array([hx, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]), (hy, hz)),
        ("wall -y", np.array([0.0, -hy, 0.0]), np.array([0.0, -1.0, 0.0]), (hx, hz)),
        ("wall +y", np.array([0.0, hy, 0.0]), np.array([0.0, 1.0, 0.0]), (hx, hz)),
        ("floor", np.array([0.0, 0.0, 0.0]), np.array([0.0, 0.0, -1.0]), (hx, hy)),
        ("ceiling", np.array([0.0, 0.0, 2.0 * hz]), np.array([0.0, 0.0, 1.0]), (hx, hy)),
    ]
    n_planes = len(planes)
    g = n_planes + 2
    scene = SceneTensors.empty(g)
    rho_plane = 0.4 + 0.1 * torch.rand(n_planes)
    for k, (name, c, nrm, half) in enumerate(planes):
        scene.type_id[k] = PLANE
        scene.presence[k] = 1.0
        scene.center[k] = torch.as_tensor(c, dtype=torch.float64)
        scene.rot6d[k] = torch.as_tensor(_plane_rot6d(nrm), dtype=torch.float64)
        scene.scale_log[k, :2] = torch.log(torch.as_tensor(half, dtype=torch.float64))
        scene.rho[k] = rho_plane[k]
    surf = n_planes
    scene.type_id[surf] = SURFEL
    scene.presence[surf] = 1.0
    scene.center[surf] = torch.as_tensor([cx + 0.4, cy - 0.6, 1.0], dtype=torch.float64)
    scene.scale_log[surf] = torch.log(torch.as_tensor([0.30, 0.20, 0.15], dtype=torch.float64))
    scene.rho[surf] = 0.6 + 0.3j
    scene.roughness[surf] = 0.25
    cap = n_planes + 1
    scene.type_id[cap] = CAPSULE
    scene.presence[cap] = 1.0
    scene.center[cap] = torch.as_tensor([cx + 1.1, cy, 0.9], dtype=torch.float64)
    scene.scale_log[cap] = torch.log(torch.as_tensor([0.5, 0.2, 0.1], dtype=torch.float64))
    scene.rho[cap] = 0.7 - 0.2j
    return scene


def _svg_envelope(path: Path, title: str, env: np.ndarray, los_tap: float) -> None:
    w, h, pad = 640, 240, 10
    peaks = np.argsort(env)[-3:][::-1]
    span = max(float(np.max(env)), 1e-9)
    lines = []
    points = [
        (pad + 2 + i * (w - 2 * pad) / S_TAPS, h - pad - (env[i] / span) * (h - 2 * pad))
        for i in range(S_TAPS)
    ]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
    )
    lines.append(f'<rect width="{w}" height="{h}" fill="white"/>')
    lines.append(f'<text x="{pad}" y="18" font-size="12">{title}</text>')
    los_x = pad + 2 + los_tap * (w - 2 * pad) / S_TAPS
    lines.append(f'<line x1="{los_x:.1f}" y1="{pad}" x2="{los_x:.1f}" y2="{h-pad}" stroke="#999" stroke-dasharray="3 3"/>')
    lines.append(f'<polyline points="{pts}" fill="none" stroke="#123f67" stroke-width="1.5"/>')
    for rank, i in enumerate(peaks):
        x = pad + 2 + i * (w - 2 * pad) / S_TAPS
        y = h - pad - (env[i] / span) * (h - 2 * pad)
        lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#b1462f"/>')
        lines.append(f'<text x="{x:.1f}" y="{y-5:.1f}" font-size="10" fill="#b1462f">#{i}</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    geom_path = Path(argv[0]) if argv else DEFAULT_GEOMETRY
    if not geom_path.exists():
        sys.exit(f"geometry file not found: {geom_path}")
    data = json.loads(geom_path.read_text(encoding="utf-8"))
    node_rows = sorted(data["nodes"], key=lambda r: r["node_id"])
    nodes = torch.as_tensor(
        [r["position_m"] for r in node_rows], dtype=torch.float64
    )

    template = make_template_v1()
    kernel = torch.as_tensor(correlation_kernel(template, template).samples)
    scene = build_demo_scene(nodes)
    h = render_scene(scene, nodes, kernel)
    h_al = fractional_shift(h, torch.full((h.shape[0],), F0_MARKER, dtype=torch.float64))
    env = h_al.abs().numpy()

    out = OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    for li, (a, b) in enumerate([(i, j) for i in range(5) for j in range(5) if i != j]):
        _svg_envelope(out / f"link-{a}-{b}.svg", f"link {a}->{b} envelope",
                      env[li], F0_MARKER)
    print(f"demo rendered to {out}")
    print(f"LOS peak taps: {[int(np.argmax(e[:40])) for e in env]}")
    print(f"max envelope (link index): {int(np.unravel_index(np.argmax(env), env.shape)[0])}")


if __name__ == "__main__":
    main()
