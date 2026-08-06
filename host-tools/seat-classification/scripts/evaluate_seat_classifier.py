"""Evaluate a trained seat-classifier checkpoint on a saved dataset split.

Fully standalone: needs only the checkpoint written by train_seat_classifier.py
(model weights plus the dB-compression and normalization parameters it was
trained with) and the dataset .npz files — no training happens here. Applies
the identical preprocessing to the requested split and reports loss, accuracy,
per-class precision/recall/F1, the confusion matrix (printed and saved as a
PNG heatmap under ../results/), and per-person accuracy.

Usage:
  python evaluate_seat_classifier.py                          # test split, data_raw
  python evaluate_seat_classifier.py --data-dir data_calibrated
  python evaluate_seat_classifier.py --split train            # sanity check
  python evaluate_seat_classifier.py --checkpoint <path.pt>
  python evaluate_seat_classifier.py --no-plot
"""

import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from train_seat_classifier import SeatCNN

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_ROOT = os.path.join(SCRIPT_DIR, "..", "dataset")
MODEL_DIR = os.path.join(SCRIPT_DIR, "..", "models")
RESULTS_DIR = os.path.join(SCRIPT_DIR, "..", "results")
BATCH_SIZE = 256

# Light-mode chart tokens + sequential blue ramp (magnitude encoding).
SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_MUTED = "#898781"
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5",
            "#256abf", "#184f95", "#0d366b"]


def plot_confusion(confusion, class_names, accuracy, subtitle, out_path):
    """Row-normalized heatmap (color = recall fraction), counts annotated."""
    frac = confusion / np.maximum(confusion.sum(axis=1, keepdims=True), 1)
    n = len(class_names)
    cmap = LinearSegmentedColormap.from_list("seq_blue", SEQ_BLUE)

    fig, ax = plt.subplots(figsize=(6.6, 5.6), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    im = ax.imshow(frac, cmap=cmap, vmin=0.0, vmax=1.0)

    # 2px surface-colored gaps between cells.
    ax.set_xticks(np.arange(n + 1) - 0.5, minor=True)
    ax.set_yticks(np.arange(n + 1) - 0.5, minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2)
    ax.tick_params(which="both", length=0)

    for t in range(n):
        for p in range(n):
            dark_cell = frac[t, p] > 0.55
            ax.text(p, t, f"{confusion[t, p]:d}",
                    ha="center", va="center", fontsize=13,
                    fontweight="bold" if t == p else "normal",
                    color="#ffffff" if dark_cell else INK_PRIMARY)

    ax.set_xticks(range(n), class_names, fontsize=9.5, color=INK_PRIMARY)
    ax.set_yticks(range(n), class_names, fontsize=9.5, color=INK_PRIMARY)
    ax.set_xlabel("Predicted seat", fontsize=10, color=INK_MUTED)
    ax.set_ylabel("True seat", fontsize=10, color=INK_MUTED)
    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.set_title(f"Seat classifier — confusion matrix\n{subtitle}  ·  "
                 f"accuracy {accuracy:.2%}",
                 fontsize=11, color=INK_PRIMARY, pad=12)
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("row fraction (recall)", fontsize=9, color=INK_MUTED)
    cbar.ax.tick_params(labelsize=8, colors=INK_MUTED)
    cbar.outline.set_visible(False)

    fig.tight_layout()
    fig.savefig(out_path, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data_raw",
                        help="dataset variant folder under ../dataset")
    parser.add_argument("--split", default="test",
                        help="dataset file prefix: test, train, or any "
                             "<name> with a matching <name>_dataset.npz "
                             "(e.g. wednesday_test)")
    parser.add_argument("--checkpoint", default=None,
                        help="path to .pt checkpoint (default: "
                             "../models/seat_cnn_<data-dir>.pt)")
    parser.add_argument("--no-plot", action="store_true",
                        help="skip writing the confusion-matrix PNG")
    args = parser.parse_args()

    ckpt_path = args.checkpoint or os.path.join(
        MODEL_DIR, f"seat_cnn_{args.data_dir}.pt")
    # weights_only=False: the checkpoint also stores numpy normalization stats.
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    class_names = ckpt["class_names"]

    npz = np.load(os.path.join(DATASET_ROOT, args.data_dir,
                               f"{args.split}_dataset.npz"))
    X = np.clip(20.0 * np.log10(npz["X"] + 1e-6),
                ckpt["db_floor"], ckpt["db_ceil"])
    X = (X - ckpt["norm_mean"]) / ckpt["norm_std"]
    y = npz["y"]

    model = SeatCNN(n_classes=len(class_names))
    model.load_state_dict(ckpt["state_dict"])
    model.eval()

    loader = DataLoader(
        TensorDataset(torch.from_numpy(X[:, None, :, :].astype(np.float32)),
                      torch.from_numpy(y)),
        batch_size=BATCH_SIZE)
    criterion = nn.CrossEntropyLoss()
    preds, total_loss = [], 0.0
    with torch.no_grad():
        for xb, yb in loader:
            logits = model(xb)
            total_loss += criterion(logits, yb).item() * len(yb)
            preds.append(logits.argmax(1))
    preds = torch.cat(preds).numpy()

    n_cls = len(class_names)
    confusion = np.zeros((n_cls, n_cls), dtype=int)
    for t, p in zip(y, preds):
        confusion[t, p] += 1
    accuracy = confusion.trace() / confusion.sum()

    print(f"checkpoint: {os.path.normpath(ckpt_path)}")
    print(f"data: {args.data_dir}/{args.split}_dataset.npz  "
          f"({len(y)} samples)\n")
    print(f"loss {total_loss / len(y):.4f}  accuracy {accuracy:.4f} "
          f"({confusion.trace()}/{confusion.sum()})\n")
    print(f"{'class':>12s} {'precision':>9s} {'recall':>7s} {'f1':>7s} {'support':>8s}")
    for c, name in enumerate(class_names):
        tp = confusion[c, c]
        prec = tp / max(confusion[:, c].sum(), 1)
        rec = tp / max(confusion[c, :].sum(), 1)
        f1 = 2 * prec * rec / max(prec + rec, 1e-12)
        print(f"{name:>12s} {prec:9.4f} {rec:7.4f} {f1:7.4f} {confusion[c].sum():8d}")

    print("\nconfusion matrix (rows = true, cols = predicted):")
    print(" " * 12 + "".join(f"{n[:10]:>11s}" for n in class_names))
    for c, name in enumerate(class_names):
        print(f"{name:>12s}" + "".join(f"{v:11d}" for v in confusion[c]))

    persons = npz["person"]
    print("\nper-person accuracy:")
    for who in np.unique(persons):
        m = persons == who
        print(f"  {who:10s} {np.mean(preds[m] == y[m]):.4f} ({m.sum()} samples)")

    if not args.no_plot:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        out_path = os.path.join(
            RESULTS_DIR, f"confusion_matrix_{args.data_dir}_{args.split}.png")
        plot_confusion(confusion, class_names, accuracy,
                       f"{args.data_dir} · {args.split} set ({len(y)} samples)",
                       out_path)
        print(f"\nsaved confusion-matrix plot to {os.path.normpath(out_path)}")


if __name__ == "__main__":
    main()
