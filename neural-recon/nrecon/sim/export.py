"""Dataset shard writer/reader with deterministic render-record pipeline.

One `.npz` per 256 scenes plus one `manifest.jsonl` line per scene. The
record pipeline is: sample scene -> render -> residual FIR -> align to the
first-path marker -> quantize to the hardware transport (to_i16). Every
step is deterministic given the scene seed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch

from nrecon.constants import G_MAX, N_NODES, S_TAPS, directed_links
from nrecon.sim.delay import fractional_shift
from nrecon.sim.hardware import (
    Nuisance,
    RESID_TAPS,
    apply_resid_fir,
    apply_reverb_tail,
    sample_nuisance,
)
from nrecon.sim.quantize import to_i16
from nrecon.sim.render import render_scene
from nrecon.sim.scenes import SceneSpec, sample_scene, spec_to_scene

SHARD_SIZE = 256

# Split by hash of the room seed: 80% train / 10% val / 10% test.
SPLIT_BUCKETS = {"train": 800, "val": 900}


def split_for_room(room_id: int) -> str:
    h = int(hashlib.sha256(str(room_id).encode()).hexdigest()[:8], 16) % 1000
    if h < SPLIT_BUCKETS["train"]:
        return "train"
    if h < SPLIT_BUCKETS["val"]:
        return "val"
    return "test"


def config_hash(cfg: dict) -> str:
    text = json.dumps(cfg, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _git_rev() -> str:
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def render_record(spec: SceneSpec, nuis: Nuisance, kernel: torch.Tensor,
                  noise_seed: int) -> dict:
    """Render + align + quantize one scene -> record arrays (numpy)."""
    scene = spec_to_scene(spec)
    nodes = torch.as_tensor(spec.nodes, dtype=torch.float64)
    gain = torch.as_tensor(nuis.gain, dtype=torch.float64)
    phase = torch.as_tensor(nuis.phase, dtype=torch.float64)
    noise_std = torch.as_tensor(nuis.noise_std, dtype=torch.float64)

    h = render_scene(scene, nodes, kernel, nuis_gain=gain, nuis_phase=phase,
                     noise_std=noise_std, noise_seed=noise_seed)
    h_np = h.numpy()

    # late-multipath/diffuse tail (channel effect), then residual FIR
    # (receiver-filter effect, applied downstream of the channel), then
    # alignment (marker + hardware peak offset)
    h_reverb = apply_reverb_tail(h_np, nuis.reverb_tail)
    h_fir = apply_resid_fir(h_reverb, nuis.resid_fir)
    align = torch.as_tensor(nuis.fp_taps + nuis.peak_offset, dtype=torch.float64)
    h_al = fractional_shift(torch.as_tensor(h_fir, dtype=torch.complex128), align).numpy()

    cir = to_i16(h_al, nuis.dgc, nuis.accum)
    cir[nuis.missing] = 0

    n = len(spec.primitives)
    labels = _labels(spec, n)
    return {
        "cir_i16": cir.astype(np.int16),
        "link_valid": (~nuis.missing).astype(bool),
        "fp_q10_6": np.round((nuis.fp_recorded + _cir_start(nuis.fp_taps)) * 64.0).astype(np.int32),
        "fp_aligned": (nuis.fp_taps + nuis.peak_offset).astype(np.float32),
        "cir_start": _cir_start(nuis.fp_taps).astype(np.int32),
        "dgc": nuis.dgc.astype(np.int8),
        "accum": nuis.accum.astype(np.int16),
        "cfo": nuis.cfo.astype(np.float32),
        "t_in_cycle": nuis.t_in_cycle.astype(np.float32),
        "node_pos": spec.nodes.astype(np.float32),
        "labels": labels,
        "nuisance": _nuisance_arrays(nuis),
    }


def _cir_start(fp_taps: np.ndarray) -> np.ndarray:
    return np.round(fp_taps).astype(np.int32) - 16


def _labels(spec: SceneSpec, n: int) -> dict:
    g = G_MAX
    prim_type = np.zeros(g, dtype=np.int8)
    prim_present = np.zeros(g, dtype=np.float32)
    prim_center = np.zeros((g, 3), dtype=np.float32)
    prim_rot = np.zeros((g, 3, 3), dtype=np.float32)
    prim_scale = np.zeros((g, 3), dtype=np.float32)
    prim_rho = np.zeros((g, 2), dtype=np.float32)
    prim_rough = np.zeros(g, dtype=np.float32)
    prim_atten = np.zeros(g, dtype=np.float32)
    prim_dynamic = np.zeros(g, dtype=np.float32)
    for i, p in enumerate(spec.primitives):
        prim_type[i] = p.type
        prim_present[i] = 1.0
        prim_center[i] = p.center
        prim_rot[i] = p.rot
        prim_scale[i] = p.scale
        prim_rho[i] = [p.rho.real, p.rho.imag]
        prim_rough[i] = p.rough
        prim_atten[i] = p.atten
        prim_dynamic[i] = p.dynamic
    return {
        "prim_type": prim_type, "prim_present": prim_present,
        "prim_center": prim_center, "prim_rot": prim_rot,
        "prim_scale": prim_scale, "prim_rho": prim_rho,
        "prim_rough": prim_rough, "prim_atten": prim_atten,
        "prim_dynamic": prim_dynamic,
    }


def _nuisance_arrays(nuis: Nuisance) -> dict:
    return {
        "link_gain": nuis.gain.astype(np.float32),
        "link_phase": nuis.phase.astype(np.float32),
        "noise_std": nuis.noise_std.astype(np.float32),
        "resid_fir": np.stack([nuis.resid_fir.real, nuis.resid_fir.imag], axis=-1).astype(np.float32),
        "reverb_tail": np.stack([nuis.reverb_tail.real, nuis.reverb_tail.imag],
                                axis=-1).astype(np.float32),
    }


def spec_from_record(rec: dict, scene_index: int) -> SceneSpec:
    """Rebuild a SceneSpec from stored label arrays (validator consistency)."""
    g = rec["prim_type"].shape[0]
    primitives = []
    from nrecon.sim.scenes import PrimitiveSpec

    for i in range(g):
        if rec["prim_present"][i] == 0:
            continue
        rot = rec["prim_rot"][i]
        rho = complex(rec["prim_rho"][i, 0], rec["prim_rho"][i, 1])
        primitives.append(PrimitiveSpec(
            type=int(rec["prim_type"][i]),
            center=rec["prim_center"][i],
            rot=rot,
            scale=rec["prim_scale"][i],
            rho=rho,
            rough=float(rec["prim_rough"][i]),
            atten=float(rec["prim_atten"][i]),
            dynamic=float(rec["prim_dynamic"][i]),
        ))
    return SceneSpec(room_id=scene_index, layout_id=scene_index, stage=0,
                     seed=scene_index, nodes=rec["node_pos"], primitives=primitives)


def nuisance_from_record(rec: dict) -> Nuisance:
    n = rec["link_gain"].shape[0]
    resid = rec["resid_fir"][..., 0] + 1j * rec["resid_fir"][..., 1]
    reverb = rec["reverb_tail"][..., 0] + 1j * rec["reverb_tail"][..., 1]
    return Nuisance(
        gain=rec["link_gain"], phase=rec["link_phase"], noise_std=rec["noise_std"],
        dgc=rec["dgc"], accum=rec["accum"], cfo=rec["cfo"],
        fp_taps=rec["fp_aligned"], fp_recorded=rec["fp_aligned"],
        peak_offset=np.zeros(n),
        resid_fir=resid, missing=~rec["link_valid"],
        t_in_cycle=rec["t_in_cycle"],
        reverb_tail=reverb,
    )


def write_shard(path: Path, records: list, manifest_lines: list) -> None:
    """Write one .npz shard plus its manifest lines (single file path base)."""
    arrays = {}
    for key in ("cir_i16", "link_valid", "fp_q10_6", "fp_aligned", "cir_start",
                "dgc", "accum", "cfo", "t_in_cycle", "node_pos"):
        arrays[key] = np.stack([r[key] for r in records])
    for lkey in ("prim_type", "prim_present", "prim_center", "prim_rot", "prim_scale",
                 "prim_rho", "prim_rough", "prim_atten", "prim_dynamic"):
        arrays[lkey] = np.stack([r["labels"][lkey] for r in records])
    for nkey in ("link_gain", "link_phase", "noise_std", "resid_fir", "reverb_tail"):
        arrays[nkey] = np.stack([r["nuisance"][nkey] for r in records])
    np.savez_compressed(path.with_suffix(".npz"), **arrays)
    with open(path.with_suffix(".manifest.jsonl"), "a", encoding="utf-8") as f:
        for line in manifest_lines:
            f.write(json.dumps(line) + "\n")


def read_shard(path: Path) -> dict:
    data = np.load(path.with_suffix(".npz"))
    return {k: data[k] for k in data.files}


def build_scene_pipeline(spec: SceneSpec, cfg: dict, kernel: torch.Tensor) -> dict:
    """One scene through the full record pipeline (used by builder/validator)."""
    links = directed_links(spec.nodes.shape[0])
    n_links = len(links)
    rng = np.random.Generator(np.random.PCG64(spec.seed + 10_000_003))
    nuis = sample_nuisance(rng, cfg, n_links)
    return render_record(spec, nuis, kernel, noise_seed=spec.seed)


def build_dataset(cfg: dict, out_dir: Path, kernel: torch.Tensor,
                  progress_cb=None, limit: int = None) -> list:
    """Build all scenes of a stage config into `out_dir`. Returns manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    n_seeds = limit if limit is not None else int(cfg["scenes"])
    layouts = int(cfg.get("layouts_per_scene", 1))
    total = n_seeds * layouts
    (out_dir / "config.yaml").write_text(
        json.dumps(cfg, indent=2, default=str), encoding="utf-8"
    )
    manifest = []
    records = []
    rev = _git_rev()

    pulse_manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "artifacts" / "pulse" / "manifest.json").read_text()
    )
    pulse_hash = pulse_manifest["kernel_sha256"]
    done = 0
    for i in range(n_seeds):
        seed = int(cfg.get("base_seed", 0)) + i
        for li in range(layouts):
            spec = sample_scene(seed, cfg, layout_index=li)
            rec = build_scene_pipeline(spec, cfg, kernel)
            records.append(rec)
            split = split_for_room(spec.room_id)
            manifest.append({
                "index": done, "scene_seed": seed, "layout_index": li,
                "room_id": spec.room_id, "layout_id": spec.layout_id,
                "stage": cfg["stage"], "config_hash": config_hash(cfg),
                "generator_git_rev": rev, "pulse_manifest_hash": pulse_hash,
                "split": split,
            })
            done += 1
            if len(records) == SHARD_SIZE:
                write_shard(out_dir / f"shard-{(done - 1) // SHARD_SIZE:06d}", records,
                            manifest[-len(records):])
                records = []
        if progress_cb:
            progress_cb(done, total)
    if records:
        write_shard(out_dir / f"shard-{(done - 1) // SHARD_SIZE:06d}", records,
                    manifest[-len(records):])
    return manifest
