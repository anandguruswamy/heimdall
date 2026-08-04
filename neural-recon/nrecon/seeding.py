"""Determinism plumbing: seed the global RNGs consistently.

Every dataset, experiment, and training run takes an explicit integer seed
recorded in its manifest (with generator git revision and config hash);
rebuilding with the same seed must be bit-identical.
"""

from __future__ import annotations

import random

import numpy as np
import torch


def seed_all(seed: int) -> None:
    """Seed `random`, `numpy`, and `torch` (CPU and CUDA) deterministically."""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
