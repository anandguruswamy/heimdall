"""HeimdallSetNet: the assembled geometry-conditioned set-prediction network
(paper Fig. 1). Inputs a directed CIR set + metadata + geometry; outputs a
fixed-size typed primitive slot set ready for the Phase 6 losses.
"""

from __future__ import annotations

import torch
from torch import nn

from nrecon.model.decoder import PrimitiveDecoder, split_heads
from nrecon.model.encoder import LinkEncoder
from nrecon.model.set_encoder import SetEncoder


class HeimdallSetNet(nn.Module):
    def __init__(self, d_model: int = 128, heads: int = 4, ffn: int = 1536,
                 g_max: int = 48, meta_dim: int = 7, geom_dim: int = 11,
                 fourier_bands: int = 5):
        super().__init__()
        self.encoder = LinkEncoder(d_model, meta_dim, geom_dim, fourier_bands)
        self.set_encoder = SetEncoder(d_model, heads, ffn, n_blocks=6, geom_dim=geom_dim)
        self.decoder = PrimitiveDecoder(g_max, d_model, heads, ffn, n_blocks=4)

    def forward(self, x: torch.Tensor, meta: torch.Tensor, geom: torch.Tensor,
                link_valid: torch.Tensor) -> dict:
        tokens = self.encoder(x, meta, geom)
        tokens = self.set_encoder(tokens, geom, link_valid)
        key_padding_mask = ~link_valid
        raw = self.decoder(tokens, key_padding_mask)
        return split_heads(raw)

    def count_parameters(self) -> int:
        return sum(p.numel() for p in self.parameters())
