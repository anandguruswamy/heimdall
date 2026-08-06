from __future__ import annotations

import numpy as np
import torch

from nrecon.constants import directed_links
from nrecon.train.data import ShardDataset


def test_prepared_sample_cache_reuses_preprocessing():
    dataset = object.__new__(ShardDataset)
    dataset.cache_prepared = True
    dataset.prepared = {}
    dataset.manifest = [{"index": 0}]
    calls = []
    dataset._record = lambda index: {"index": index}

    def prepare(record):
        calls.append(record)
        return {"x": torch.tensor([1.0])}

    dataset._prepare = prepare
    first = dataset[0]
    second = dataset[0]
    assert first is second
    assert len(calls) == 1


def test_permuted_cache_reuses_cir_and_rebuilds_geometry():
    dataset = object.__new__(ShardDataset)
    dataset.cache_prepared = True
    dataset.prepared = {
        0: {
            "x": torch.ones(20, 64, 3),
            "geom": torch.zeros(20, 11),
            "node_pos": torch.arange(15, dtype=torch.float32).reshape(5, 3),
        }
    }
    dataset.dtype = torch.float32
    dataset.links = directed_links(5)
    dataset.permutations = [np.asarray([2, 4, 1, 0, 3])]
    sample = dataset.__getitem_permuted__(0)
    assert sample["x"] is dataset.prepared[0]["x"]
    assert not torch.equal(sample["node_pos"], dataset.prepared[0]["node_pos"])
    assert sample["geom"].shape == (20, 11)
