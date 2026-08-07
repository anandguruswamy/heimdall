"""Generate schema-v2 manifests for legacy multilabel checkpoints.

The team-trained seat_cnn_*_multilabel.pt checkpoints predate the manifest
contract: they carry multi_label=True but no model_mode and no sidecar
.manifest.json, so the service would fall back to legacy single-label
handling. This writes the manifest that declares their true contract: 20
directed links, full uncropped taps (crop="full"), multilabel mode, and the
exact raw/calibrated variant string the service matches on.
"""

import argparse
import json
import os

import numpy as np
import torch

try:
    from build_seat_dataset import make_link_order
except ImportError:  # imported as part of the scripts package (unit tests)
    from scripts.build_seat_dataset import make_link_order


def write_manifest(checkpoint_path, threshold):
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    if not checkpoint.get("multi_label"):
        raise SystemExit(f"{checkpoint_path} is not a multi_label checkpoint")
    if checkpoint.get("model_mode") is not None:
        raise SystemExit(f"{checkpoint_path} already carries model_mode="
                         f"{checkpoint['model_mode']!r}; the trainer wrote its manifest")
    class_names = [str(name) for name in checkpoint["class_names"]]
    if class_names != ["FrontLeft", "FrontRight", "BackRight", "BackLeft"]:
        raise SystemExit(f"{checkpoint_path}: class_names {class_names} do not match the "
                         "canonical bit order; the service assumes it and would "
                         "mislabel seats")
    mean = np.asarray(checkpoint["norm_mean"])
    n_links, n_taps = mean.shape
    link_order = make_link_order("directed")
    if len(link_order) != n_links:
        raise SystemExit(f"{checkpoint_path}: norm stats cover {n_links} links, "
                         f"expected {len(link_order)} directed links")
    data_dir = str(checkpoint.get("data_dir", ""))
    manifest = {
        "schema_version": 2,
        "model_mode": "multilabel",
        "architecture": "standard",
        "seat_names": class_names,
        "person_names": [],
        "link_order": [list(pair) for pair in link_order],
        "link_mode": "directed",
        "n_links": n_links, "n_taps": n_taps,
        "taps_left": 0, "taps_right": n_taps - 1,
        "crop": "full",
        "threshold": threshold,
        "variant": "calibrated" if "calibrated" in data_dir else "raw",
        "dataset_variant": data_dir.replace("data_", "", 1),
        "db_floor": float(checkpoint["db_floor"]), "db_ceil": float(checkpoint["db_ceil"]),
        "db_params": {"floor": float(checkpoint["db_floor"]),
                      "ceil": float(checkpoint["db_ceil"]), "epsilon": 1e-6},
        "subset_accuracy": float(checkpoint.get("test_subset_accuracy", 0.0)),
        "mean_bit_accuracy": float(checkpoint.get("test_mean_bit_accuracy", 0.0)),
        "seat_accuracy": float(checkpoint.get("test_subset_accuracy", 0.0)),
        "checkpoint_filename": os.path.basename(checkpoint_path),
        "features": {"normalization": "per-link-tap", "input": "magnitude"},
    }
    manifest_path = os.path.splitext(checkpoint_path)[0] + ".manifest.json"
    with open(manifest_path, "w", encoding="ascii") as stream:
        json.dump(manifest, stream, separators=(",", ":"), sort_keys=True)
    print(f"wrote {manifest_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", action="append", required=True,
                        help="multilabel .pt path (repeatable)")
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()
    if not 0.0 < args.threshold < 1.0:
        parser.error("--threshold must be strictly between 0 and 1")
    for path in args.checkpoint:
        write_manifest(path, args.threshold)


if __name__ == "__main__":
    main()
