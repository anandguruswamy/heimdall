"""Shard dataset and batched collate for the trainer.

Produces network inputs (preprocessed channels, metadata, geometry),
the ground-truth label dict, and the LOS-at-0 target CIRs for the
renderer-in-the-loop loss. Stage 4 supports per-epoch node-label
permutation augmentation (Algorithm 1 step 4).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch

from nrecon.constants import directed_links
from nrecon.model.preprocess import geometry_features, metadata_vector, preprocess_cirs
from nrecon.sim.export import read_shard
from nrecon.sim.quantize import from_i16
from nrecon.sim.delay import fractional_shift

G = 48


class ShardDataset:
    def __init__(self, dataset_dir: str, split: str, kernel: torch.Tensor,
                 permute_labels: bool = False, dtype=torch.float32,
                 seed: int = 0):
        self.dir = Path(dataset_dir)
        self.split = split
        self.kernel = kernel
        self.permute_labels = permute_labels
        self.dtype = dtype
        self.links = directed_links(5)
        self._load_manifest()
        self._load_shards()
        self.rng = np.random.default_rng(seed)

    def _load_manifest(self):
        self.manifest = []
        for mf in sorted(self.dir.glob("shard-*.manifest.jsonl")):
            for line in mf.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                if entry["split"] == self.split:
                    self.manifest.append(entry)

    def _load_shards(self):
        self.shards = {}
        for npz in sorted(self.dir.glob("shard-*.npz")):
            self.shards[npz.stem] = read_shard(npz)

    def _record(self, index: int) -> dict:
        shard = self.shards[f"shard-{index // 256:06d}"]
        row = index % 256
        return {k: v[row] for k, v in shard.items()}

    def __len__(self):
        return len(self.manifest)

    def __getitem__(self, i: int) -> dict:
        rec = self._record(self.manifest[i]["index"])
        return self._prepare(rec)

    def _prepare(self, rec: dict, perm: np.ndarray = None) -> dict:
        """One record -> (x, meta, geom, valid, truth, target)."""
        node_pos = rec["node_pos"]
        link_order = list(range(20))
        links = self.links
        if perm is not None:
            inv = np.argsort(perm)
            node_pos = node_pos[inv]
            links = [(int(perm[a]), int(perm[b])) for a, b in self.links]
        idx = np.asarray(link_order)

        cir = rec["cir_i16"][idx]
        dgc = rec["dgc"][idx]
        accum = rec["accum"][idx]
        fp = rec["fp_aligned"][idx]
        cfo = rec["cfo"][idx]
        tic = rec["t_in_cycle"][idx]
        valid = rec["link_valid"][idx]

        x = preprocess_cirs(cir, dgc, accum, fp, self.kernel).to(self.dtype)
        meta = metadata_vector(fp, dgc, accum, cfo, tic, valid).to(self.dtype)
        geom = geometry_features(node_pos, links).to(self.dtype)

        h = torch.as_tensor(from_i16(cir, dgc, accum))
        cplx = torch.complex64 if self.dtype == torch.float32 else torch.complex128
        target = fractional_shift(
            h.to(torch.complex128),
            -torch.as_tensor(fp, dtype=torch.float64)).to(cplx)

        truth = {
            "prim_type": torch.as_tensor(rec["prim_type"], dtype=torch.long),
            "prim_present": torch.as_tensor(rec["prim_present"], dtype=self.dtype),
            "prim_center": torch.as_tensor(rec["prim_center"], dtype=self.dtype),
            "prim_rot": torch.as_tensor(rec["prim_rot"], dtype=self.dtype),
            "prim_scale": torch.as_tensor(rec["prim_scale"], dtype=self.dtype),
            "prim_rho": torch.as_tensor(rec["prim_rho"], dtype=self.dtype),
        }
        return {"x": x, "meta": meta, "geom": geom,
                "valid": torch.as_tensor(valid, dtype=torch.bool),
                "truth": truth, "target": target,
                "node_pos": torch.as_tensor(node_pos, dtype=self.dtype)}

    def permute_epoch(self):
        """Draw a fresh node-label permutation per sample (stage 4)."""
        self.permutations = []
        for _ in range(len(self.manifest)):
            self.permutations.append(self.rng.permutation(5))

    def __getitem_permuted__(self, i: int) -> dict:
        rec = self._record(self.manifest[i]["index"])
        perm = self.permutations[i % len(self.permutations)]
        return self._prepare(rec, perm)


def collate(samples: list) -> dict:
    out = {}
    for k in ("x", "meta", "geom", "target", "node_pos"):
        out[k] = torch.stack([s[k] for s in samples])
    out["valid"] = torch.stack([s["valid"] for s in samples])
    t = samples[0]["truth"]
    out["truth"] = {k: torch.stack([s["truth"][k] for s in samples]) for k in t}
    return out


def to_device(batch: dict, device) -> dict:
    """Move every tensor in a collated batch (including the nested `truth`
    dict) to `device`. No-op copy when already resident there."""
    out = {}
    for k, v in batch.items():
        if isinstance(v, dict):
            out[k] = {kk: vv.to(device) for kk, vv in v.items()}
        elif torch.is_tensor(v):
            out[k] = v.to(device)
        else:
            out[k] = v
    return out
