"""HeimdallSetNet: the assembled geometry-conditioned set-prediction network
(paper Fig. 1). Inputs a directed CIR set + geometry (no per-link scalar
metadata -- see preprocess.py's module docstring); outputs a fixed-size
typed primitive slot set ready for the Phase 6 losses.
"""

from __future__ import annotations

import torch
from torch import nn

from nrecon.model.decoder import PrimitiveDecoder, split_heads
from nrecon.model.encoder import LinkEncoder
from nrecon.model.set_encoder import SetEncoder


MODEL_CONFIG_FIELDS = {
    "d_model": "model_d_model",
    "heads": "model_heads",
    "ffn": "model_ffn",
    "g_max": "model_queries",
    "encoder_blocks": "model_encoder_blocks",
    "decoder_blocks": "model_decoder_blocks",
}


class HeimdallSetNet(nn.Module):
    def __init__(self, d_model: int = 128, heads: int = 4, ffn: int = 1536,
                  g_max: int = 48, geom_dim: int = 11,
                  fourier_bands: int = 5, encoder_blocks: int = 6,
                  decoder_blocks: int = 4):
        super().__init__()
        self.encoder = LinkEncoder(d_model, geom_dim, fourier_bands)
        self.set_encoder = SetEncoder(
            d_model, heads, ffn, n_blocks=encoder_blocks, geom_dim=geom_dim)
        self.decoder = PrimitiveDecoder(
            g_max, d_model, heads, ffn, n_blocks=decoder_blocks)

    @classmethod
    def from_config(cls, config) -> "HeimdallSetNet":
        """Construct from a TrainConfig or saved config dictionary.

        Missing fields intentionally retain the legacy architecture so old
        checkpoints remain loadable.
        """
        values = config if isinstance(config, dict) else vars(config)
        kwargs = {
            constructor_name: values[config_name]
            for constructor_name, config_name in MODEL_CONFIG_FIELDS.items()
            if config_name in values
        }
        return cls(**kwargs)

    def forward(self, x: torch.Tensor, geom: torch.Tensor,
                link_valid: torch.Tensor) -> dict:
        tokens = self.encoder(x, geom)
        tokens = self.set_encoder(tokens, geom, link_valid)
        key_padding_mask = ~link_valid
        raw = self.decoder(tokens, key_padding_mask)
        return split_heads(raw)

    def count_parameters(self) -> int:
        return sum(p.numel() for p in self.parameters())
