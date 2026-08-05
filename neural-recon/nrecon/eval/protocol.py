"""Evaluation protocol entry point (plan Phase 7 step 2).

Scope note (2026-08-05, PROVISIONAL/pragmatic first pass): implements the
"network" system only (a trained `HeimdallSetNet` checkpoint) over one
named test set, reporting the primitive-recovery and held-out-link
physical-consistency metrics from `nrecon.eval.metrics`, plus per-scene
runtime. Deferred for a later pass: the other systems ("backprojection",
"voting", "optimizer-random", "optimizer-voting", "hybrid"), the
node-geometry/SNR/missing-link/path-order stratification, and the
seen/unseen room-topology split -- see `reports/N7-evaluation.md` for
what was actually run.

Usage: python -m nrecon.eval.protocol --checkpoint runs/train-run4/checkpoint.pt
       --dataset datasets/eval-test-fixed --out reports/eval-test-fixed.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch

from nrecon.eval.metrics import held_out_link_consistency, primitive_recovery_metrics
from nrecon.model.net import HeimdallSetNet
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.train.data import ShardDataset, collate, to_device
from nrecon.train.loop import render_predicted


class EvalDataset(ShardDataset):
    """Like `ShardDataset` but includes every record regardless of its
    train/val/test split label -- appropriate for a purpose-built
    evaluation set whose seeds are disjoint from every training stage
    (the split label is meaningless for it)."""

    def _load_manifest(self):
        self.manifest = []
        for mf in sorted(self.dir.glob("shard-*.manifest.jsonl")):
            for line in mf.read_text(encoding="utf-8").splitlines():
                self.manifest.append(json.loads(line))


def _kernel():
    t = make_template_v1()
    return torch.as_tensor(correlation_kernel(t, t).samples)


def load_network(checkpoint: str, device: torch.device) -> HeimdallSetNet:
    state = torch.load(checkpoint, map_location="cpu", weights_only=False)
    model = HeimdallSetNet().to(device)
    model.load_state_dict(state["model"])
    model.eval()
    return model


def evaluate_network(checkpoint: str, dataset_dir: str, device: str = "cpu",
                     n: int = None, held_out_k: int = 4, seed: int = 0) -> dict:
    """Evaluate one "network" system over a named test set (plan Phase 7
    step 2). Returns a dict with primitive-recovery and held-out-link
    metrics plus timing, ready to serialize."""
    dev = torch.device(device)
    kernel = _kernel()
    model = load_network(checkpoint, dev)
    ds = EvalDataset(dataset_dir, split="__all__", kernel=kernel, seed=seed)
    n = len(ds) if n is None else min(n, len(ds))
    rng = np.random.default_rng(seed)

    prim_results = []
    held_out_results = []
    runtimes = []
    with torch.no_grad():
        for i in range(n):
            sample = ds[i]
            batch = collate([sample])
            batch_dev = to_device(batch, dev)

            t0 = time.perf_counter()
            pred = model(batch_dev["x"], batch_dev["geom"], batch_dev["valid"])
            runtimes.append(time.perf_counter() - t0)
            prim_results.append(primitive_recovery_metrics(pred, batch_dev["truth"]))

            # Held-out-link physical consistency: additionally mask
            # held_out_k links from the network's input (on top of any
            # already-missing links), re-run, render the predicted scene
            # for every link, and compare against the target only on the
            # newly-withheld links.
            valid_np = batch["valid"][0].numpy().copy()
            candidates = np.nonzero(valid_np)[0]
            k = min(held_out_k, len(candidates))
            if k > 0:
                held = rng.choice(candidates, size=k, replace=False)
                valid_ho = batch["valid"].clone()
                valid_ho[0, held] = False
                pred_ho = model(batch_dev["x"], batch_dev["geom"], valid_ho.to(dev))
                h_hat = render_predicted(pred_ho, batch_dev, kernel.to(dev))
                held_mask = torch.zeros_like(batch["valid"])
                held_mask[0, held] = True
                held_out_results.append(
                    held_out_link_consistency(h_hat, batch_dev["target"], held_mask.to(dev)))

    agg = _aggregate_primitive(prim_results)
    agg_ho = _aggregate_held_out(held_out_results)
    return {
        "checkpoint": checkpoint,
        "dataset_dir": dataset_dir,
        "n_scenes": n,
        "device": device,
        "mean_runtime_s": float(np.mean(runtimes)) if runtimes else float("nan"),
        "primitive_recovery": agg,
        "held_out_link_consistency": agg_ho,
    }


def _aggregate_primitive(results: list) -> dict:
    if not results:
        return {}
    totals = {"n_truth": 0, "n_matched": 0, "n_pred_unmatched": 0, "type_correct": 0}
    lists = {k: [] for k in (
        "plane_normal_err_deg", "plane_offset_err_m", "surfel_center_err_m",
        "surfel_cov_frobenius_err", "capsule_center_err_m", "capsule_halflen_err_m",
        "capsule_radius_err_m")}
    for r in results:
        totals["n_truth"] += r.n_truth
        totals["n_matched"] += r.n_matched
        totals["n_pred_unmatched"] += r.n_pred_unmatched
        totals["type_correct"] += r.type_correct
        for k in lists:
            lists[k].extend(getattr(r, k))

    def _stat(vals):
        if not vals:
            return {"median": float("nan"), "mean": float("nan"), "n": 0}
        arr = np.asarray(vals)
        return {"median": float(np.median(arr)), "mean": float(arr.mean()), "n": len(arr)}

    return {
        **totals,
        "recall": totals["n_matched"] / totals["n_truth"] if totals["n_truth"] else float("nan"),
        "type_accuracy": totals["type_correct"] / totals["n_matched"] if totals["n_matched"] else float("nan"),
        **{k: _stat(v) for k, v in lists.items()},
    }


def _aggregate_held_out(results: list) -> dict:
    complex_err = [v for r in results for v in r.complex_err]
    envelope_err = [v for r in results for v in r.envelope_err]

    def _stat(vals):
        if not vals:
            return {"median": float("nan"), "mean": float("nan"), "n": 0}
        arr = np.asarray(vals)
        return {"median": float(np.median(arr)), "mean": float(arr.mean()), "n": len(arr)}

    return {"complex_err": _stat(complex_err), "envelope_err": _stat(envelope_err)}


def main(argv: list = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--held-out-k", type=int, default=4)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    result = evaluate_network(args.checkpoint, args.dataset, device=args.device,
                              n=args.n, held_out_k=args.held_out_k)
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
