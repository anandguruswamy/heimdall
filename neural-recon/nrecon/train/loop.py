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
from nrecon.sim.primitives import EMPTY, SceneTensors, rot6d_to_matrix
from nrecon.sim.pulse import correlation_kernel, make_template_v1
from nrecon.sim.render import (
    SurfelPulseLookup,
    build_surfel_pulse_lookup,
    render_scene,
    render_scene_batched,
)
from nrecon.train.data import ShardDataset, collate, to_device
from nrecon.train.losses import LossWeights, MatchWeights, set_loss, total_loss


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
    val_scenes: int = 16
    early_stop_patience: int = 4  # eval windows without improvement
    early_stop_min_delta: float = 1e-4
    max_steps: int = 0  # hard step cap; 0 = epochs * batches
    max_minutes: float = 0.0  # wall-clock cap; 0 disables
    device: str = "cpu"  # "cpu" or "cuda" (or "cuda:N"); model/kernel/batches moved here
    init_checkpoint: str = ""  # curriculum warm-start: load model weights only
                               # (fresh optimizer/step=0) from a PRIOR run's
                                # checkpoint.pt; ignored if this run already
                                # has its own checkpoint to resume from
    batched_renderer: bool = False  # opt in; scalar renderer remains the fallback
    surfel_pulse_backend: str = "exact"  # exact, bank-16x, or cache-1x-phase
    surfel_sigma_bins: int = 128
    surfel_phase_bins: int = 128
    surfel_sigma_max_ns: float = 15.0
    capsule_attenuation_backend: str = "legacy"  # legacy, compact, or gaussian
    train_render_links: int = 0  # 0 renders all directed links
    train_render_probability: float = 1.0
    train_render_batch_size: int = 0  # 0 renders every scene in the optimizer batch
    train_render_presence_threshold: float = 0.0
    compact_surfel_slots: bool = False
    match_rotation_weight: float = 0.5
    compile_model: bool = False
    compile_set_loss: bool = False
    matmul_precision: str = "highest"  # "high" enables TF32 on supported CUDA GPUs
    # Legacy defaults preserve existing checkpoint compatibility. Reduced
    # architectures opt in explicitly through training configs.
    model_d_model: int = 128
    model_heads: int = 4
    model_ffn: int = 1536
    model_encoder_blocks: int = 6
    model_decoder_blocks: int = 4
    model_queries: int = 48
    cache_prepared_dataset: bool = False
    replay_dataset_dir: str = ""
    replay_fraction: float = 0.0


class RunMonitor:
    """Health checks and early-stop bookkeeping for a training run."""

    def __init__(self, cfg: TrainConfig):
        self.cfg = cfg
        self.best = float("inf")
        self.plateau = 0
        self.stop_reason = None
        self.history = []

    def check_loss(self, total: float, step: int) -> str | None:
        """Returns a stop reason for a non-finite training loss."""
        if not (total == total) or total in (float("inf"), float("-inf")):
            self.stop_reason = f"non-finite loss {total} at step {step}"
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
              device=None, surfel_lookup: SurfelPulseLookup = None,
              set_loss_fn=None) -> dict:
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
            h_hat = render_predicted(pred, batch, kernel,
                                     batched=cfg.batched_renderer,
                                     surfel_lookup=None,
                                     capsule_attenuation_backend="compact")
            parts, (rows, cols) = total_loss(
                pred, batch["truth"], h_hat, batch["target"], batch["valid"],
                weights, return_matches=True, set_loss_fn=set_loss_fn)
            losses.append(float(parts["total"]))
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


SCENE_CENTER_CLAMP = 50.0  # m; well beyond any plausible room (PROVISIONAL safety bound)
SCENE_SCALE_LOG_MAX = 3.0  # log(20 m); matches RunMonitor's giant-surfel threshold


def pred_to_scene(pred: dict, batch: dict, dtype=torch.float32,
                  presence_threshold: float = 0.0) -> SceneTensors:
    """Network heads -> one SceneTensors per batch element.

    Clamps `center`/`scale_log` to a generous but finite range: the raw
    decoder heads for both are unconstrained linear outputs (paper Fig. 1
    heads have no activation there), and during early/unstable training
    they can occasionally take extreme values that blow up
    UWBRender's numerics (e.g. an enormous surfel scale making the
    Gaussian-broadening kernel width overflow to inf/nan -- hit this
    exactly, step ~100-120 of the first real run 2, 2026-08-05). Clamping
    here keeps the renderer numerically well-behaved without changing the
    network's own (unclamped) prediction used for the loss/matching.
    """
    b = pred["center"].shape[0]
    type_id = pred["type_logits"].argmax(-1).detach()
    if presence_threshold > 0.0:
        type_id = type_id.masked_fill(
            pred["presence"][..., 0].detach() < presence_threshold, EMPTY)
    type_id = type_id.cpu()
    scenes = []
    for bi in range(b):
        g = pred["center"].shape[1]
        scene = SceneTensors.empty(g, dtype=dtype)
        scene.type_id = type_id[bi]
        scene.presence = pred["presence"][bi, :, 0]
        # nan_to_num first: torch.clamp leaves actual NaN as NaN (only
        # bounds finite values), so a NaN prediction would otherwise still
        # reach the renderer.
        scene.center = torch.nan_to_num(
            pred["center"][bi], nan=0.0, posinf=SCENE_CENTER_CLAMP,
            neginf=-SCENE_CENTER_CLAMP).clamp(-SCENE_CENTER_CLAMP, SCENE_CENTER_CLAMP)
        scene.rot6d = torch.nan_to_num(pred["rot6d"][bi], nan=0.0,
                                       posinf=1.0, neginf=-1.0)
        scene.scale_log = torch.nan_to_num(
            pred["scale_log"][bi], nan=SCENE_SCALE_LOG_MAX,
            posinf=SCENE_SCALE_LOG_MAX,
            neginf=-SCENE_SCALE_LOG_MAX).clamp(max=SCENE_SCALE_LOG_MAX)
        rho = torch.complex(pred["rho"][bi, :, 0], pred["rho"][bi, :, 1])
        scene.rho = rho
        scene.roughness = pred["roughness"][bi, :, 0]
        scene.atten = pred["atten"][bi, :, 0]
        scene.dynamic_p = pred["dynamic"][bi, :, 0]
        scenes.append(scene)
    return scenes


def pred_to_batched_scene(pred: dict, dtype=torch.float32,
                          presence_threshold: float = 0.0) -> SceneTensors:
    """Network heads -> one SceneTensors whose fields retain [B,G,...]."""
    center = torch.nan_to_num(
        pred["center"], nan=0.0, posinf=SCENE_CENTER_CLAMP,
        neginf=-SCENE_CENTER_CLAMP).clamp(-SCENE_CENTER_CLAMP, SCENE_CENTER_CLAMP)
    rot6d = torch.nan_to_num(pred["rot6d"], nan=0.0, posinf=1.0, neginf=-1.0)
    scale_log = torch.nan_to_num(
        pred["scale_log"], nan=SCENE_SCALE_LOG_MAX,
        posinf=SCENE_SCALE_LOG_MAX,
        neginf=-SCENE_SCALE_LOG_MAX).clamp(max=SCENE_SCALE_LOG_MAX)
    complex_dtype = torch.complex128 if dtype == torch.float64 else torch.complex64
    type_id = pred["type_logits"].argmax(-1)
    if presence_threshold > 0.0:
        type_id = type_id.masked_fill(
            pred["presence"][..., 0].detach() < presence_threshold, EMPTY)
    return SceneTensors(
        type_id=type_id,
        presence=pred["presence"][..., 0],
        center=center.to(dtype),
        rot6d=rot6d.to(dtype),
        scale_log=scale_log.to(dtype),
        rho=torch.complex(pred["rho"][..., 0], pred["rho"][..., 1]).to(complex_dtype),
        roughness=pred["roughness"][..., 0].to(dtype),
        atten=pred["atten"][..., 0].to(dtype),
        dynamic_p=pred["dynamic"][..., 0].to(dtype),
    )


def render_predicted(pred: dict, batch: dict, kernel: torch.Tensor,
                     dtype=torch.float32, batched: bool = False,
                      surfel_lookup: SurfelPulseLookup = None,
                      capsule_attenuation_backend: str = "legacy",
                      links=None, presence_threshold: float = 0.0,
                      compact_surfel_slots: bool = False) -> torch.Tensor:
    """Render predicted scenes -> [B, L, 64] complex (LOS at 0)."""
    if batched:
        scene = pred_to_batched_scene(pred, dtype, presence_threshold)
        return render_scene_batched(
            scene, batch["node_pos"].to(dtype), kernel.to(dtype),
            surfel_lookup=surfel_lookup,
            capsule_attenuation_backend=capsule_attenuation_backend,
            links=links) / 4.0
    b = pred["center"].shape[0]
    scenes = pred_to_scene(pred, batch, dtype, presence_threshold)
    outs = []
    for bi in range(b):
        h = render_scene(scenes[bi], batch["node_pos"][bi], kernel.to(dtype),
                          surfel_lookup=surfel_lookup,
                          capsule_attenuation_backend=capsule_attenuation_backend,
                          compact_surfel_slots=compact_surfel_slots,
                          skip_zero_presence=False, links=links)
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
    torch.set_float32_matmul_precision(cfg.matmul_precision)
    kernel = _kernel().to(device)
    if cfg.surfel_pulse_backend == "exact":
        surfel_lookup = None
    else:
        surfel_lookup = build_surfel_pulse_lookup(
            kernel.to(torch.float32), cfg.surfel_pulse_backend,
            sigma_bins=cfg.surfel_sigma_bins,
            phase_bins=cfg.surfel_phase_bins,
            sigma_max_ns=cfg.surfel_sigma_max_ns)
    amp_enabled = bool(cfg.amp and device.type == "cuda")

    train_ds = ShardDataset(cfg.dataset_dir, "train", kernel.cpu(),
                            permute_labels=cfg.permute_labels, seed=cfg.seed,
                            cache_prepared=cfg.cache_prepared_dataset)
    val_ds = ShardDataset(
        cfg.dataset_dir, "val", kernel.cpu(), seed=cfg.seed + 1,
        cache_prepared=cfg.cache_prepared_dataset)
    if not 0.0 <= cfg.replay_fraction < 1.0:
        raise ValueError("replay_fraction must be in [0,1)")
    replay_count = round(cfg.batch_size * cfg.replay_fraction)
    if replay_count and not cfg.replay_dataset_dir:
        raise ValueError("replay_dataset_dir is required when replay_fraction > 0")
    replay_ds = None if not replay_count else ShardDataset(
        cfg.replay_dataset_dir, "train", kernel.cpu(), seed=cfg.seed + 2,
        cache_prepared=cfg.cache_prepared_dataset)
    current_samples_per_batch = cfg.batch_size - replay_count
    if current_samples_per_batch <= 0:
        raise ValueError("replay_fraction leaves no current-stage samples")
    base_model = HeimdallSetNet.from_config(cfg).to(device).train()
    model = base_model
    optim = torch.optim.AdamW(base_model.parameters(), lr=cfg.lr, weight_decay=cfg.wd)

    start_step = 0
    best_val_center = float("inf")
    val_plateau = 0
    ckpt = out / "checkpoint.pt"
    best_ckpt = out / "best_checkpoint.pt"
    if ckpt.exists():
        # weights_only=False: PyTorch >=2.6 defaults torch.load to
        # weights_only=True, which cannot unpickle our checkpoint (its
        # "config" dict, saved from cfg.__dict__, trips the stricter
        # unpickler on a numpy scalar). Our own checkpoints are trusted
        # (never loaded from an untrusted source).
        state = torch.load(ckpt, map_location="cpu", weights_only=False)
        base_model.load_state_dict(state["model"])
        optim.load_state_dict(state["optim"])
        _optimizer_state_to_device(optim, device)
        start_step = state["step"]
        best_val_center = state.get("best_val_center", best_val_center)
        val_plateau = state.get("val_plateau", 0)
        print(f"resumed from step {start_step}")
    elif cfg.init_checkpoint:
        init_state = torch.load(cfg.init_checkpoint, map_location="cpu",
                                weights_only=False)
        base_model.load_state_dict(init_state["model"])
        print(f"curriculum warm-start: loaded weights from "
              f"{cfg.init_checkpoint} (its step {init_state['step']}); "
               f"optimizer and step count start fresh")

    if cfg.compile_model:
        model = torch.compile(base_model)
    compiled_set_loss = torch.compile(set_loss) if cfg.compile_set_loss else None

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

    def save_checkpoint(path: Path, checkpoint_step: int, val=None) -> None:
        torch.save({
            "model": base_model.state_dict(),
            "optim": optim.state_dict(),
            "step": checkpoint_step,
            "config": cfg.__dict__,
            "best_val_center": best_val_center,
            "val_plateau": val_plateau,
            "validation": val,
        }, path)
    last_log = time.perf_counter()
    run_start = time.perf_counter()
    nonfinite_streak = 0
    NONFINITE_STREAK_LIMIT = 5
    parts = None  # guards the final return if every step fails immediately
    loss_ema = None  # for the loss-spike guard below
    LOSS_SPIKE_MULT = 8.0
    LOSS_SPIKE_ABS_FLOOR = 15.0  # only trip on a large absolute loss, not
                                 # relative noise once loss_ema is already small
    for epoch in range(cfg.epochs):
        if cfg.permute_labels:
            train_ds.permute_epoch()
        rng = np.random.default_rng(cfg.seed + epoch)
        rng.shuffle(indices)
        for bi in range(0, len(indices), current_samples_per_batch):
            if step >= total_steps:
                break
            if cfg.max_minutes and (time.perf_counter() - run_start) / 60.0 >= cfg.max_minutes:
                print(f"WALL-CLOCK CAP reached at step {step} "
                      f"({cfg.max_minutes} min)", flush=True)
                monitor.stop_reason = "max_minutes reached"
                break
            batch_idx = indices[bi:bi + current_samples_per_batch]
            if train_ds.permute_labels:
                samples = [train_ds.__getitem_permuted__(i) for i in batch_idx]
            else:
                samples = [train_ds[i] for i in batch_idx]
            if replay_ds is not None:
                replay_rng = np.random.default_rng(cfg.seed + 4_000_003 + step)
                replay_indices = replay_rng.choice(
                    len(replay_ds), size=replay_count, replace=False)
                samples.extend(replay_ds[int(i)] for i in replay_indices)
            batch = to_device(collate(samples), device)
            if not 0.0 < cfg.train_render_probability <= 1.0:
                raise ValueError("train_render_probability must be in (0,1]")
            physics_rng = np.random.default_rng(cfg.seed + 2_000_003 + step)
            compute_physics = physics_rng.random() < cfg.train_render_probability
            render_batch_indices = None
            render_batch = batch
            if compute_physics and cfg.train_render_batch_size:
                render_source_size = batch["x"].shape[0]
                if not 0 < cfg.train_render_batch_size <= render_source_size:
                    raise ValueError(
                        "train_render_batch_size must be in "
                        f"[1,{render_source_size}], got {cfg.train_render_batch_size}")
                batch_rng = np.random.default_rng(cfg.seed + 3_000_003 + step)
                selected = np.sort(batch_rng.choice(
                    render_source_size, size=cfg.train_render_batch_size,
                    replace=False))
                render_batch_indices = torch.as_tensor(
                    selected, dtype=torch.long, device=device)
                render_batch = {
                    key: value.index_select(0, render_batch_indices)
                    if torch.is_tensor(value) and value.shape[0] == render_source_size
                    else value
                    for key, value in batch.items()
                }
            all_links = directed_links(batch["node_pos"].shape[1])
            if compute_physics and cfg.train_render_links:
                if not 0 < cfg.train_render_links <= len(all_links):
                    raise ValueError(
                        f"train_render_links must be in [1,{len(all_links)}], "
                        f"got {cfg.train_render_links}")
                link_rng = np.random.default_rng(cfg.seed + 1_000_003 + step)
                link_indices_np = np.sort(link_rng.choice(
                    len(all_links), size=cfg.train_render_links, replace=False))
                render_links = [all_links[i] for i in link_indices_np]
                link_indices = torch.as_tensor(
                    link_indices_np, dtype=torch.long, device=device)
                render_target = render_batch["target"].index_select(1, link_indices)
                render_valid = render_batch["valid"].index_select(1, link_indices)
                sampling_probability = cfg.train_render_links / len(all_links)
                full_valid_count = render_batch["valid"].sum()
            elif compute_physics:
                render_links = None
                render_target = render_batch["target"]
                render_valid = render_batch["valid"]
                sampling_probability = 1.0
                full_valid_count = None
            else:
                render_links = None
                render_target = None
                render_valid = batch["valid"]
                sampling_probability = 1.0
                full_valid_count = None

            def closure(include_physics: bool):
                optim.zero_grad()
                with torch.autocast("cuda", enabled=amp_enabled):
                    pred = model(batch["x"], batch["geom"], batch["valid"])
                h_hat = None
                if include_physics:
                    render_pred = pred if render_batch_indices is None else {
                        key: value.index_select(0, render_batch_indices)
                        for key, value in pred.items()
                    }
                    h_hat = render_predicted(render_pred, render_batch, kernel,
                                             batched=cfg.batched_renderer,
                                             surfel_lookup=surfel_lookup,
                                              capsule_attenuation_backend=
                                              cfg.capsule_attenuation_backend,
                                              links=render_links,
                                              presence_threshold=
                                              cfg.train_render_presence_threshold,
                                              compact_surfel_slots=
                                              cfg.compact_surfel_slots)
                parts, matches = total_loss(
                    pred, batch["truth"], h_hat, render_target, render_valid,
                    weights, return_matches=True,
                    full_valid_count=full_valid_count,
                    sampling_probability=sampling_probability,
                    render_loss_scale=1.0 / cfg.train_render_probability,
                    match_weights=MatchWeights(rot=cfg.match_rotation_weight),
                    set_loss_fn=compiled_set_loss)
                total = parts["total"]
                scaler.scale(total).backward()
                return pred, parts, matches

            # Broad exception guard (defense in depth): an extreme/degenerate
            # predicted primitive can occasionally blow up UWBRender's
            # numerics in ways the nan_to_num/clamp in pred_to_scene doesn't
            # anticipate (hit a ValueError from an inf kernel-broadening
            # width at step ~100-120 of the first real run 2, 2026-08-05,
            # a step *after* one that had already passed the finite-loss
            # check below -- i.e. a fresh instability from a new batch's
            # geometry, not a lingering bad model state). Treat any
            # exception here the same as a non-finite loss rather than
            # crashing the whole (potentially many-hour, unattended) run.
            failure_reason = None
            try:
                pred, parts, matches = closure(compute_physics)
                total_val = float(parts["total"].detach())
                if not (total_val == total_val) or total_val in (float("inf"), float("-inf")):
                    failure_reason = f"non-finite loss {total_val}"
                elif loss_ema is not None and total_val > max(
                        LOSS_SPIKE_MULT * loss_ema, LOSS_SPIKE_ABS_FLOOR):
                    # Loss-spike guard: a finite-but-extreme gradient from a
                    # single pathological batch can still knock the model
                    # into a bad basin it never recovers from, even though
                    # gradient clipping bounds the applied step's norm --
                    # hit this exactly once the crash guards above stopped
                    # the run from dying outright: loss jumped ~4.9 -> ~228
                    # around step 126-140 of the first successfully-completed
                    # run 2 and never recovered, cascading the damage
                    # through the warm-started runs 3 and 4 (2026-08-05).
                    failure_reason = (
                        f"loss spike {total_val:.4f} > "
                        f"{LOSS_SPIKE_MULT}x EMA ({loss_ema:.4f}) / floor {LOSS_SPIKE_ABS_FLOOR}")
            except Exception as exc:  # noqa: BLE001 - intentionally broad, see comment above
                failure_reason = f"exception {exc!r}"
            if failure_reason is not None and compute_physics:
                failed_parts = " ".join(
                    f"{name}={float(value.detach()):.4f}"
                    for name, value in parts.items() if torch.is_tensor(value)) \
                    if parts is not None else "unavailable"
                print(f"PHYSICS STEP REJECTED at step {step + 1}: "
                      f"{failure_reason}; {failed_parts}; retrying set-only",
                      flush=True)
                try:
                    pred, parts, matches = closure(False)
                    total_val = float(parts["total"].detach())
                    if (total_val == total_val) and total_val not in (
                            float("inf"), float("-inf")) and (
                            loss_ema is None or total_val <= max(
                                LOSS_SPIKE_MULT * loss_ema, LOSS_SPIKE_ABS_FLOOR)):
                        failure_reason = None
                    else:
                        failure_reason = f"set-only retry loss {total_val}"
                except Exception as exc:  # noqa: BLE001 - same guarded step
                    failure_reason = f"set-only retry exception {exc!r}"
            if failure_reason is not None:
                nonfinite_streak += 1
                print(f"STEP FAILED at step {step + 1}: {failure_reason} "
                      f"(streak {nonfinite_streak}/{NONFINITE_STREAK_LIMIT}); "
                      f"skipping optimizer step", flush=True)
                optim.zero_grad(set_to_none=True)
                if nonfinite_streak >= NONFINITE_STREAK_LIMIT:
                    monitor.stop_reason = (
                        f"non-finite loss/exception for {nonfinite_streak} consecutive steps")
                    break
                continue
            scaler.unscale_(optim)
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), cfg.clip)
            if not torch.isfinite(grad_norm):
                nonfinite_streak += 1
                print(f"STEP FAILED at step {step + 1}: non-finite gradient norm "
                      f"{float(grad_norm)} (streak "
                      f"{nonfinite_streak}/{NONFINITE_STREAK_LIMIT}); skipping "
                      "optimizer step", flush=True)
                optim.zero_grad(set_to_none=True)
                scaler.update()
                if nonfinite_streak >= NONFINITE_STREAK_LIMIT:
                    monitor.stop_reason = (
                        f"non-finite gradient for {nonfinite_streak} consecutive steps")
                    break
                continue
            nonfinite_streak = 0
            loss_ema = total_val if loss_ema is None else 0.98 * loss_ema + 0.02 * total_val
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
                    rows, cols = matches
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
                val = evaluate(model, val_ds, kernel, weights, cfg, device=device,
                               surfel_lookup=surfel_lookup,
                               set_loss_fn=compiled_set_loss, n=cfg.val_scenes)
                val_writer.writerow([step, val["val_loss"], val["val_med_center"],
                                     time.perf_counter() - run_start])
                val_f.flush()
                print(f"val step {step}: loss {val['val_loss']:.4f} "
                      f"medCenter {val['val_med_center']:.3f}", flush=True)
                val_center = val["val_med_center"]
                if np.isfinite(val_center) and \
                        val_center < best_val_center - cfg.early_stop_min_delta:
                    best_val_center = val_center
                    val_plateau = 0
                    save_checkpoint(best_ckpt, step, val)
                    print(f"new best checkpoint: center {best_val_center:.3f} m",
                          flush=True)
                else:
                    val_plateau += 1
                    if cfg.early_stop_patience and \
                            val_plateau >= cfg.early_stop_patience:
                        monitor.stop_reason = (
                            f"validation center did not improve for "
                            f"{val_plateau} evaluations")
                        print(f"EARLY STOP: {monitor.stop_reason}", flush=True)

            if step % cfg.checkpoint_every == 0:
                save_checkpoint(ckpt, step)
            if monitor.stop_reason:
                break
        if monitor.stop_reason:
            break

    save_checkpoint(ckpt, step)
    log_f.close()
    final_loss = float(parts["total"].detach()) if parts is not None else float("nan")
    return {"steps": step, "final_loss": final_loss}
