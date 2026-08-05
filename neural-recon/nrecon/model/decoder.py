"""Primitive set decoder (paper Sec. V-C).

G_MAX = 48 learned queries through 4 self/cross-attention blocks against
the encoded link tokens. Each slot predicts: type logits (4), presence,
center (3), rot6d (6), log-scales (3), complex reflectivity (2),
roughness, attenuation, dynamic probability, and bounded log-variances for
center, scales, and rotation.
"""

from __future__ import annotations

import math

import torch
from torch import nn

HEAD_DIM = 4 + 1 + 3 + 6 + 3 + 2 + 1 + 1 + 1 + 9  # 31


class DecoderBlock(nn.Module):
    def __init__(self, d_model: int, heads: int, ffn: int):
        super().__init__()
        self.ln_self = nn.LayerNorm(d_model)
        self.self_attn = nn.MultiheadAttention(d_model, heads, batch_first=True)
        self.ln_cross = nn.LayerNorm(d_model)
        self.cross_attn = nn.MultiheadAttention(d_model, heads, batch_first=True)
        self.ln_ffn = nn.LayerNorm(d_model)
        self.ffn = nn.Sequential(
            nn.Linear(d_model, ffn), nn.GELU(), nn.Linear(ffn, d_model))

    def forward(self, q: torch.Tensor, mem: torch.Tensor,
                key_padding_mask: torch.Tensor) -> torch.Tensor:
        h, _ = self.self_attn(self.ln_self(q), self.ln_self(q), self.ln_self(q))
        q = q + h
        h, _ = self.cross_attn(self.ln_cross(q), mem, mem,
                               key_padding_mask=key_padding_mask)
        q = q + h
        q = q + self.ffn(self.ln_ffn(q))
        return q


class PrimitiveDecoder(nn.Module):
    def __init__(self, g_max: int = 48, d_model: int = 128, heads: int = 4,
                 ffn: int = 512, n_blocks: int = 4):
        super().__init__()
        self.queries = nn.Parameter(torch.randn(g_max, d_model) * 0.02)
        self.blocks = nn.ModuleList(
            [DecoderBlock(d_model, heads, ffn) for _ in range(n_blocks)])
        self.head = nn.Linear(d_model, HEAD_DIM)

    def forward(self, tokens: torch.Tensor,
                key_padding_mask: torch.Tensor) -> torch.Tensor:
        b = tokens.shape[0]
        q = self.queries.unsqueeze(0).expand(b, -1, -1)
        for block in self.blocks:
            q = block(q, tokens, key_padding_mask)
        return self.head(q)  # [B, G, 31]


def split_heads(raw: torch.Tensor) -> dict:
    """Interpret the raw [B, G, 31] head output.

    Sanitizes NaN/Inf before splitting (2026-08-05): a NaN reaching
    `presence` (sigmoid(NaN) = NaN) tripped `binary_cross_entropy`'s
    hard input-range CUDA assertion during the first real curriculum run,
    which -- unlike a Python exception -- corrupts the CUDA context for
    the rest of the process and cannot be recovered from mid-run. This is
    the network's single output boundary, so sanitizing here protects
    every downstream consumer (loss, rendering, evaluation) at once.
    """
    raw = torch.nan_to_num(raw, nan=0.0, posinf=20.0, neginf=-20.0)
    return {
        "type_logits": raw[..., 0:4],
        "presence": torch.sigmoid(raw[..., 4:5]),
        "center": raw[..., 5:8],
        "rot6d": raw[..., 8:14],
        "scale_log": raw[..., 14:17],
        "rho": raw[..., 17:19],  # real, imag
        "roughness": torch.sigmoid(raw[..., 19:20]),
        "atten": torch.nn.functional.softplus(raw[..., 20:21]),
        "dynamic": torch.sigmoid(raw[..., 21:22]),
        "log_var_center": _bounded_log_var(raw[..., 22:25]),
        "log_var_scale": _bounded_log_var(raw[..., 25:28]),
        "log_var_rot": _bounded_log_var(raw[..., 28:31]),
    }


def _bounded_log_var(x: torch.Tensor, lo: float = -6.0, hi: float = 4.0) -> torch.Tensor:
    return lo + (hi - lo) * torch.sigmoid(x)
