"""Train a CNN seat classifier on the heimdall chair-occupancy CIR dataset.

Input:  train_dataset.npz / test_dataset.npz produced by build_seat_dataset.py
        (X: (N, 20, 64) float32 CIR magnitude matrices, y: seat labels 0..3).
Usage:  python train_seat_classifier.py [--data-dir data_raw|data_calibrated]
        [--dataset-root DIR] [--epochs N]
        --dataset-root defaults to ../dataset next to this script, so existing
        usage is unchanged; the heimdall dashboard passes a per-run directory.

Preprocessing: log-compression (dB) to tame the ~7 orders of magnitude between
direct-path taps and the noise floor, then per-(link, tap) standardization
using statistics computed on the training set only.

Model: small CNN whose conv kernels span only the tap axis and share weights
across all 20 link rows (a perturbed path looks the same physics-wise on any
link); the dense head then learns which combination of links encodes which
seat. ~190k parameters.

Outputs per-epoch train/val loss and accuracy, then final test accuracy,
per-class metrics, and the confusion matrix. Saves the model + normalization
stats to ../models/seat_cnn_<variant>.pt.
"""

import argparse
import os
import time

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_ROOT = os.path.join(SCRIPT_DIR, "..", "dataset")
MODEL_DIR = os.path.join(SCRIPT_DIR, "..", "models")
CLASS_NAMES = ["FrontLeft", "FrontRight", "BackRight", "BackLeft"]

SEED = 42
BATCH_SIZE = 64
EPOCHS = 30
LR = 1e-3
WEIGHT_DECAY = 1e-4
VAL_FRACTION = 0.1
DB_FLOOR, DB_CEIL = -60.0, 40.0


def to_db(x):
    """Magnitude -> clipped dB. Handles the ~1e-18 tail taps gracefully."""
    return np.clip(20.0 * np.log10(x + 1e-6), DB_FLOOR, DB_CEIL)


class SeatCNN(nn.Module):
    def __init__(self, n_links=20, n_taps=64, n_classes=4):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(1, 32, kernel_size=(1, 7), padding=(0, 3)),
            nn.BatchNorm2d(32), nn.ReLU(),
            nn.MaxPool2d((1, 2)),
            nn.Conv2d(32, 64, kernel_size=(1, 5), padding=(0, 2)),
            nn.BatchNorm2d(64), nn.ReLU(),
            nn.MaxPool2d((1, 2)),
            nn.Conv2d(64, 64, kernel_size=(1, 3), padding=(0, 1)),
            nn.ReLU(),
            nn.AdaptiveAvgPool2d((n_links, 1)),  # pool over taps, keep links
        )
        self.head = nn.Sequential(
            nn.Flatten(),
            nn.Linear(64 * n_links, 128), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(128, n_classes),
        )

    def forward(self, x):          # x: (B, 1, 20, 64)
        return self.head(self.features(x))


def run_epoch(model, loader, criterion, optimizer=None):
    training = optimizer is not None
    model.train(training)
    total_loss, correct, count = 0.0, 0, 0
    with torch.set_grad_enabled(training):
        for xb, yb in loader:
            logits = model(xb)
            loss = criterion(logits, yb)
            if training:
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            total_loss += loss.item() * len(yb)
            correct += (logits.argmax(1) == yb).sum().item()
            count += len(yb)
    return total_loss / count, correct / count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data_raw",
                        help="dataset variant folder under the dataset root "
                             "(data_raw or data_calibrated)")
    parser.add_argument("--dataset-root", default=DATASET_ROOT,
                        help="root folder containing the data_raw/data_calibrated "
                             "variants (default: ../dataset next to this script)")
    parser.add_argument("--epochs", type=int, default=EPOCHS)
    parser.add_argument("--shuffle-labels", action="store_true",
                        help="control experiment: randomly permute the training "
                             "labels (test labels untouched); test accuracy "
                             "should collapse to ~25%% if the pipeline has no "
                             "label leakage")
    args = parser.parse_args()
    data_dir = os.path.join(args.dataset_root, args.data_dir)

    np.random.seed(SEED)
    torch.manual_seed(SEED)

    train_npz = np.load(os.path.join(data_dir, "train_dataset.npz"))
    test_npz = np.load(os.path.join(data_dir, "test_dataset.npz"))
    X_train_all, y_train_all = to_db(train_npz["X"]), train_npz["y"]
    X_test, y_test = to_db(test_npz["X"]), test_npz["y"]

    if args.shuffle_labels:
        y_train_all = np.random.RandomState(SEED + 1).permutation(y_train_all)
        print("*** CONTROL RUN: training labels randomly permuted ***")

    # Per-(link, tap) standardization with train-set statistics only.
    mean = X_train_all.mean(axis=0)
    std = X_train_all.std(axis=0) + 1e-6
    X_train_all = (X_train_all - mean) / std
    X_test = (X_test - mean) / std

    # Stratified train/val split for epoch monitoring.
    val_idx = []
    rng = np.random.RandomState(SEED)
    for c in np.unique(y_train_all):
        idx = rng.permutation(np.flatnonzero(y_train_all == c))
        val_idx.extend(idx[:int(round(VAL_FRACTION * len(idx)))])
    val_mask = np.zeros(len(y_train_all), dtype=bool)
    val_mask[val_idx] = True

    def make_loader(X, y, shuffle):
        ds = TensorDataset(torch.from_numpy(X[:, None, :, :].astype(np.float32)),
                           torch.from_numpy(y))
        return DataLoader(ds, batch_size=BATCH_SIZE, shuffle=shuffle)

    train_loader = make_loader(X_train_all[~val_mask], y_train_all[~val_mask], True)
    val_loader = make_loader(X_train_all[val_mask], y_train_all[val_mask], False)
    test_loader = make_loader(X_test, y_test, False)

    model = SeatCNN()
    n_params = sum(p.numel() for p in model.parameters())
    print(f"data: {data_dir}")
    print(f"train {np.sum(~val_mask)} / val {np.sum(val_mask)} / test {len(y_test)}  "
          f"| model params: {n_params:,}\n")

    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=LR,
                                 weight_decay=WEIGHT_DECAY)

    best_val_acc, best_state = 0.0, None
    for epoch in range(1, args.epochs + 1):
        t0 = time.time()
        train_loss, train_acc = run_epoch(model, train_loader, criterion, optimizer)
        val_loss, val_acc = run_epoch(model, val_loader, criterion)
        marker = ""
        if val_acc >= best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            marker = " *"
        print(f"epoch {epoch:2d}/{args.epochs}  "
              f"train loss {train_loss:.4f} acc {train_acc:.4f}  |  "
              f"val loss {val_loss:.4f} acc {val_acc:.4f}  "
              f"({time.time() - t0:.1f}s){marker}")

    model.load_state_dict(best_state)
    test_loss, test_acc = run_epoch(model, test_loader, criterion)

    # Confusion matrix and per-class metrics on the test set.
    model.eval()
    with torch.no_grad():
        preds = torch.cat([model(xb).argmax(1) for xb, _ in test_loader]).numpy()
    n_cls = len(CLASS_NAMES)
    confusion = np.zeros((n_cls, n_cls), dtype=int)
    for t, p in zip(y_test, preds):
        confusion[t, p] += 1

    print(f"\n=== test evaluation (best-val model) ===")
    print(f"test loss {test_loss:.4f}  accuracy {test_acc:.4f} "
          f"({confusion.trace()}/{confusion.sum()})\n")
    print(f"{'class':>12s} {'precision':>9s} {'recall':>7s} {'f1':>7s} {'support':>8s}")
    for c, name in enumerate(CLASS_NAMES):
        tp = confusion[c, c]
        prec = tp / max(confusion[:, c].sum(), 1)
        rec = tp / max(confusion[c, :].sum(), 1)
        f1 = 2 * prec * rec / max(prec + rec, 1e-12)
        print(f"{name:>12s} {prec:9.4f} {rec:7.4f} {f1:7.4f} {confusion[c].sum():8d}")

    print("\nconfusion matrix (rows = true, cols = predicted):")
    header = " " * 12 + "".join(f"{n[:10]:>11s}" for n in CLASS_NAMES)
    print(header)
    for c, name in enumerate(CLASS_NAMES):
        print(f"{name:>12s}" + "".join(f"{v:11d}" for v in confusion[c]))

    # Per-person accuracy — a first hint at generalization across subjects.
    persons = test_npz["person"]
    print("\nper-person test accuracy:")
    for who in np.unique(persons):
        m = persons == who
        print(f"  {who:10s} {np.mean(preds[m] == y_test[m]):.4f} ({m.sum()} samples)")

    os.makedirs(MODEL_DIR, exist_ok=True)
    suffix = "_shuffled-labels" if args.shuffle_labels else ""
    ckpt_path = os.path.join(MODEL_DIR, f"seat_cnn_{args.data_dir}{suffix}.pt")
    torch.save({"state_dict": model.state_dict(), "norm_mean": mean,
                "norm_std": std, "class_names": CLASS_NAMES,
                "db_floor": DB_FLOOR, "db_ceil": DB_CEIL,
                "data_dir": args.data_dir, "test_accuracy": test_acc}, ckpt_path)
    print(f"\nsaved model to {ckpt_path}")


if __name__ == "__main__":
    main()
