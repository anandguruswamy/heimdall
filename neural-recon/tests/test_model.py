"""Model tests (plan Phase 5): shapes, parameter count, node-relabel
invariance (Hungarian-matched), missing-link masking, preprocess
round-trip, and torch determinism."""

from __future__ import annotations

import numpy as np
import pytest
import torch
from scipy.optimize import linear_sum_assignment

from nrecon.constants import F0_MARKER, directed_links
from nrecon.model.decoder import HEAD_DIM, split_heads
from nrecon.model.net import HeimdallSetNet
from nrecon.model.preprocess import geometry_features, preprocess_cirs
from nrecon.seeding import seed_all
from nrecon.sim.export import build_scene_pipeline
from nrecon.sim.pulse import correlation_kernel, make_template_v1

torch.use_deterministic_algorithms(True)

STAGE1_CFG = {
    "stage": 1, "scenes": 1, "node_mode": "fixed_live", "jitter_m": 0.0,
    "room": {"x_range": [4, 6], "y_range": [4, 6], "z_max": [2.4, 3.0],
             "partitions": [0, 0], "planes": False},
    "surfels": {"count": [1, 3]},
    "furniture": {"count": [0, 0]}, "people": {"count": [0, 0]}, "hw": {},
}


def _kernel():
    t = make_template_v1()
    return torch.as_tensor(correlation_kernel(t, t).samples)


def _record_batch(n: int = 2, seeds=(400, 401)) -> dict:
    kernel = _kernel()
    recs = []
    for s in seeds:
        from nrecon.sim.scenes import sample_scene

        spec = sample_scene(s, STAGE1_CFG)
        recs.append(build_scene_pipeline(spec, STAGE1_CFG, kernel))
    batch = {}
    for key in recs[0]:
        if isinstance(recs[0][key], np.ndarray):
            batch[key] = np.stack([r[key] for r in recs])
        else:
            batch[key] = recs[0][key]
    return batch


def _inputs(batch: dict, kernel: torch.Tensor, links, dtype=torch.float64):
    x = preprocess_cirs(batch["cir_i16"], batch["dgc"], batch["accum"],
                        batch["fp_aligned"], kernel).double()
    geom = geometry_features(batch["node_pos"], links).double()
    valid = torch.as_tensor(batch["link_valid"])
    return x, geom, valid


def _make_net(dtype: torch.dtype = torch.float64) -> HeimdallSetNet:
    seed_all(81)
    net = HeimdallSetNet()
    return net.to(dtype).eval()


def test_forward_shapes_full_and_masked():
    net = _make_net()
    links = directed_links(5)
    batch = _record_batch()
    x, geom, valid = _inputs(batch, _kernel(), links)
    out = net(x, geom, valid)
    for k, v in out.items():
        assert v.shape[0] == 2, k
        assert v.shape[1] == 48, k
    assert out["type_logits"].shape == (2, 48, 4)
    assert out["presence"].shape == (2, 48, 1)
    assert out["center"].shape == (2, 48, 3)
    assert out["rot6d"].shape == (2, 48, 6)
    assert out["scale_log"].shape == (2, 48, 3)
    assert out["log_var_center"].shape == (2, 48, 3)

    # partially masked link set
    valid_m = valid.clone()
    valid_m[0, [3, 7]] = False
    out_m = net(x, geom, valid_m)
    assert torch.isfinite(out_m["center"]).all()


def test_legacy_default_parameter_count():
    net = _make_net(dtype=torch.float32)
    assert net.count_parameters() == 5_177_345


def test_reduced_architecture_from_config():
    net = HeimdallSetNet.from_config({
        "model_d_model": 128,
        "model_heads": 4,
        "model_ffn": 512,
        "model_encoder_blocks": 4,
        "model_decoder_blocks": 3,
        "model_queries": 24,
    })
    assert net.count_parameters() == 1_881_473
    raw = net(torch.randn(2, 20, 64, 3), torch.randn(2, 20, 11),
              torch.ones(2, 20, dtype=torch.bool))
    assert raw["center"].shape == (2, 24, 3)


def test_node_relabel_invariance():
    seed_all(82)
    links = directed_links(5)
    batch = _record_batch(seeds=(402, 403))
    kernel = _kernel()
    x, geom, valid = _inputs(batch, kernel, links)

    sigma = [2, 4, 1, 0, 3]
    perm = np.asarray(sigma)
    # Relabeled link list: link k becomes (sigma(a_k), sigma(b_k)); the
    # CIR stays at position k (the network is order-invariant). The
    # position matrix must be permuted by the INVERSE permutation so that
    # new_positions[sigma(a)] == old_positions[a].
    rel_links = [(perm[a], perm[b]) for a, b in links]
    inv = np.argsort(perm)
    x_p = x.clone()
    valid_p = valid.clone()
    geom_p = geometry_features(batch["node_pos"][:, inv], rel_links).double()

    net = _make_net()
    out = net(x, geom, valid)
    out_p = net(x_p, geom_p, valid_p)

    # match slots by parameter distance (Hungarian)
    def param_vec(o):
        return torch.cat([
            o["center"], o["rot6d"], o["scale_log"], o["rho"],
            o["presence"], o["roughness"], o["atten"], o["dynamic"]], dim=-1)

    a = param_vec(out).detach().double()
    b = param_vec(out_p).detach().double()
    cost = torch.cdist(a[0], b[0]).numpy()
    row, col = linear_sum_assignment(cost)
    assert np.max(cost[row, col]) < 1e-4, np.max(cost[row, col])


def test_missing_link_mask_correctness():
    seed_all(83)
    links = directed_links(5)
    batch = _record_batch(seeds=(404,))
    kernel = _kernel()
    x, geom, valid = _inputs(batch, kernel, links)

    net = _make_net()
    valid_m = valid.clone()
    valid_m[0, 5] = False
    out_masked = net(x, geom, valid_m)

    # under the same mask, the masked link's CIR content must not matter
    x_zero = x.clone()
    x_zero[0, 5] = 0.0
    out_zero = net(x_zero, geom, valid_m)
    for k in ("center", "rot6d", "scale_log", "presence", "type_logits"):
        assert torch.allclose(out_masked[k], out_zero[k], atol=1e-9), k


def test_preprocess_roundtrip_on_shard():
    seed_all(84)
    kernel = _kernel()
    batch = _record_batch(seeds=(405, 406))
    x = preprocess_cirs(batch["cir_i16"], batch["dgc"], batch["accum"],
                        batch["fp_aligned"], kernel)
    assert torch.isfinite(x).all()
    # LOS-dominant links: envelope peak near marker 16 after alignment
    env = x[..., 0].pow(2) + x[..., 1].pow(2)
    for scene in range(env.shape[0]):
        for li in range(env.shape[1]):
            if not batch["link_valid"][scene, li]:
                continue
            peak = int(torch.argmax(env[scene, li]))
            assert abs(peak - F0_MARKER) <= 1.5, (scene, li, peak)


def test_split_heads_sanitizes_nan_inf():
    """Regression test (2026-08-05): a NaN in the raw decoder output
    reached `presence` (sigmoid(NaN) = NaN) and tripped
    binary_cross_entropy's hard input-range CUDA assertion during the
    first real curriculum run -- which, unlike a Python exception,
    corrupts the CUDA context for the rest of the process. split_heads
    must sanitize before splitting so every head output stays finite and
    (for the sigmoid/softplus-bounded heads) in its valid range."""
    raw = torch.zeros(2, 5, HEAD_DIM, dtype=torch.float64)
    raw[0, 0, 4] = float("nan")  # presence pre-sigmoid
    raw[0, 1, 0] = float("inf")  # a type logit
    raw[1, 2, 17] = float("-inf")  # rho real part
    out = split_heads(raw)
    for k, v in out.items():
        assert torch.isfinite(v).all(), k
    assert (out["presence"] >= 0.0).all() and (out["presence"] <= 1.0).all()
    assert (out["roughness"] >= 0.0).all() and (out["roughness"] <= 1.0).all()
    assert (out["dynamic"] >= 0.0).all() and (out["dynamic"] <= 1.0).all()
    assert (out["atten"] >= 0.0).all()


def test_deterministic_algorithms_and_seeding():
    seed_all(85)
    links = directed_links(5)
    batch = _record_batch(seeds=(407,))
    kernel = _kernel()
    x, geom, valid = _inputs(batch, kernel, links)
    net = _make_net()
    with torch.no_grad():
        o1 = net(x, geom, valid)["center"].clone()
    seed_all(85)
    net = _make_net()
    with torch.no_grad():
        o2 = net(x, geom, valid)["center"]
    assert torch.equal(o1, o2)
