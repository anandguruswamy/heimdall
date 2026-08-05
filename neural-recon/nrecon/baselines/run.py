"""Phase 4 experiment runner: V1 (1-surfel multistart) and V2 (2-4 planes +
1 surfel, the paper's decisive experiment).

Usage: python -m nrecon.baselines.run v1 --config configs/exp-v1-surfel.yaml
       python -m nrecon.baselines.run v2 --config configs/exp-v2-planes-surfel.yaml

Results: runs/fit/<name>/scene-*.json plus runs/fit/<name>/summary.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
import yaml

from nrecon.baselines.backprojection import envelope_from_record
from nrecon.baselines.ellipsoid_voting import extract_peaks, vote
from nrecon.baselines.fit_scene import (
    FitConfig,
    fit_scene,
    init_from_points,
    init_gt_perturbed,
    init_random,
)
from nrecon.constants import directed_links
from nrecon.sim.export import build_scene_pipeline
from nrecon.sim.primitives import PLANE, SURFEL, rot6d_to_matrix
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.sim.quantize import from_i16
from nrecon.sim.scenes import (
    LIVE_GEOMETRY,
    PrimitiveSpec,
    SceneSpec,
    rotation_from_normal,
    sample_scene,
    spec_to_scene,
)

RUNS = Path(__file__).resolve().parents[2] / "runs" / "fit"


def _kernel():
    t = make_template_v1()
    return torch.as_tensor(correlation_kernel(t, t).samples)


def _live_nodes() -> torch.Tensor:
    import json as _json

    data = _json.loads(LIVE_GEOMETRY.read_text(encoding="utf-8"))
    rows = sorted(data["nodes"], key=lambda r: r["node_id"])
    return torch.as_tensor([r["position_m"] for r in rows], dtype=torch.float64)


def _bounds(nodes: torch.Tensor) -> np.ndarray:
    lo = nodes.numpy().min(axis=0) - 1.5
    hi = nodes.numpy().max(axis=0) + 1.5
    lo[2] = 0.2
    hi[2] = max(hi[2], 2.6)
    return np.stack([lo, hi])


def _target_from_record(rec: dict) -> tuple:
    """Scaled complex target, fp taps, gain/accum ratio from a record row."""
    target = from_i16(rec["cir_i16"], rec["dgc"], rec["accum"])
    fp = rec["fp_aligned"].astype(np.float64)
    gain = 10.0 ** ((rec["dgc"].astype(np.float64) - 3.0) * 2.65 / 20.0)
    gain_accum = gain / np.maximum(1, rec["accum"].astype(np.float64))
    return target, fp, gain_accum


def _fit_cfg(scene_cfg: dict, iterations: int = None) -> FitConfig:
    f = scene_cfg.get("fit", {})
    return FitConfig(
        iterations=int(iterations if iterations else f.get("iterations", 300)),
        lr=float(f.get("lr", 1e-2)),
        envelope_first_frac=float(f.get("envelope_first_frac", 0.5)),
        env_weight=float(f.get("env_weight", 1.0)),
        lambda_presence=float(f.get("lambda_presence", 1e-2)),
        lambda_scale=float(f.get("lambda_scale", 0.1)),
        lambda_overlap=float(f.get("lambda_overlap", 0.0)),
    )


def _well_conditioned_surfel(seed: int, rng_extra: np.random.Generator) -> SceneSpec:
    """Observable 1-surfel scene at the fixed live geometry: strong diffuse
    reflector (rho ~1.1, rough ~0.7) at >= 0.8 m from every node, so the
    echo survives the hardware quantization on most links."""
    rng = np.random.Generator(np.random.PCG64(seed ^ 0x51AB))
    nodes = _live_nodes().numpy()
    for _ in range(64):
        mu = np.array([rng.uniform(0.6, 2.2), rng.uniform(0.5, 2.2),
                       rng.uniform(0.4, 1.4)])
        if min(np.linalg.norm(mu - n) for n in nodes) >= 0.8:
            break
    p = PrimitiveSpec(SURFEL, mu, rotation_from_normal(np.array([0.0, 0.0, 1.0])),
                      np.array([0.3, 0.3, 0.3]), 1.1 + 0.3j, 0.7)
    return SceneSpec(room_id=seed, layout_id=seed, stage=1, seed=seed,
                     nodes=nodes, primitives=[p])


def run_v1(cfg: dict) -> dict:
    name = cfg["name"]
    seed_all_cfg = int(cfg.get("seed", 0))
    rng = np.random.Generator(np.random.PCG64(seed_all_cfg))
    kernel = _kernel()
    nodes = _live_nodes()
    links = directed_links(5)
    scene_cfg = {
        "stage": 1, "scenes": 1, "node_mode": "fixed_live", "jitter_m": 0.0,
        "room": {"x_range": [4, 6], "y_range": [4, 6], "z_max": [2.4, 3.0],
                 "partitions": [0, 0], "planes": False},
        "surfels": {"count": [0, 0]},
        "furniture": {"count": [0, 0]}, "people": {"count": [0, 0]}, "hw": {},
    }
    bounds = _bounds(nodes)
    restarts = int(cfg.get("restarts", 8))
    fcfg = _fit_cfg(cfg)

    results = []
    for i in range(int(cfg["n_scenes"])):
        seed = seed_all_cfg + i
        spec = _well_conditioned_surfel(seed, rng)
        truth = spec_to_scene(spec)
        rec = build_scene_pipeline(spec, scene_cfg, kernel)
        target, fp, gain_accum = _target_from_record(rec)
        target_t = torch.as_tensor(target, dtype=torch.complex128)
        fp_t = torch.as_tensor(fp, dtype=torch.float64)

        best = None
        restart_errs = []
        for k in range(restarts):
            init = init_random(rng, [SURFEL], bounds)
            res = fit_scene(target_t, fp_t, nodes, kernel, init, fcfg,
                            gain_accum=gain_accum)
            c_fit = res.scene.center[0].detach().numpy()
            err = float(np.linalg.norm(c_fit - truth.center[0].numpy()))
            restart_errs.append(err)
            print(f"  restart {k}: err {err:.3f} m loss {res.loss_trace[-1]:.4f} "
                  f"({res.runtime_s:.1f}s)", flush=True)
            if best is None or res.loss_trace[-1] < best["loss"]:
                best = {"loss": float(res.loss_trace[-1]), "err": err,
                        "restart": k, "runtime": res.runtime_s}
        ok = best["err"] < float(cfg.get("success_center_m", 0.10))
        results.append({
            "scene": i, "seed": seed, "surfel_center_err_m": best["err"],
            "best_restart": best["restart"], "final_loss": best["loss"],
            "runtime_s": best["runtime"], "restart_errs_m": restart_errs,
            "success": ok,
        })
        print(f"v1 scene {i}: err {best['err']:.3f} m, best restart "
              f"{best['restart']}/{restarts}, success={ok}", flush=True)
    return {"results": results,
            "success_rate": sum(r["success"] for r in results) / len(results)}


def _v2_scene(seed: int, n_planes_range) -> SceneSpec:
    rng = np.random.Generator(np.random.PCG64(seed))
    nodes = _live_nodes().numpy()
    n_planes = int(rng.integers(n_planes_range[0], n_planes_range[1] + 1))
    primitives = []
    for _ in range(n_planes):
        c = np.array([rng.uniform(0.6, 2.0), rng.uniform(0.4, 2.0),
                      rng.uniform(0.4, 1.6)])
        normal = rng.standard_normal(3)
        normal /= np.linalg.norm(normal)
        primitives.append(PrimitiveSpec(
            PLANE, c, rotation_from_normal(normal),
            np.array([rng.uniform(1.0, 3.0), rng.uniform(1.0, 3.0), 0.3]),
            0.5 + 0.2j * rng.standard_normal(), rng.uniform(0.1, 0.4)))
    for _ in range(64):
        mu = np.array([rng.uniform(0.6, 2.2), rng.uniform(0.5, 2.2),
                       rng.uniform(0.4, 1.4)])
        if min(np.linalg.norm(mu - n) for n in nodes) >= 0.8:
            break
    primitives.append(PrimitiveSpec(
        SURFEL, mu, rotation_from_normal(np.array([0.0, 0.0, 1.0])),
        np.array([0.3, 0.3, 0.3]), 1.1 + 0.3j, 0.7))
    return SceneSpec(room_id=seed, layout_id=seed, stage=4, seed=seed,
                     nodes=nodes, primitives=primitives)


def _plane_metrics(fit_scene_t: SceneTensors, truth: SceneTensors) -> dict:
    fit_idx = [g for g in range(fit_scene_t.type_id.numel())
               if int(fit_scene_t.type_id[g]) == PLANE]
    truth_idx = [g for g in range(truth.type_id.numel())
                 if int(truth.type_id[g]) == PLANE]
    n = min(len(fit_idx), len(truth_idx))
    norm_errs = []
    offset_errs = []
    used = set()
    for gi in fit_idx:
        n_fit = rot6d_to_matrix(fit_scene_t.rot6d[gi])[:, 2].detach().numpy()
        c_fit = fit_scene_t.center[gi].detach().numpy()
        best = None
        for gj in truth_idx:
            if gj in used:
                continue
            n_t = rot6d_to_matrix(truth.rot6d[gj])[:, 2].numpy()
            c_t = truth.center[gj].numpy()
            ang = float(np.degrees(np.arccos(np.clip(abs(n_fit @ n_t), 0, 1))))
            off = abs(float(n_t @ c_fit - n_t @ c_t))
            if best is None or (ang, off) < best[:2]:
                best = (ang, off, gj)
        if best is not None:
            norm_errs.append(best[0])
            offset_errs.append(best[1])
            used.add(best[2])
    return {"plane_normal_err_deg": norm_errs, "plane_offset_err_m": offset_errs}


def run_v2(cfg: dict) -> dict:
    seed0 = int(cfg.get("seed", 0))
    kernel = _kernel()
    nodes = _live_nodes()
    links = directed_links(5)
    bounds = _bounds(nodes)
    fcfg = _fit_cfg(cfg)
    scene_cfg = {
        "stage": 2, "scenes": 1, "node_mode": "fixed_live", "jitter_m": 0.0,
        "room": {"x_range": [4, 6], "y_range": [4, 6], "z_max": [2.4, 3.0],
                 "partitions": [0, 0], "planes": False},
        "surfels": {"count": [0, 0]},
        "furniture": {"count": [0, 0]}, "people": {"count": [0, 0]}, "hw": {},
    }
    held_out = int(cfg.get("held_out_links", 4))
    n_planes_range = cfg.get("n_planes", [2, 4])
    rng = np.random.Generator(np.random.PCG64(seed0 + 777))

    summaries = []
    for i in range(int(cfg["n_scenes"])):
        seed = seed0 + i
        spec = _v2_scene(seed, n_planes_range)
        truth = spec_to_scene(spec)
        rec = build_scene_pipeline(spec, scene_cfg, kernel)
        target, fp, gain_accum = _target_from_record(rec)
        target_t = torch.as_tensor(target, dtype=torch.complex128)
        fp_t = torch.as_tensor(fp, dtype=torch.float64)

        env = envelope_from_record(rec["cir_i16"], rec["dgc"], rec["accum"])
        link_mask = np.ones(len(links), dtype=bool)
        link_mask[-held_out:] = False

        inits = {"gt_perturbed": init_gt_perturbed(
            truth, rng, pos_m=float(cfg.get("init_pos_m", 0.05)),
            rot=float(np.radians(cfg.get("init_rot_deg", 5.0))))}
        if cfg.get("voting_init", True):
            peaks = extract_peaks(env, fp)
            candidates = vote(peaks, nodes.numpy(), links, fp)
            inits["voting"] = init_from_points(candidates) if len(candidates) else \
                init_random(rng, [SURFEL], bounds)

        for init_name, init in inits.items():
            res = fit_scene(target_t, fp_t, nodes, kernel, init, fcfg,
                            gain_accum=gain_accum, link_mask=link_mask)
            metrics = _plane_metrics(res.scene, truth)
            surf_idx = [g for g in range(res.scene.type_id.numel())
                        if int(res.scene.type_id[g]) == SURFEL]
            surf_err = None
            if surf_idx:
                c_fit = res.scene.center[surf_idx[0]].detach().numpy()
                c_t = truth.center[[g for g in range(truth.type_id.numel())
                                    if int(truth.type_id[g]) == SURFEL][0]].numpy()
                surf_err = float(np.linalg.norm(c_fit - c_t))
            held_out_env = float(np.median(res.per_link_env[-held_out:]))
            summary = {
                "scene": i, "init": init_name, "runtime_s": res.runtime_s,
                "final_loss": float(res.loss_trace[-1]),
                "plane_normal_err_deg": metrics["plane_normal_err_deg"],
                "plane_offset_err_m": metrics["plane_offset_err_m"],
                "surfel_center_err_m": surf_err,
                "held_out_link_env_residual": held_out_env,
                "envelope_trace": res.env_trace,
            }
            summaries.append(summary)
            print(f"v2 scene {i} [{init_name}]: planes "
                  f"n{metrics['plane_normal_err_deg']} "
                  f"o{metrics['plane_offset_err_m']} surfel "
                  f"{surf_err:.3f} m held-out env {held_out_env:.4f}")
    return {"results": summaries}


def main(argv: list = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment", choices=["v1", "v2"])
    ap.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    name = cfg["name"]
    out = RUNS / name
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    if args.experiment == "v1":
        result = run_v1(cfg)
    else:
        result = run_v2(cfg)
    elapsed = time.perf_counter() - t0

    with open(out / "summary.json", "w", encoding="utf-8") as f:
        json.dump({"experiment": name, "elapsed_s": elapsed, **result}, f, indent=2)
    print(f"wrote {out / 'summary.json'} in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
