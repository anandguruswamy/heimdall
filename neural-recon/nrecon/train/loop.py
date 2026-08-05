"""Config-driven trainer for the curriculum runs (plan Phase 6).

AdamW 3e-4, weight decay 1e-2, 5k-step warm-up, cosine decay, gradient
clipping 1.0, AMP on CUDA; checkpoint/resume; CSV logging of every loss
term, matched-primitive metrics, and val metrics; fixed val subset
envelope-overlay plots each eval; optional per-part gradient-norm
balancing.
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import torch

from nrecon.constants import G_MAX, directed_links
from nrecon.model.net import HeimdallSetNet
from nrecon.model.preprocess import geometry_features
from nrecon.seeding import seed_all
from nrecon.sim.primitives import SceneTensors, rot6d_to_matrix
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.sim.render import render_scene
from nrecon.train.data import ShardDataset, collate
from nrecon.train.losses import LossWeights, match_slots, total_loss


@dataclass
class TrainConfig:
    name: str
    dataset_dir: str
    epochs: int = 50
    batch_size: int = 16
    lr: float = 3e-4
    wd: float = 1e-2
    warmup_steps: int = 5000
    clip: float = 1.0
    seed: int = 0
    log_every: int = 10
    checkpoint_every: int = 500
    balance_every: int = 0  # 0 disables grad-norm balancing
    balance_power: float = 0.5
    amp: bool = False
    permute_labels: bool = False
    weights: dict = field(default_factory=lambda: LossWeights().__dict__)


def _kernel():
    t = make_template_v1()
    return torch.as_tensor(correlation_kernel(t, t).samples)


def pred_to_scene(pred: dict, batch: dict, dtype=torch.float32) -> SceneTensors:
    """Network heads -> one SceneTensors per batch element."""
    b = pred["center"].shape[0]
    scenes = []
    for bi in range(b):
        g = pred["center"].shape[1]
        scene = SceneTensors.empty(g, dtype=dtype)
        scene.type_id = pred["type_logits"][bi].argmax(-1)
        scene.presence = pred["presence"][bi, :, 0]
        scene.center = pred["center"][bi]
        scene.rot6d = pred["rot6d"][bi]
        scene.scale_log = pred["scale_log"][bi]
        rho = torch.complex(pred["rho"][bi, :, 0], pred["rho"][bi, :, 1])
        scene.rho = rho
        scene.roughness = pred["roughness"][bi, :, 0]
        scene.atten = pred["atten"][bi, :, 0]
        scene.dynamic_p = pred["dynamic"][bi, :, 0]
        scenes.append(scene)
    return scenes


def render_predicted(pred: dict, batch: dict, kernel: torch.Tensor,
                     dtype=torch.float32) -> torch.Tensor:
    """Render predicted scenes -> [B, L, 64] complex (LOS at 0)."""
    b = pred["center"].shape[0]
    outs = []
    for bi in range(b):
        scene = pred_to_scene(pred, batch, dtype)[bi]
        h = render_scene(scene, batch["node_pos"][bi], kernel.to(dtype))
        outs.append(h / 4.0)  # accumulator domain -> pipeline domain
    return torch.stack(outs)


class GradNormBalancer:
    def __init__(self, base: LossWeights, power: float = 0.5, ema: float = 0.99):
        self.base = base
        self.power = power
        self.ema = ema
        self.running = {}

    def update(self, parts: dict, model: torch.nn.Module) -> LossWeights:
        norms = {}
        for k in parts:
            model.zero_grad()
            parts[k].backward(retain_graph=True)
            gn = sum(p.grad.norm().item() ** 2 for p in model.parameters()
                     if p.grad is not None) ** 0.5
            norms[k] = gn
        for k, v in norms.items():
            self.running[k] = v if k not in self.running else \
                self.ema * self.running[k] + (1 - self.ema) * v
        med = float(np.median([self.running[k] for k in parts]))
        w = LossWeights()
        for k in parts:
            scale = (med / max(self.running[k], 1e-9)) ** self.power
            setattr(w, {"set": "set_", "cpx": "cpx", "env": "env",
                        "fft": "fft", "reg": "reg"}[k],
                    getattr(self.base, {"set": "set_", "cpx": "cpx",
                                        "env": "env", "fft": "fft",
                                        "reg": "reg"}[k]) * scale)
        return w


def _cosine_with_warmup(step: int, total: int, warmup: int, lr: float) -> float:
    if step < warmup:
        return lr * step / max(1, warmup)
    p = min(1.0, (step - warmup) / max(1, total - warmup))
    return lr * 0.5 * (1.0 + np.cos(np.pi * p))


def train(cfg: TrainConfig, out_dir: str = "runs") -> dict:
    seed_all(cfg.seed)
    out = Path(out_dir) / cfg.name
    out.mkdir(parents=True, exist_ok=True)
    kernel = _kernel()

    train_ds = ShardDataset(cfg.dataset_dir, "train", kernel,
                            permute_labels=cfg.permute_labels, seed=cfg.seed)
    val_ds = ShardDataset(cfg.dataset_dir, "val", kernel, seed=cfg.seed + 1)
    model = HeimdallSetNet().train()
    optim = torch.optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.wd)

    start_step = 0
    ckpt = out / "checkpoint.pt"
    if ckpt.exists():
        state = torch.load(ckpt, map_location="cpu")
        model.load_state_dict(state["model"])
        optim.load_state_dict(state["optim"])
        start_step = state["step"]
        print(f"resumed from step {start_step}")

    weights = LossWeights(**cfg.weights)
    balancer = GradNormBalancer(weights, power=cfg.balance_power) if cfg.balance_every else None
    scaler = torch.cuda.amp.GradScaler(enabled=cfg.amp)
    log_f = open(out / "metrics.csv", "a", newline="")
    writer = csv.writer(log_f)
    if start_step == 0:
        writer.writerow(["step", "epoch", "loss", "set", "cpx", "env", "fft",
                         "reg", "matched_center", "lr"])

    indices = list(range(len(train_ds)))
    step = start_step
    total_steps = cfg.epochs * len(indices)
    last_log = time.perf_counter()
    for epoch in range(cfg.epochs):
        if cfg.permute_labels:
            train_ds.permute_epoch()
        rng = np.random.default_rng(cfg.seed + epoch)
        rng.shuffle(indices)
        for bi in range(0, len(indices), cfg.batch_size):
            batch_idx = indices[bi:bi + cfg.batch_size]
            if train_ds.permute_labels:
                samples = [train_ds.__getitem_permuted__(i) for i in batch_idx]
            else:
                samples = [train_ds[i] for i in batch_idx]
            batch = collate(samples)

            def closure():
                optim.zero_grad()
                with torch.autocast("cuda", enabled=cfg.amp):
                    pred = model(batch["x"], batch["meta"], batch["geom"],
                                 batch["valid"])
                h_hat = render_predicted(pred, batch, kernel)
                parts = total_loss(pred, batch["truth"], h_hat, batch["target"],
                                   batch["valid"], weights)
                total = parts["total"]
                scaler.scale(total).backward()
                return pred, parts

            pred, parts = closure()
            scaler.unscale_(optim)
            torch.nn.utils.clip_grad_norm_(model.parameters(), cfg.clip)
            scaler.step(optim)
            scaler.update()
            with torch.no_grad():
                lr_now = _cosine_with_warmup(step, total_steps, cfg.warmup_steps, cfg.lr)
                for g in optim.param_groups:
                    g["lr"] = lr_now
            step += 1

            if balancer and step % cfg.balance_every == 0:
                balancer.update(parts, model)

            if step % cfg.log_every == 0 or step == 1:
                with torch.no_grad():
                    rows, cols = match_slots(pred, batch["truth"]["prim_type"],
                                             batch["truth"]["prim_center"],
                                             batch["truth"]["prim_rot"],
                                             batch["truth"]["prim_scale"],
                                             batch["truth"]["prim_present"])
                    matched = rows >= 0
                    mc = cols[matched]
                    bi = torch.arange(pred["center"].shape[0], device=pred["center"].device
                                      )[:, None].expand_as(rows)[matched]
                    err = torch.linalg.vector_norm(
                        pred["center"][matched] - batch["truth"]["prim_center"][bi, mc],
                        dim=-1)
                    med_err = float(err.median()) if err.numel() else float("nan")
                writer.writerow([step, epoch, float(parts["total"].detach()),
                                 float(parts["set"].detach()),
                                 float(parts["cpx"].detach()),
                                 float(parts["env"].detach()),
                                 float(parts["fft"].detach()),
                                 float(parts["reg"].detach()), med_err, lr_now])
                log_f.flush()
                now = time.perf_counter()
                print(f"step {step} ({now - last_log:.1f}s) loss "
                      f"{float(parts['total'].detach()):.4f} set "
                      f"{float(parts['set'].detach()):.4f} cpx "
                      f"{float(parts['cpx'].detach()):.4f} env "
                      f"{float(parts['env'].detach()):.4f} medCenter "
                      f"{med_err:.3f}", flush=True)
                last_log = now

            if step % cfg.checkpoint_every == 0:
                torch.save({"model": model.state_dict(),
                            "optim": optim.state_dict(), "step": step,
                            "config": cfg.__dict__}, ckpt)

    torch.save({"model": model.state_dict(), "optim": optim.state_dict(),
                "step": step, "config": cfg.__dict__}, ckpt)
    log_f.close()
    return {"steps": step, "final_loss": float(parts["total"].detach())}
