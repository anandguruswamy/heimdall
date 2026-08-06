"""Evaluate the seat head of seat, separate, joint, or legacy checkpoints."""

import argparse
import os

import numpy as np
import torch

from train_seat_classifier import SeatCNN, build_model

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_ROOT = os.path.join(SCRIPT_DIR, "..", "dataset")
MODEL_DIR = os.path.join(SCRIPT_DIR, "..", "models")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data_raw")
    parser.add_argument("--dataset-root", default=DATASET_ROOT)
    parser.add_argument("--split", default="test")
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--no-plot", action="store_true",
                        help="retained for CLI compatibility; plotting is not performed")
    args = parser.parse_args()
    path = args.checkpoint or os.path.join(MODEL_DIR, f"seat_cnn_{args.data_dir}.pt")
    checkpoint = torch.load(path, map_location="cpu", weights_only=False)
    mode = checkpoint.get("model_mode", "seat")
    if mode == "person":
        raise SystemExit("person-only checkpoint has no seat head to evaluate")
    mean, std = np.asarray(checkpoint["norm_mean"]), np.asarray(checkpoint["norm_std"])
    if "model_mode" in checkpoint:
        names = list(checkpoint["seat_names"])
        model = build_model(mode, checkpoint["architecture"], checkpoint["n_links"],
                            checkpoint["n_taps"], len(names),
                            len(checkpoint.get("person_names", [])))
    else:
        names = list(checkpoint["class_names"])
        model = SeatCNN(mean.shape[0], mean.shape[1], len(names))
    model.load_state_dict(checkpoint["state_dict"])
    model.eval()
    data_dir = args.data_dir if os.path.isabs(args.data_dir) else os.path.join(args.dataset_root, args.data_dir)
    dataset = np.load(os.path.join(data_dir, f"{args.split}_dataset.npz"))
    X = np.clip(20.0 * np.log10(dataset["X"] + 1e-6),
                checkpoint["db_floor"], checkpoint["db_ceil"])
    X = (X - mean) / std
    y = np.asarray(dataset["seat_y"] if "seat_y" in dataset else dataset["y"])
    predictions = []
    with torch.no_grad():
        for start in range(0, len(X), 256):
            output = model(torch.from_numpy(X[start:start + 256, None].astype(np.float32)))
            logits = output["seat"] if isinstance(output, dict) else output
            predictions.extend(logits.argmax(1).numpy().tolist())
    confusion = np.zeros((len(names), len(names)), dtype=np.int64)
    np.add.at(confusion, (y, np.asarray(predictions)), 1)
    accuracy = confusion.trace() / max(confusion.sum(), 1)
    print(f"checkpoint: {os.path.normpath(path)}")
    print(f"accuracy {accuracy:.4f} ({confusion.trace()}/{confusion.sum()})")
    print("confusion matrix (rows=true, cols=predicted):")
    print(" " * 12 + "".join(f"{name[:10]:>11s}" for name in names))
    for index, name in enumerate(names):
        print(f"{name:>12s}" + "".join(f"{value:11d}" for value in confusion[index]))


if __name__ == "__main__":
    main()
