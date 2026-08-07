"""Unit tests for the dataset and model contracts."""

import json
import os
import tempfile
import unittest

import numpy as np
import torch

from scripts import build_car_dataset_multilabel as car_multilabel
from scripts.build_seat_dataset import (crop_complex, deterministic_round,
                                         make_link_order, parse_clip_label,
                                         parse_clip_name)
from scripts.train_seat_classifier import (MULTILABEL_SEAT_NAMES, _dataset_geometry,
                                           _stratified_split, build_model, combo_name,
                                           combo_confusion_matrix,
                                           multilabel_result_payload,
                                           resolve_checkpoint_mode)


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

    def test_multilabel_forward_shapes(self):
        x = torch.randn(2, 1, 10, 33)
        for architecture in ("standard", "lite"):
            with self.subTest(architecture=architecture):
                output = build_model("multilabel", architecture, 10, 33, 4, 0)(x)
                self.assertEqual({key: tuple(value.shape) for key, value in output.items()},
                                 {"seat": (2, 4)})


class MultilabelTests(unittest.TestCase):
    def test_multilabel_tag_parsing(self):
        root = tempfile.gettempdir()
        self.assertEqual(car_multilabel.parse_clip_name(os.path.join(root, "clip-000036-FrontRightJin")),
                         ([0, 1, 0, 0], "FrontRight", "Jin"))
        self.assertEqual(car_multilabel.parse_clip_name(os.path.join(root, "clip-000048-Empty")),
                         ([0, 0, 0, 0], "Empty", "none"))
        self.assertEqual(
            car_multilabel.parse_clip_name(
                os.path.join(root, "clip-000053-FrontLeftFrontRightTwoPeople")),
            ([1, 1, 0, 0], "FrontLeftFrontRight", "multiple"))
        with self.assertRaises(ValueError):
            car_multilabel.parse_clip_name(os.path.join(root, "clip-000060-Emptyy"))

    def test_multilabel_sidecar_label(self):
        with tempfile.TemporaryDirectory() as root:
            clip = os.path.join(root, "clip-000021-FrontLeftIgnored")
            os.makedirs(clip)
            with open(os.path.join(clip, "training-label.json"), "w", encoding="utf-8") as stream:
                json.dump({"seats": ["FrontLeft", "BackLeft"], "person": "multiple"}, stream)
            self.assertEqual(car_multilabel.parse_clip_label(clip),
                             ([1, 0, 0, 1], "FrontLeftBackLeft", "multiple"))
            with open(os.path.join(clip, "training-label.json"), "w", encoding="utf-8") as stream:
                json.dump({"seats": [], "person": ""}, stream)
            self.assertEqual(car_multilabel.parse_clip_label(clip),
                             ([0, 0, 0, 0], "Empty", "none"))
            with open(os.path.join(clip, "training-label.json"), "w", encoding="utf-8") as stream:
                json.dump({"seat": "BackRight", "person": "Ada"}, stream)
            self.assertEqual(car_multilabel.parse_clip_label(clip),
                             ([0, 0, 1, 0], "BackRight", "Ada"))

    def test_combo_helpers(self):
        self.assertEqual(combo_name([1, 1, 0, 0]), "FrontLeftFrontRight")
        self.assertEqual(combo_name([0, 0, 0, 0]), "Empty")
        confusion, present = combo_confusion_matrix(
            np.asarray(["Empty", "FrontLeft", "FrontLeft"]),
            np.asarray(["Empty", "FrontLeft", "FrontLeftBackLeft"]))
        self.assertEqual(present, ["Empty", "FrontLeft", "FrontLeftBackLeft"])
        np.testing.assert_array_equal(confusion,
                                      np.asarray([[1, 0, 0], [0, 1, 1], [0, 0, 0]]))

    def test_checkpoint_mode_resolution(self):
        self.assertEqual(resolve_checkpoint_mode({"model_mode": "multilabel"}), "multilabel")
        self.assertEqual(resolve_checkpoint_mode({"model_mode": "seat"}), "seat")
        self.assertEqual(resolve_checkpoint_mode({"multi_label": True,
                                                  "class_names": MULTILABEL_SEAT_NAMES}),
                         "multilabel")
        self.assertEqual(resolve_checkpoint_mode({"multi_label": False}), "seat")
        self.assertEqual(resolve_checkpoint_mode({"class_names": ["a"]}), "seat")

    def test_dataset_geometry_fallbacks(self):
        legacy = {"link_order": np.zeros((20, 2))}
        self.assertEqual(_dataset_geometry(legacy, 64), ("directed", 0, 63, "full"))
        cropped = {"link_mode": "canonical", "taps_left": 8, "taps_right": 24}
        self.assertEqual(_dataset_geometry(cropped, 33),
                         ("canonical", 8, 24, "marker_centered"))
        declared = {"link_mode": "directed", "crop": "full"}
        self.assertEqual(_dataset_geometry(declared, 64), ("directed", 0, 63, "full"))

    def test_multilabel_result_payload(self):
        payload = multilabel_result_payload([0.98, 0.01, 0.87, 0.02],
                                            MULTILABEL_SEAT_NAMES, 0.5, 7, 2.0)
        self.assertEqual(set(payload), {"frame_id", "ts", "raw_seat_bits",
                                        "raw_seat_occupied", "raw_occupied_seats",
                                        "raw_occupied_count"})
        self.assertEqual(payload["raw_seat_occupied"], [True, False, True, False])
        self.assertEqual(payload["raw_occupied_seats"], ["FrontLeft", "BackRight"])
        self.assertEqual(payload["raw_occupied_count"], 2)
        self.assertEqual(payload["frame_id"], 7)
        strict = multilabel_result_payload([0.98, 0.01, 0.87, 0.02],
                                           MULTILABEL_SEAT_NAMES, 0.9, 7, 2.0)
        self.assertEqual(strict["raw_seat_occupied"], [True, False, False, False])
        self.assertEqual(strict["raw_occupied_count"], 1)

    def test_packed_bit_strata(self):
        bits = np.asarray([[0, 0, 0, 0]] * 4 + [[1, 0, 0, 0]] * 4 + [[1, 1, 0, 0]] * 4,
                          dtype=np.int64)
        strata = bits @ np.array([8, 4, 2, 1], dtype=np.int64)
        self.assertEqual(sorted(set(strata.tolist())), [0, 8, 12])
        train_idx, val_idx = _stratified_split(strata)
        for value in (0, 8, 12):
            self.assertIn(value, strata[train_idx])
            self.assertIn(value, strata[val_idx])


if __name__ == "__main__":
    unittest.main()
