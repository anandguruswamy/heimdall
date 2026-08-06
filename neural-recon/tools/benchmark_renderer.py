"""Benchmark complete GPU training steps with scalar and batched UWBRender."""

from __future__ import annotations

import argparse
import statistics
import time

import torch
from torch.profiler import ProfilerActivity, profile, record_function

from nrecon.constants import directed_links
from nrecon.model.net import HeimdallSetNet
from nrecon.seeding import seed_all
from nrecon.train.data import ShardDataset, collate, to_device
from nrecon.sim.render import build_surfel_pulse_lookup, render_scene
from nrecon.train.loop import _kernel, pred_to_scene, render_predicted
from nrecon.train.losses import (
    LossWeights,
    MatchWeights,
    match_slots,
    set_loss,
    total_loss,
)


def _training_step(model, optimizer, batch, kernel, weights,
                   batched: bool, rebuild_scenes: bool, surfel_lookup,
                   capsule_backend: str, render_links, render_target,
                   render_valid, full_valid_count, sampling_probability,
                   compute_physics: bool = True,
                   match_rotation_weight: float = 0.5,
                   set_loss_fn=None, presence_threshold: float = 0.0,
                   compact_surfel_slots: bool = False,
                   render_batch_size: int = 0) -> None:
    optimizer.zero_grad(set_to_none=True)
    with record_function("model_forward"):
        pred = model(batch["x"], batch["geom"], batch["valid"])
    with record_function("renderer_forward"):
        render_pred = pred
        render_batch = batch
        if compute_physics and render_batch_size:
            index = torch.arange(render_batch_size, device=pred["center"].device)
            render_pred = {key: value.index_select(0, index) for key, value in pred.items()}
            render_batch = {
                key: value.index_select(0, index)
                if torch.is_tensor(value) and value.shape[0] == pred["center"].shape[0]
                else value
                for key, value in batch.items()
            }
            render_target = render_target.index_select(0, index)
            render_valid = render_valid.index_select(0, index)
        if not compute_physics:
            h_hat = None
        elif rebuild_scenes:
            outs = []
            for bi in range(pred["center"].shape[0]):
                scene = pred_to_scene(pred, batch)[bi]
                outs.append(render_scene(
                    scene, batch["node_pos"][bi], kernel,
                    surfel_lookup=surfel_lookup,
                    capsule_attenuation_backend=capsule_backend,
                    links=render_links) / 4.0)
            h_hat = torch.stack(outs)
        else:
            h_hat = render_predicted(render_pred, render_batch, kernel, batched=batched,
                                     surfel_lookup=surfel_lookup,
                                     capsule_attenuation_backend=capsule_backend,
                                     links=render_links,
                                     presence_threshold=presence_threshold,
                                     compact_surfel_slots=compact_surfel_slots)
    with record_function("loss_forward"):
        parts = total_loss(
            pred, batch["truth"], h_hat, render_target, render_valid, weights,
            full_valid_count=full_valid_count,
            sampling_probability=sampling_probability,
            match_weights=MatchWeights(rot=match_rotation_weight),
            set_loss_fn=set_loss_fn)
    with record_function("backward"):
        parts["total"].backward()
    with record_function("optimizer"):
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()


def benchmark(dataset_dir: str, batch_size: int, batched: bool, rebuild_scenes: bool,
              warmup: int, steps: int, profile_step: bool = False,
              surfel_backend: str = "exact", sigma_bins: int = 128,
              phase_bins: int = 128, sigma_max_ns: float = 15.0,
              capsule_backend: str = "legacy", render_link_count: int = 0,
              skip_physics: bool = False,
               match_rotation_weight: float = 0.5,
               freeze_model: bool = False, compile_model: bool = False,
               compile_loss: bool = False, matmul_precision: str = "highest",
               model_d_model: int = 128, model_heads: int = 4,
               model_ffn: int = 1536, model_encoder_blocks: int = 6,
               model_decoder_blocks: int = 4, model_queries: int = 48,
               checkpoint: str = "", presence_threshold: float = 0.0,
               compact_surfel_slots: bool = False,
               render_batch_size: int = 0) -> dict:
    seed_all(0)
    torch.set_float32_matmul_precision(matmul_precision)
    device = torch.device("cuda")
    kernel = _kernel().to(device)
    torch.cuda.synchronize()
    build_start = time.perf_counter()
    surfel_lookup = None if surfel_backend == "exact" else build_surfel_pulse_lookup(
        kernel.to(torch.float32), surfel_backend, sigma_bins=sigma_bins,
        phase_bins=phase_bins, sigma_max_ns=sigma_max_ns)
    torch.cuda.synchronize()
    build_s = time.perf_counter() - build_start
    cache_bytes = 0 if surfel_lookup is None else (
        surfel_lookup.table.numel() * surfel_lookup.table.element_size())
    dataset = ShardDataset(dataset_dir, "train", kernel.cpu(), seed=0)
    samples = [dataset[i % len(dataset)] for i in range(batch_size)]
    batch = to_device(collate(samples), device)
    all_links = directed_links(batch["node_pos"].shape[1])
    if render_link_count:
        link_indices = torch.linspace(
            0, len(all_links) - 1, render_link_count, device=device).round().long().unique()
        render_links = [all_links[i] for i in link_indices.cpu().tolist()]
        render_target = batch["target"].index_select(1, link_indices)
        render_valid = batch["valid"].index_select(1, link_indices)
        full_valid_count = batch["valid"].sum()
        sampling_probability = len(render_links) / len(all_links)
    else:
        render_links = None
        render_target = batch["target"]
        render_valid = batch["valid"]
        full_valid_count = None
        sampling_probability = 1.0
    model = HeimdallSetNet(
        d_model=model_d_model, heads=model_heads, ffn=model_ffn,
        encoder_blocks=model_encoder_blocks, decoder_blocks=model_decoder_blocks,
        g_max=model_queries).to(device).train()
    parameter_count = model.count_parameters()
    if checkpoint:
        state = torch.load(checkpoint, map_location=device, weights_only=False)
        model.load_state_dict(state["model"])
    if compile_model:
        model = torch.compile(model)
    set_loss_fn = torch.compile(set_loss) if compile_loss else None
    optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4, weight_decay=1e-2)
    if checkpoint and freeze_model:
        for group in optimizer.param_groups:
            group["lr"] = 0.0
    weights = LossWeights()
    timings = []
    for step in range(warmup):
        torch.cuda.synchronize()
        _training_step(model, optimizer, batch, kernel, weights,
                       batched, rebuild_scenes, surfel_lookup, capsule_backend,
                       render_links, render_target, render_valid, full_valid_count,
                       sampling_probability,
                       False if freeze_model else not skip_physics,
                       0.5 if freeze_model else match_rotation_weight,
                       set_loss_fn, presence_threshold, compact_surfel_slots,
                       render_batch_size)
        torch.cuda.synchronize()

    if freeze_model:
        for group in optimizer.param_groups:
            group["lr"] = 0.0

    match_agreement = 1.0
    if match_rotation_weight != 0.5:
        with torch.no_grad():
            probe = model(batch["x"], batch["geom"], batch["valid"])
            truth = batch["truth"]
            _, exact_cols = match_slots(
                probe, truth["prim_type"], truth["prim_center"], truth["prim_rot"],
                truth["prim_scale"], truth["prim_present"], MatchWeights(rot=0.5))
            _, approximate_cols = match_slots(
                probe, truth["prim_type"], truth["prim_center"], truth["prim_rot"],
                truth["prim_scale"], truth["prim_present"],
                MatchWeights(rot=match_rotation_weight))
            matched = exact_cols >= 0
            match_agreement = float(
                (exact_cols[matched] == approximate_cols[matched]).float().mean()) \
                if matched.any() else 1.0

    if profile_step:
        with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
                     record_shapes=True, profile_memory=True) as prof:
            _training_step(model, optimizer, batch, kernel, weights,
                           batched, rebuild_scenes, surfel_lookup, capsule_backend,
                           render_links, render_target, render_valid, full_valid_count,
                            sampling_probability, not skip_physics,
                            match_rotation_weight, set_loss_fn, presence_threshold,
                            compact_surfel_slots, render_batch_size)
            torch.cuda.synchronize()
        print(prof.key_averages().table(sort_by="self_cuda_time_total", row_limit=30))
        print(prof.key_averages(group_by_input_shape=True).table(
            sort_by="self_cuda_time_total", row_limit=20))
        print(prof.key_averages().table(sort_by="self_cpu_time_total", row_limit=20))

    torch.cuda.reset_peak_memory_stats()
    for _ in range(steps):
        torch.cuda.synchronize()
        start = time.perf_counter()
        _training_step(model, optimizer, batch, kernel, weights,
                       batched, rebuild_scenes, surfel_lookup, capsule_backend,
                       render_links, render_target, render_valid, full_valid_count,
                       sampling_probability, not skip_physics,
                       match_rotation_weight, set_loss_fn, presence_threshold,
                       compact_surfel_slots, render_batch_size)
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - start
        timings.append(elapsed)

    with torch.no_grad():
        probe = model(batch["x"], batch["geom"], batch["valid"])
        type_counts = torch.bincount(
            probe["type_logits"].argmax(-1).flatten(), minlength=4).cpu().tolist()
        presence_mean = float(probe["presence"].mean())
        threshold_nrmse = 0.0
        if presence_threshold > 0.0:
            exact_h = render_predicted(
                probe, batch, kernel, batched=batched, surfel_lookup=surfel_lookup,
                capsule_attenuation_backend=capsule_backend, links=render_links,
                compact_surfel_slots=compact_surfel_slots)
            threshold_h = render_predicted(
                probe, batch, kernel, batched=batched, surfel_lookup=surfel_lookup,
                capsule_attenuation_backend=capsule_backend, links=render_links,
                presence_threshold=presence_threshold,
                compact_surfel_slots=compact_surfel_slots)
            threshold_nrmse = float(
                torch.linalg.vector_norm(threshold_h - exact_h) /
                torch.linalg.vector_norm(exact_h).clamp(min=1e-12))

    return {
        "renderer": "scalar-rebuild" if rebuild_scenes else (
            "batched" if batched else "scalar-hoisted"),
        "surfel_backend": surfel_backend,
        "capsule_backend": capsule_backend,
        "batch_size": batch_size,
        "render_links": len(all_links) if render_links is None else len(render_links),
        "physics": not skip_physics,
        "match_rot": match_rotation_weight,
        "match_agreement": match_agreement,
        "frozen": freeze_model,
        "compiled": compile_model,
        "compiled_loss": compile_loss,
        "matmul_precision": matmul_precision,
        "parameters": parameter_count,
        "model": (f"d{model_d_model}-h{model_heads}-f{model_ffn}-"
                  f"e{model_encoder_blocks}-d{model_decoder_blocks}-g{model_queries}"),
        "type_counts": type_counts,
        "presence_mean": presence_mean,
        "checkpoint": checkpoint or "fresh",
        "presence_threshold": presence_threshold,
        "threshold_nrmse": threshold_nrmse,
        "compact_surfel_slots": compact_surfel_slots,
        "render_batch_size": render_batch_size or batch_size,
        "median_step_s": statistics.median(timings),
        "mean_step_s": statistics.mean(timings),
        "min_step_s": min(timings),
        "max_step_s": max(timings),
        "peak_memory_gib": torch.cuda.max_memory_allocated() / 1024**3,
        "cache_build_s": build_s,
        "cache_mib": cache_bytes / 1024**2,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", default="datasets/stage2")
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--batched", action="store_true")
    parser.add_argument("--rebuild-scenes", action="store_true")
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--steps", type=int, default=10)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--surfel-backend", choices=(
        "exact", "bank-16x", "cache-1x-phase"), default="exact")
    parser.add_argument("--sigma-bins", type=int, default=128)
    parser.add_argument("--phase-bins", type=int, default=128)
    parser.add_argument("--sigma-max-ns", type=float, default=15.0)
    parser.add_argument("--capsule-backend", choices=(
        "legacy", "compact", "gaussian"), default="legacy")
    parser.add_argument("--render-links", type=int, default=0)
    parser.add_argument("--skip-physics", action="store_true")
    parser.add_argument("--match-rotation-weight", type=float, default=0.5)
    parser.add_argument("--freeze-model", action="store_true")
    parser.add_argument("--compile-model", action="store_true")
    parser.add_argument("--compile-loss", action="store_true")
    parser.add_argument("--matmul-precision", choices=("highest", "high", "medium"),
                        default="highest")
    parser.add_argument("--model-d-model", type=int, default=128)
    parser.add_argument("--model-heads", type=int, default=4)
    parser.add_argument("--model-ffn", type=int, default=1536)
    parser.add_argument("--model-encoder-blocks", type=int, default=6)
    parser.add_argument("--model-decoder-blocks", type=int, default=4)
    parser.add_argument("--model-queries", type=int, default=48)
    parser.add_argument("--checkpoint", default="")
    parser.add_argument("--presence-threshold", type=float, default=0.0)
    parser.add_argument("--compact-surfel-slots", action="store_true")
    parser.add_argument("--render-batch-size", type=int, default=0)
    args = parser.parse_args()
    result = benchmark(args.dataset_dir, args.batch_size, args.batched, args.rebuild_scenes,
                       args.warmup, args.steps, args.profile, args.surfel_backend,
                       args.sigma_bins, args.phase_bins, args.sigma_max_ns,
                       args.capsule_backend, args.render_links, args.skip_physics,
                       args.match_rotation_weight, args.freeze_model,
                       args.compile_model, args.compile_loss, args.matmul_precision,
                       args.model_d_model, args.model_heads, args.model_ffn,
                       args.model_encoder_blocks, args.model_decoder_blocks,
                       args.model_queries, args.checkpoint, args.presence_threshold,
                       args.compact_surfel_slots, args.render_batch_size)
    print(" ".join(f"{key}={value}" for key, value in result.items()), flush=True)


if __name__ == "__main__":
    main()
