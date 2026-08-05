"""Shared link encoder (paper Sec. V-A).

Four residual 1D-CNN stages (widths 32/64/96/128, kernels 7/5/5/3, strided
downsampling, GroupNorm, GELU) with attention pooling to e_cir in R^128;
metadata MLP, Fourier-feature geometry MLP, and a learned direction-role
embedding; the token sum per Eq. (12).
"""

from __future__ import annotations

import math

import torch
from torch import nn


class ResidualConvStage(nn.Module):
    def __init__(self, in_ch: int, out_ch: int, kernel: int, stride: int = 2):
        super().__init__()
        self.conv1 = nn.Conv1d(in_ch, out_ch, kernel, stride=stride,
                               padding=kernel // 2, bias=False)
        self.gn1 = nn.GroupNorm(4, out_ch)
        self.conv2 = nn.Conv1d(out_ch, out_ch, kernel, padding=kernel // 2, bias=False)
        self.gn2 = nn.GroupNorm(4, out_ch)
        self.skip = (nn.Conv1d(in_ch, out_ch, 1, stride=stride, bias=False)
                     if in_ch != out_ch or stride != 1 else nn.Identity())
        self.act = nn.GELU()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.act(self.gn1(self.conv1(x)))
        h = self.gn2(self.conv2(h))
        return self.act(h + self.skip(x))


class LinkEncoder(nn.Module):
    """[B, L, 64, 3] CIR channels + [B, L, M] metadata + [B, L, 10] geometry
    -> [B, L, 128] link tokens."""

    def __init__(self, d_model: int = 128, meta_dim: int = 7, geom_dim: int = 11,
                 fourier_bands: int = 5):
        super().__init__()
        widths = (32, 64, 96, 128)
        kernels = (7, 5, 5, 3)
        stages = []
        in_ch = 3
        for w, k in zip(widths, kernels):
            stages.append(ResidualConvStage(in_ch, w, k))
            in_ch = w
        self.stages = nn.ModuleList(stages)
        self.pool = nn.Sequential(
            nn.Linear(128, 128), nn.GELU(), nn.Linear(128, 1))
        self.e_cir_proj = nn.Linear(128, d_model)

        self.meta_mlp = nn.Sequential(
            nn.Linear(meta_dim, 64), nn.GELU(), nn.Linear(64, d_model))
        fourier_out = 2 * fourier_bands * geom_dim
        self.geom_mlp = nn.Sequential(
            nn.Linear(fourier_out, 128), nn.GELU(), nn.Linear(128, d_model))
        self.role_emb = nn.Embedding(2, d_model)
        self.fourier = FourierFeatures(fourier_bands)

    def forward(self, x: torch.Tensor, meta: torch.Tensor,
                geom: torch.Tensor) -> torch.Tensor:
        b, l = x.shape[:2]
        h = x.reshape(b * l, 3, -1)
        for stage in self.stages:
            h = stage(h)
        att = torch.softmax(self.pool(h.transpose(1, 2)).squeeze(-1), dim=-1)
        e_cir = (h.transpose(1, 2) * att.unsqueeze(-1)).sum(dim=1)
        e_cir = self.e_cir_proj(e_cir).reshape(b, l, -1)

        e_meta = self.meta_mlp(meta)
        e_geom = self.geom_mlp(self.fourier(geom))
        # learned direction role keyed by a label-invariant geometric
        # quantity: the baseline direction's world-frame x-sign
        # (geom columns 6:9 are (p_j' - p_i'))
        role = (geom[..., 6] >= 0).long()
        e_role = self.role_emb(role)
        return e_cir + e_meta + e_geom + e_role


class FourierFeatures(nn.Module):
    def __init__(self, dim: int, bands: int = 5):
        super().__init__()
        freqs = []
        for i in range(1, bands + 1):
            freqs.append(2.0 ** i)
        self.freqs = torch.as_tensor(freqs, dtype=torch.float32)
        self.out_dim = 2 * len(freqs) * dim

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        f = self.freqs.to(x.device)
        v = x.unsqueeze(-1) * f  # [..., D, K]
        return torch.cat([torch.sin(v), torch.cos(v)], dim=-1).flatten(-2)
