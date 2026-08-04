"""Determinism plumbing tests."""

from __future__ import annotations

import random

import numpy as np
import torch

from nrecon.seeding import seed_all


def test_seed_all_reproduces_draws():
    seed_all(42)
    py_a = random.random()
    np_a = np.random.rand(4)
    torch_a = torch.rand(4)

    seed_all(42)
    py_b = random.random()
    np_b = np.random.rand(4)
    torch_b = torch.rand(4)

    assert py_a == py_b
    assert np.array_equal(np_a, np_b)
    assert torch.equal(torch_a, torch_b)


def test_seed_all_changes_draws():
    seed_all(1)
    a = np.random.rand(4)
    seed_all(2)
    b = np.random.rand(4)
    assert not np.array_equal(a, b)
