"""Unit tests for the dataset and model contracts."""

import os
import tempfile
import unittest

import numpy as np
import torch

from scripts.build_seat_dataset import (crop_complex, deterministic_round,
                                         make_link_order, parse_clip_label,
                                         parse_clip_name)
from scripts.train_seat_classifier import build_model


class DatasetTests(unittest.TestCase):
    def test_canonical_link_order(self):
        links = make_link_order("canonical")
        self.assertEqual(len(links), 10)
        self.assertEqual(links, [(0, 1), (0, 2), (0, 3), (0, 4), (1, 2),
                                 (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)])
        self.assertTrue(all(source < target for source, target in links))

    def test_deterministic_crop_and_padding(self):
        cir = np.arange(5) + 1j * np.arange(5)
        np.testing.assert_array_equal(crop_complex(cir, 1, 3, 2),
                                      np.asarray([0, 0, 0, 1 + 1j, 2 + 2j, 3 + 3j]))
        np.testing.assert_array_equal(crop_complex(cir, 4, 1, 2),
                                      np.asarray([3 + 3j, 4 + 4j, 0, 0]))
        self.assertEqual(deterministic_round(2.5), 3)
        self.assertEqual(deterministic_round(-2.5), -3)

    def test_empty_and_person_parsing(self):
        root = tempfile.gettempdir()
        self.assertEqual(parse_clip_name(os.path.join(root, "clip-7-Empty")),
                         ("Empty", ""))
        self.assertEqual(parse_clip_name(os.path.join(root, "clip-8-BackRightAda")),
                         ("BackRight", "Ada"))
        with self.assertRaises(ValueError):
            parse_clip_name(os.path.join(root, "clip-9-EmptyAda"))

    def test_exact_exported_person_label(self):
        with tempfile.TemporaryDirectory(prefix="clip-7-FrontLeftLegacy") as clip:
            with open(os.path.join(clip, "training-label.json"), "w", encoding="utf-8") as stream:
                stream.write('{"seat":"FrontLeft","person":"Ada Lovelace"}')
            self.assertEqual(parse_clip_label(clip), ("FrontLeft", "Ada Lovelace"))


class ModelTests(unittest.TestCase):
    def test_forward_shapes_all_modes_and_architectures(self):
        expected = {"seat": {"seat": (2, 5)},
                    "person": {"person": (2, 4)},
                    "separate": {"seat": (2, 5), "person": (2, 4)},
                    "joint": {"seat": (2, 5), "person": (2, 4)}}
        x = torch.randn(2, 1, 10, 33)
        for architecture in ("standard", "lite"):
            for mode in expected:
                with self.subTest(architecture=architecture, mode=mode):
                    output = build_model(mode, architecture, 10, 33, 5, 4)(x)
                    self.assertEqual({key: tuple(value.shape) for key, value in output.items()},
                                     expected[mode])


if __name__ == "__main__":
    unittest.main()
