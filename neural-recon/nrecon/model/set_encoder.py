"""Geometry-aware set encoder (paper Sec. V-B).

Six pre-norm transformer blocks (width 128, 4 heads, FFN 512) over the 20
link tokens. Attention logits include a learned relative-geometry bias from
baseline directions, midpoint separation, and shared-node indicators.
Missing links are masked via the key-padding mask. Node identifiers are not
embedded.
"""

from __future__ import annotations

import torch
from torch import nn


class GeometryBiasMLP(nn.Module):
    """Pairwise link-geometry bias: [L, L] logit offsets.

    Inputs per (i, j): baseline direction cosines of both links, midpoint
    separation, shared-node indicators (0/1/2 shared nodes), link lengths.
    """

    def __init__(self, in_dim: int = 5, hidden: int = 32):
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(in_dim, hidden), nn.GELU(), nn.Linear(hidden, 1))

    def forward(self, link_geom: torch.Tensor) -> torch.Tensor:
        """`link_geom` [B, L, D] -> [B, L, L] bias."""
        b, l, _ = link_geom.shape
        gi = link_geom.unsqueeze(2).expand(b, l, l, -1)  # [B, L, L, D]
        gj = link_geom.unsqueeze(1).expand(b, l, l, -1)
        mid_i = gi[..., :3] + gi[..., 6:9]
        mid_j = gj[..., :3] + gj[..., 6:9]
        # shared-node indicator from geometry (label-free): how many of link
        # i's endpoints coincide with any of link j's endpoints (0..2)
        ep_i = torch.stack([gi[..., 0:3], gi[..., 3:6]], dim=-2).unsqueeze(-2)  # [B,L,L,2,1,3]
        ep_j = torch.stack([gj[..., 0:3], gj[..., 3:6]], dim=-2).unsqueeze(-3)  # [B,L,L,1,2,3]
        same = (ep_i - ep_j).abs().sum(dim=-1) < 1e-3  # [B,L,L,2,2]
        shared = same.any(dim=-1).sum(dim=-1, keepdim=True).to(link_geom.dtype)
        sep = torch.linalg.vector_norm(mid_i - mid_j, dim=-1, keepdim=True)
        len_i = gi[..., 9:10]
        len_j = gj[..., 9:10]
        cos = (gi[..., 6:9] * gj[..., 6:9]).sum(dim=-1, keepdim=True)
        feat = torch.cat([cos, sep, len_i, len_j, shared], dim=-1)
        return self.mlp(feat).squeeze(-1)


class SetEncoderBlock(nn.Module):
    def __init__(self, d_model: int, heads: int, ffn: int):
        super().__init__()
        self.ln1 = nn.LayerNorm(d_model)
        self.attn = nn.MultiheadAttention(d_model, heads, batch_first=True)
        self.ln2 = nn.LayerNorm(d_model)
        self.ffn = nn.Sequential(
            nn.Linear(d_model, ffn), nn.GELU(), nn.Linear(ffn, d_model))

    def forward(self, x: torch.Tensor, bias: torch.Tensor,
                key_padding_mask: torch.Tensor) -> torch.Tensor:
        b, l, _ = x.shape
        heads = self.attn.num_heads
        mask = bias.unsqueeze(1).expand(b, heads, l, l).reshape(b * heads, l, l)
        pad = key_padding_mask[:, None, None, :].expand(b, heads, l, l).reshape(b * heads, l, l)
        mask = mask.masked_fill(pad, -1e9)
        h = self.ln1(x)
        attn_out, _ = self.attn(h, h, h, attn_mask=mask, need_weights=False)
        x = x + attn_out
        x = x + self.ffn(self.ln2(x))
        return x


class SetEncoder(nn.Module):
    def __init__(self, d_model: int = 128, heads: int = 4, ffn: int = 512,
                 n_blocks: int = 6, geom_dim: int = 11):
        super().__init__()
        self.blocks = nn.ModuleList(
            [SetEncoderBlock(d_model, heads, ffn) for _ in range(n_blocks)])
        self.bias_mlp = GeometryBiasMLP(in_dim=5)

    def forward(self, tokens: torch.Tensor, link_geom: torch.Tensor,
                link_valid: torch.Tensor) -> torch.Tensor:
        bias = self.bias_mlp(link_geom)
        key_padding_mask = ~link_valid
        x = tokens
        for block in self.blocks:
            x = block(x, bias, key_padding_mask)
        return x
