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
from nrecon.train.data import ShardDataset, collate, to_device
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
    # monitoring / early stop
    eval_every: int = 50  # val evaluation cadence (steps); 0 disables
    early_stop_patience: int = 4  # eval windows without improvement
    early_stop_min_delta: float = 1e-4
    max_steps: int = 0  # hard step cap; 0 = epochs * batches
    max_minutes: float = 0.0  # wall-clock cap; 0 disables
    device: str = "cpu"  # "cpu" or "cuda" (or "cuda:N"); model/kernel/batches moved here
    init_checkpoint: str = ""  # curriculum warm-start: load model weights only
                               # (fresh optimizer/step=0) from a PRIOR run's
                               # checkpoint.pt; ignored if this run already
                               # has its own checkpoint to resume from


class RunMonitor:
    """Health checks and early-stop bookkeeping for a training run."""

    def __init__(self, cfg: TrainConfig):
        self.cfg = cfg
        self.best = float("inf")
        self.plateau = 0
        self.stop_reason = None
        self.history = []

    def check_loss(self, total: float, step: int) -> str | None:
        """Returns a stop reason or None. NaN/inf -> immediate stop."""
        if not (total == total) or total in (float("inf"), float("-inf")):
            self.stop_reason = f"non-finite loss {total} at step {step}"
            return self.stop_reason
        if total < self.best - self.cfg.early_stop_min_delta:
            self.best = total
            self.plateau = 0
        else:
            self.plateau += 1
            if self.cfg.early_stop_patience and \
                    self.plateau >= self.cfg.early_stop_patience:
                self.stop_reason = (f"loss plateau at {total:.4f} for "
                                    f"{self.plateau} eval windows")
                return self.stop_reason
        return None

    def check_degenerate(self, pred: dict) -> str | None:
        """Presence collapse / giant-surfel degeneracy (plan Phase 6 step 5)."""
        p = pred["presence"].detach()
        if float(p.mean()) < 0.05:
            return "presence collapse (mean presence < 0.05)"
        s = torch.exp(pred["scale_log"].detach())
        if float(s.max()) > 20.0:
            return "giant-surfel runaway (max scale > 20 m)"
        return None

    def record(self, step: int, total: float, med_center: float) -> None:
        self.history.append({"step": step, "loss": total, "med_center": med_center})


def evaluate(model: torch.nn.Module, ds: ShardDataset, kernel: torch.Tensor,
             weights: LossWeights, cfg: TrainConfig, n: int = 16,
             device=None) -> dict:
    """Mean loss and matched-center error on a fixed val subset."""
    model.eval()
    losses = []
    centers = []
    with torch.no_grad():
        for i in range(min(n, len(ds))):
            sample = ds[i]
            batch = collate([sample])
            if device is not None:
                batch = to_device(batch, device)
            pred = model(batch["x"], batch["geom"], batch["valid"])
            h_hat = render_predicted(pred, batch, kernel)
            parts = total_loss(pred, batch["truth"], h_hat, batch["target"],
                               batch["valid"], weights)
            losses.append(float(parts["total"]))
            rows, cols = match_slots(pred, batch["truth"]["prim_type"],
                                     batch["truth"]["prim_center"],
                                     batch["truth"]["prim_rot"],
                                     batch["truth"]["prim_scale"],
                                     batch["truth"]["prim_present"])
            matched = rows >= 0
            if matched.any():
                mc = cols[matched]
                bi = torch.arange(pred["center"].shape[0], device=pred["center"].device
                                  )[:, None].expand_as(rows)[matched]
                err = torch.linalg.vector_norm(
                    pred["center"][matched] - batch["truth"]["prim_center"][bi, mc], dim=-1)
                centers.append(float(err.median()))
    model.train()
    return {"val_loss": float(np.mean(losses)),
            "val_med_center": float(np.median(centers)) if centers else float("nan")}


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


def _optimizer_state_to_device(optim: torch.optim.Optimizer, device) -> None:
    """`optim.load_state_dict` restores tensor state (e.g. Adam moments) on
    whatever device `torch.load` used (we load with map_location="cpu" for
    portability); move it onto `device` to match the (already-moved) model
    parameters, or the first `optim.step()` raises a device-mismatch error."""
    for state in optim.state.values():
        for k, v in state.items():
            if torch.is_tensor(v):
                state[k] = v.to(device)


def train(cfg: TrainConfig, out_dir: str = "runs") -> dict:
    seed_all(cfg.seed)
    out = Path(out_dir) / cfg.name
    out.mkdir(parents=True, exist_ok=True)
    device = torch.device(cfg.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError(
            f"device={cfg.device!r} requested but torch.cuda.is_available() is False")
    kernel = _kernel().to(device)
    amp_enabled = bool(cfg.amp and device.type == "cuda")

    train_ds = ShardDataset(cfg.dataset_dir, "train", kernel.cpu(),
                            permute_labels=cfg.permute_labels, seed=cfg.seed)
    val_ds = ShardDataset(cfg.dataset_dir, "val", kernel.cpu(), seed=cfg.seed + 1)
    model = HeimdallSetNet().to(device).train()
    optim = torch.optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.wd)

    start_step = 0
    ckpt = out / "checkpoint.pt"
    if ckpt.exists():
        # weights_only=False: PyTorch >=2.6 defaults torch.load to
        # weights_only=True, which cannot unpickle our checkpoint (its
        # "config" dict, saved from cfg.__dict__, trips the stricter
        # unpickler on a numpy scalar). Our own checkpoints are trusted
        # (never loaded from an untrusted source).
        state = torch.load(ckpt, map_location="cpu", weights_only=False)
        model.load_state_dict(state["model"])
        optim.load_state_dict(state["optim"])
        _optimizer_state_to_device(optim, device)
        start_step = state["step"]
        print(f"resumed from step {start_step}")
    elif cfg.init_checkpoint:
        init_state = torch.load(cfg.init_checkpoint, map_location="cpu",
                                weights_only=False)
        model.load_state_dict(init_state["model"])
        print(f"curriculum warm-start: loaded weights from "
              f"{cfg.init_checkpoint} (its step {init_state['step']}); "
              f"optimizer and step count start fresh")

    weights = LossWeights(**cfg.weights)
    balancer = GradNormBalancer(weights, power=cfg.balance_power) if cfg.balance_every else None
    scaler = torch.amp.GradScaler("cuda", enabled=amp_enabled)
    monitor = RunMonitor(cfg)
    log_f = open(out / "metrics.csv", "a", newline="")
    writer = csv.writer(log_f)
    if start_step == 0:
        writer.writerow(["step", "epoch", "loss", "set", "cpx", "env", "fft",
                         "reg", "matched_center", "lr", "wall_s"])
    val_f = open(out / "val.csv", "a", newline="")
    val_writer = csv.writer(val_f)
    if start_step == 0:
        val_writer.writerow(["step", "val_loss", "val_med_center", "wall_s"])

    indices = list(range(len(train_ds)))
    step = start_step
    total_steps = cfg.max_steps or (cfg.epochs * len(indices))
    last_log = time.perf_counter()
    run_start = time.perf_counter()
    for epoch in range(cfg.epochs):
        if cfg.permute_labels:
            train_ds.permute_epoch()
        rng = np.random.default_rng(cfg.seed + epoch)
        rng.shuffle(indices)
        for bi in range(0, len(indices), cfg.batch_size):
            if step >= total_steps:
                break
            if cfg.max_minutes and (time.perf_counter() - run_start) / 60.0 >= cfg.max_minutes:
                print(f"WALL-CLOCK CAP reached at step {step} "
                      f"({cfg.max_minutes} min)", flush=True)
                monitor.stop_reason = "max_minutes reached"
                break
            batch_idx = indices[bi:bi + cfg.batch_size]
            if train_ds.permute_labels:
                samples = [train_ds.__getitem_permuted__(i) for i in batch_idx]
            else:
                samples = [train_ds[i] for i in batch_idx]
            batch = to_device(collate(samples), device)

            def closure():
                optim.zero_grad()
                with torch.autocast("cuda", enabled=amp_enabled):
                    pred = model(batch["x"], batch["geom"], batch["valid"])
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
                wall_s = time.perf_counter() - run_start
                writer.writerow([step, epoch, float(parts["total"].detach()),
                                 float(parts["set"].detach()),
                                 float(parts["cpx"].detach()),
                                 float(parts["env"].detach()),
                                 float(parts["fft"].detach()),
                                 float(parts["reg"].detach()), med_err, lr_now,
                                 wall_s])
                log_f.flush()
                now = time.perf_counter()
                print(f"step {step} ({now - last_log:.1f}s) loss "
                      f"{float(parts['total'].detach()):.4f} set "
                      f"{float(parts['set'].detach()):.4f} cpx "
                      f"{float(parts['cpx'].detach()):.4f} env "
                      f"{float(parts['env'].detach()):.4f} medCenter "
                      f"{med_err:.3f}", flush=True)
                last_log = now
                stop = monitor.check_loss(float(parts["total"].detach()), step)
                if stop:
                    print(f"EARLY STOP: {stop}", flush=True)
                    break
                degen = monitor.check_degenerate(pred)
                if degen:
                    print(f"DEGENERATE: {degen}", flush=True)

            if cfg.eval_every and step % cfg.eval_every == 0:
                val = evaluate(model, val_ds, kernel, weights, cfg, device=device)
                val_writer.writerow([step, val["val_loss"], val["val_med_center"],
                                     time.perf_counter() - run_start])
                val_f.flush()
                print(f"val step {step}: loss {val['val_loss']:.4f} "
                      f"medCenter {val['val_med_center']:.3f}", flush=True)

            if step % cfg.checkpoint_every == 0:
                torch.save({"model": model.state_dict(),
                            "optim": optim.state_dict(), "step": step,
                            "config": cfg.__dict__}, ckpt)
        if monitor.stop_reason:
            break

    torch.save({"model": model.state_dict(), "optim": optim.state_dict(),
                "step": step, "config": cfg.__dict__}, ckpt)
    log_f.close()
    return {"steps": step, "final_loss": float(parts["total"].detach())}
