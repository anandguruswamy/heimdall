"""Train seat, person, separate, joint, or multilabel CIR classifiers.

Single-label modes (seat/person/separate/joint) classify one occupant over the
five classes including Empty. Multilabel mode trains four independent per-seat
sigmoid detectors (bit order FrontLeft, FrontRight, BackRight, BackLeft; Empty
is the all-zero vector) on datasets built by build_car_dataset_multilabel.py.
"""

import argparse
import copy
import itertools
import json
import os
import random
import time

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_ROOT = os.path.join(SCRIPT_DIR, "..", "dataset")
MODEL_DIR = os.path.join(SCRIPT_DIR, "..", "models")
SEAT_NAMES = ["FrontLeft", "FrontRight", "BackRight", "BackLeft", "Empty"]
# Multilabel bit order: one independent occupied bit per physical seat, Empty
# is the all-zero vector (never a bit of its own).
MULTILABEL_SEAT_NAMES = SEAT_NAMES[:4]
SEAT_ABBR = {"FrontLeft": "FL", "FrontRight": "FR", "BackRight": "BR", "BackLeft": "BL"}
SEED = 42
BATCH_SIZE = 64
EPOCHS = 30
LR = 1e-3
WEIGHT_DECAY = 1e-4
VAL_FRACTION = 0.1
DB_FLOOR, DB_CEIL = -60.0, 40.0


def to_db(x, floor=DB_FLOOR, ceil=DB_CEIL):
    return np.clip(20.0 * np.log10(x + 1e-6), floor, ceil)


class Backbone(nn.Module):
    def __init__(self, n_links, architecture="standard"):
        super().__init__()
        channels = (32, 64, 64) if architecture == "standard" else (16, 32, 32)
        self.out_features = channels[-1] * n_links
        self.features = nn.Sequential(
            nn.Conv2d(1, channels[0], (1, 7), padding=(0, 3)),
            nn.BatchNorm2d(channels[0]), nn.ReLU(), nn.MaxPool2d((1, 2)),
            nn.Conv2d(channels[0], channels[1], (1, 5), padding=(0, 2)),
            nn.BatchNorm2d(channels[1]), nn.ReLU(), nn.MaxPool2d((1, 2)),
            nn.Conv2d(channels[1], channels[2], (1, 3), padding=(0, 1)),
            nn.ReLU(), nn.AdaptiveAvgPool2d((n_links, 1)), nn.Flatten())

    def forward(self, x):
        return self.features(x)


def _head(in_features, n_classes, architecture):
    dense = 128 if architecture == "standard" else 64
    return nn.Sequential(nn.Linear(in_features, dense), nn.ReLU(), nn.Dropout(0.3),
                         nn.Linear(dense, n_classes))


class ClassifierModel(nn.Module):
    """One serializable model class covering every schema-v2 mode."""
    def __init__(self, mode, architecture, n_links, n_taps, n_seats, n_people):
        super().__init__()
        del n_taps  # Convolutions and adaptive pooling accept any viable tap count.
        self.mode = mode
        if mode == "separate":
            self.seat_backbone = Backbone(n_links, architecture)
            self.person_backbone = Backbone(n_links, architecture)
            self.seat_head = _head(self.seat_backbone.out_features, n_seats, architecture)
            self.person_head = _head(self.person_backbone.out_features, n_people, architecture)
        else:
            self.backbone = Backbone(n_links, architecture)
            if mode in ("seat", "joint", "multilabel"):
                self.seat_head = _head(self.backbone.out_features, n_seats, architecture)
            if mode in ("person", "joint"):
                self.person_head = _head(self.backbone.out_features, n_people, architecture)

    def forward(self, x):
        if self.mode == "separate":
            return {"seat": self.seat_head(self.seat_backbone(x)),
                    "person": self.person_head(self.person_backbone(x))}
        features = self.backbone(x)
        output = {}
        if hasattr(self, "seat_head"):
            output["seat"] = self.seat_head(features)
        if hasattr(self, "person_head"):
            output["person"] = self.person_head(features)
        return output


class SeatCNN(nn.Module):
    """Legacy four-class checkpoint architecture."""
    def __init__(self, n_links=20, n_taps=64, n_classes=4):
        super().__init__()
        del n_taps
        self.features = nn.Sequential(
            nn.Conv2d(1, 32, (1, 7), padding=(0, 3)), nn.BatchNorm2d(32), nn.ReLU(),
            nn.MaxPool2d((1, 2)), nn.Conv2d(32, 64, (1, 5), padding=(0, 2)),
            nn.BatchNorm2d(64), nn.ReLU(), nn.MaxPool2d((1, 2)),
            nn.Conv2d(64, 64, (1, 3), padding=(0, 1)), nn.ReLU(),
            nn.AdaptiveAvgPool2d((n_links, 1)))
        self.head = nn.Sequential(nn.Flatten(), nn.Linear(64 * n_links, 128), nn.ReLU(),
                                  nn.Dropout(0.3), nn.Linear(128, n_classes))

    def forward(self, x):
        return self.head(self.features(x))


def build_model(mode, architecture, n_links, n_taps, n_seats=5, n_people=2):
    return ClassifierModel(mode, architecture, n_links, n_taps, n_seats, n_people)


def _stratified_split(labels):
    rng = np.random.RandomState(SEED)
    validation = []
    for label in np.unique(labels):
        indices = rng.permutation(np.flatnonzero(labels == label))
        if len(indices) >= 2:
            count = min(max(int(round(VAL_FRACTION * len(indices))), 1), len(indices) - 1)
            validation.extend(indices[:count])
    if not validation:
        return np.arange(len(labels)), np.arange(len(labels))
    mask = np.ones(len(labels), dtype=bool)
    mask[validation] = False
    return np.flatnonzero(mask), np.asarray(validation, dtype=np.int64)


def _weights(labels, n_classes, ignore=-999):
    valid = labels != ignore
    counts = np.bincount(labels[valid], minlength=n_classes).astype(np.float32)
    weights = np.zeros(n_classes, dtype=np.float32)
    present = counts > 0
    weights[present] = counts[present].sum() / (present.sum() * counts[present])
    return torch.from_numpy(weights)


def _metrics(logits, target, n_classes, ignore=None):
    if ignore is not None:
        keep = target != ignore
        logits, target = logits[keep], target[keep]
    confusion = np.zeros((n_classes, n_classes), dtype=np.int64)
    if len(target):
        pred = logits.argmax(1).cpu().numpy()
        truth = target.cpu().numpy()
        np.add.at(confusion, (truth, pred), 1)
    accuracy = float(confusion.trace() / confusion.sum()) if confusion.sum() else None
    return accuracy, confusion


def _run(model, loader, criteria, mode, device, optimizer=None):
    training = optimizer is not None
    model.train(training)
    total_loss, batches = 0.0, 0
    collected = {"seat": [[], []], "person": [[], []]}
    with torch.set_grad_enabled(training):
        for xb, seat_y, person_y in loader:
            xb, seat_y, person_y = xb.to(device), seat_y.to(device), person_y.to(device)
            output = model(xb)
            losses = []
            if "seat" in output:
                losses.append(criteria["seat"](output["seat"], seat_y))
                collected["seat"][0].append(output["seat"].detach().cpu())
                collected["seat"][1].append(seat_y.detach().cpu())
            if "person" in output:
                if mode != "joint" or torch.any(person_y != -1):
                    losses.append(criteria["person"](output["person"], person_y))
                collected["person"][0].append(output["person"].detach().cpu())
                collected["person"][1].append(person_y.detach().cpu())
            loss = sum(losses)
            if training:
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            total_loss += loss.item()
            batches += 1
    result = {"loss": total_loss / max(batches, 1)}
    for task, (logits, targets) in collected.items():
        if logits:
            result[task] = (torch.cat(logits), torch.cat(targets))
    return result


def _json_safe(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _dataset_geometry(npz, n_taps):
    """Feature geometry with fallbacks for legacy multilabel datasets.

    Datasets from build_seat_dataset.py always carry taps_left/taps_right and
    are marker-centered crops; the car builders write full uncropped taps and
    declare crop="full" (older copies of their output carry no geometry keys
    at all, which means the same thing).
    """
    if "crop" in npz:
        crop = str(npz["crop"])
    else:
        crop = "marker_centered" if "taps_left" in npz else "full"
    return (str(npz.get("link_mode", "directed")), int(npz.get("taps_left", 0)),
            int(npz.get("taps_right", n_taps - 1)), crop)


def resolve_checkpoint_mode(checkpoint):
    """Task mode of a checkpoint dict, covering every checkpoint generation."""
    mode = checkpoint.get("model_mode")
    if mode is not None:
        return str(mode)
    return "multilabel" if checkpoint.get("multi_label") else "seat"


def multilabel_result_payload(probs, seat_names, threshold, frame_id, ts):
    """One live-inference NDJSON line for a multilabel prediction."""
    occupied = [bool(float(p) >= threshold) for p in probs]
    names = [name for name, bit in zip(seat_names, occupied) if bit]
    return {"frame_id": frame_id, "ts": ts,
            "raw_seat_bits": [round(float(p), 4) for p in probs],
            "raw_seat_occupied": occupied, "raw_occupied_seats": names,
            "raw_occupied_count": len(names)}


def _canonical_combos(seat_names):
    return ["Empty"] + ["".join(combo) for size in range(1, len(seat_names) + 1)
                        for combo in itertools.combinations(seat_names, size)]


def combo_name(bits, seat_names=None):
    """4-bit occupancy vector -> combination name, e.g. [1,1,0,0] ->
    'FrontLeftFrontRight', all zeros -> 'Empty'. Matches the label_name field
    written by build_car_dataset_multilabel.py."""
    seat_names = seat_names or MULTILABEL_SEAT_NAMES
    name = "".join(seat for seat, bit in zip(seat_names, bits) if bit)
    return name or "Empty"


def combo_abbr(name, seat_names=None):
    seat_names = seat_names or MULTILABEL_SEAT_NAMES
    if name == "Empty":
        return "Empty"
    return "".join(SEAT_ABBR.get(seat, seat) for seat in seat_names if seat in name)


def combo_confusion_matrix(true_names, pred_names, seat_names=None):
    """Confusion matrix over the observed seat-occupancy combinations, in
    canonical order (Empty first, then subsets by size)."""
    seat_names = seat_names or MULTILABEL_SEAT_NAMES
    observed = set(true_names) | set(pred_names)
    present = [combo for combo in _canonical_combos(seat_names) if combo in observed]
    index = {combo: i for i, combo in enumerate(present)}
    confusion = np.zeros((len(present), len(present)), dtype=np.int64)
    for true, pred in zip(true_names, pred_names):
        confusion[index[true], index[pred]] += 1
    return confusion, present


def print_combo_confusion(confusion, combos, seat_names=None):
    print(f"\ncombination-level confusion matrix (rows = true, cols = predicted; "
          f"{', '.join(f'{combo_abbr(c, seat_names)}={c}' for c in combos if c != 'Empty')}):")
    print(" " * 22 + "".join(f"{combo_abbr(c, seat_names):>8s}" for c in combos))
    for i, name in enumerate(combos):
        print(f"{name:>22s}" + "".join(f"{v:8d}" for v in confusion[i]))
    print(f"\n{'combination':>22s} {'precision':>9s} {'recall':>7s} {'f1':>7s} {'support':>8s}")
    for i, name in enumerate(combos):
        tp = confusion[i, i]
        precision = tp / max(confusion[:, i].sum(), 1)
        recall = tp / max(confusion[i, :].sum(), 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-12)
        print(f"{name:>22s} {precision:9.4f} {recall:7.4f} {f1:7.4f} {confusion[i].sum():8d}")


def _bit_accuracy(logits, targets, threshold):
    # >= matches the live-inference decision rule exactly.
    preds = (torch.sigmoid(logits) >= threshold).float()
    return float((preds == targets).float().mean())


def _multilabel_report(probs, bits_true, label_names, persons, seat_names, threshold):
    preds = (probs >= threshold).astype(np.int64)
    exact = np.all(preds == bits_true, axis=1)
    subset = float(exact.mean())
    mean_bit = float((preds == bits_true).mean())
    print(f"test mean per-seat-bit accuracy {mean_bit:.4f}  "
          f"subset (exact-match) accuracy {subset:.4f} ({int(exact.sum())}/{len(bits_true)})\n")
    print(f"{'seat':>12s} {'precision':>9s} {'recall':>7s} {'f1':>7s} {'accuracy':>8s} {'support':>8s}")
    per_seat = {}
    for i, seat in enumerate(seat_names):
        tp = int(((preds[:, i] == 1) & (bits_true[:, i] == 1)).sum())
        fp = int(((preds[:, i] == 1) & (bits_true[:, i] == 0)).sum())
        fn = int(((preds[:, i] == 0) & (bits_true[:, i] == 1)).sum())
        tn = int(((preds[:, i] == 0) & (bits_true[:, i] == 0)).sum())
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-12)
        accuracy = (tp + tn) / max(tp + fp + fn + tn, 1)
        per_seat[seat] = {"precision": precision, "recall": recall, "f1": f1,
                          "accuracy": accuracy, "support": tp + fn}
        print(f"{seat:>12s} {precision:9.4f} {recall:7.4f} {f1:7.4f} {accuracy:8.4f} {tp + fn:8d}")
    by_combo = {}
    print("\nsubset (exact-match) accuracy by observed seat combination:")
    for combo in sorted(np.unique(label_names)):
        mask = label_names == combo
        by_combo[str(combo)] = float(exact[mask].mean())
        print(f"  {str(combo):24s} {exact[mask].mean():.4f} ({int(mask.sum())} samples)")
    by_person = {}
    print("\nsubset (exact-match) accuracy by person:")
    for who in np.unique(persons):
        mask = persons == who
        by_person[str(who)] = float(exact[mask].mean())
        print(f"  {str(who):10s} {exact[mask].mean():.4f} ({int(mask.sum())} samples)")
    pred_names = np.asarray([combo_name(row, seat_names) for row in preds])
    confusion, combos = combo_confusion_matrix(label_names, pred_names, seat_names)
    print_combo_confusion(confusion, combos, seat_names)
    return {"subset_accuracy": subset, "mean_bit_accuracy": mean_bit,
            "seat_accuracy": subset, "per_seat": per_seat,
            "combo_names": combos, "combo_confusion": confusion,
            "exact_match_by_combo": by_combo, "exact_match_by_person": by_person}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("seat", "person", "separate", "joint", "multilabel"),
                        default=None)
    parser.add_argument("--multi-label", action="store_true",
                        help="alias for --mode multilabel (legacy spelling)")
    parser.add_argument("--with-empty", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--architecture", choices=("standard", "lite"), default="standard")
    parser.add_argument("--dataset-root", default=DATASET_ROOT)
    parser.add_argument("--data-dir", default="data_raw")
    parser.add_argument("--epochs", type=int, default=EPOCHS)
    parser.add_argument("--patience", type=int, default=5)
    parser.add_argument("--device", default=None)
    parser.add_argument("--tag", default=None, help="optional checkpoint filename suffix")
    parser.add_argument("--threshold", type=float, default=None,
                        help="multilabel per-seat decision threshold (default 0.5)")
    parser.add_argument("--shuffle-labels", action="store_true")
    args = parser.parse_args()
    if args.with_empty:
        parser.error("--with-empty belongs to the legacy trainer; --mode seat already "
                     "trains the 5-class task including Empty")
    if args.multi_label and args.mode not in (None, "multilabel"):
        parser.error(f"--multi-label conflicts with --mode {args.mode}")
    args.mode = "multilabel" if args.multi_label else (args.mode or "seat")
    multilabel = args.mode == "multilabel"
    if args.threshold is not None and not multilabel:
        parser.error("--threshold applies only to --mode multilabel")
    threshold = 0.5 if args.threshold is None else args.threshold
    if not 0.0 < threshold < 1.0:
        parser.error("--threshold must be strictly between 0 and 1")

    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(SEED)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    device = torch.device(args.device or ("cuda" if torch.cuda.is_available() else "cpu"))

    data_dir = args.data_dir if os.path.isabs(args.data_dir) else os.path.join(args.dataset_root, args.data_dir)
    if multilabel and not os.path.normpath(data_dir).endswith("_multilabel"):
        # Keep the legacy spelling working: --multi-label --data-dir data_raw
        # resolves the suffixed folder the multilabel builder writes.
        candidate = os.path.normpath(data_dir) + "_multilabel"
        if os.path.isfile(os.path.join(candidate, "train_dataset.npz")):
            data_dir = candidate
    train_path = os.path.join(data_dir, "train_dataset.npz")
    if not os.path.isfile(train_path):
        raise SystemExit(f"no dataset at {train_path}; if the build step skipped the "
                         "calibrated variant (missing or inconsistent frozen "
                         "references), train the raw variant instead")
    train_npz = np.load(train_path)
    test_npz = np.load(os.path.join(data_dir, "test_dataset.npz"))
    X_all, X_test = to_db(train_npz["X"]), to_db(test_npz["X"])
    seat_all = np.asarray(train_npz["seat_y"] if "seat_y" in train_npz else train_npz["y"], dtype=np.int64)
    seat_test = np.asarray(test_npz["seat_y"] if "seat_y" in test_npz else test_npz["y"], dtype=np.int64)
    if multilabel and seat_all.ndim != 2:
        raise SystemExit(f"{data_dir} holds single-label targets; build a multilabel "
                         "dataset with build_car_dataset_multilabel.py or drop --mode multilabel")
    if not multilabel and seat_all.ndim != 1:
        raise SystemExit(f"{data_dir} holds multilabel targets; pass --mode multilabel")
    if multilabel:
        if seat_all.shape[1] != len(MULTILABEL_SEAT_NAMES):
            raise SystemExit(f"multilabel dataset must carry {len(MULTILABEL_SEAT_NAMES)} "
                             f"seat bits, found {seat_all.shape[1]}")
        npz_names = ([str(value) for value in train_npz["seat_names"]]
                     if "seat_names" in train_npz else MULTILABEL_SEAT_NAMES)
        if list(npz_names) != MULTILABEL_SEAT_NAMES:
            raise SystemExit(f"multilabel bit order {npz_names} does not match "
                             f"{MULTILABEL_SEAT_NAMES}")
        person_names = []
        person_all = np.zeros(len(seat_all), dtype=np.int64)
        person_test = np.zeros(len(seat_test), dtype=np.int64)
    else:
        person_names = [str(value) for value in train_npz.get("person_names", np.unique(train_npz["person"]))]
        if "person_y" in train_npz:
            person_all, person_test = train_npz["person_y"].astype(np.int64), test_npz["person_y"].astype(np.int64)
        else:
            mapping = {name: index for index, name in enumerate(person_names)}
            person_all = np.asarray([mapping[str(value)] for value in train_npz["person"]])
            person_test = np.asarray([mapping[str(value)] for value in test_npz["person"]])
    n_links, n_taps = X_all.shape[1:]
    if args.mode in ("person", "separate"):
        person_all = np.where(person_all < 0, len(person_names), person_all)
        person_test = np.where(person_test < 0, len(person_names), person_test)
        model_person_names = person_names + ["n/a"]
    else:
        model_person_names = person_names

    if args.shuffle_labels:
        rng = np.random.RandomState(SEED + 1)
        if args.mode in ("seat", "separate", "joint", "multilabel"):
            seat_all = rng.permutation(seat_all)
        if args.mode in ("person", "separate", "joint"):
            person_all = rng.permutation(person_all)

    mean, std = X_all.mean(axis=0), X_all.std(axis=0) + 1e-6
    X_all, X_test = (X_all - mean) / std, (X_test - mean) / std
    if multilabel:
        strata = seat_all @ np.array([8, 4, 2, 1], dtype=np.int64)
    elif args.mode in ("joint", "separate"):
        strata = seat_all * (len(person_names) + 1) + (person_all + 1)
    elif args.mode == "person":
        strata = person_all
    else:
        strata = seat_all
    train_idx, val_idx = _stratified_split(strata)

    def loader(X, seat, person, indices=None, shuffle=False):
        if indices is not None:
            X, seat, person = X[indices], seat[indices], person[indices]
        dataset = TensorDataset(torch.from_numpy(X[:, None].astype(np.float32)),
                                torch.from_numpy(seat), torch.from_numpy(person))
        return DataLoader(dataset, batch_size=min(BATCH_SIZE, max(len(dataset), 1)), shuffle=shuffle)

    seat_targets_all = seat_all.astype(np.float32) if multilabel else seat_all
    seat_targets_test = seat_test.astype(np.float32) if multilabel else seat_test
    train_loader = loader(X_all, seat_targets_all, person_all, train_idx, True)
    val_loader = loader(X_all, seat_targets_all, person_all, val_idx)
    test_loader = loader(X_test, seat_targets_test, person_test)
    n_people = len(model_person_names)
    model = build_model(args.mode, args.architecture, n_links, n_taps,
                        len(MULTILABEL_SEAT_NAMES) if multilabel else len(SEAT_NAMES),
                        n_people).to(device)
    criteria = {}
    if multilabel:
        criteria["seat"] = nn.BCEWithLogitsLoss()
    elif args.mode in ("seat", "separate", "joint"):
        criteria["seat"] = nn.CrossEntropyLoss(
            weight=_weights(seat_all[train_idx], len(SEAT_NAMES)).to(device))
    if args.mode in ("person", "separate"):
        criteria["person"] = nn.CrossEntropyLoss(
            weight=_weights(person_all[train_idx], n_people).to(device))
    elif args.mode == "joint":
        criteria["person"] = nn.CrossEntropyLoss(
            weight=_weights(person_all[train_idx], n_people, -1).to(device), ignore_index=-1)
    optimizer = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)

    best_metric, best_state, stale = -1.0, None, 0
    for epoch in range(1, args.epochs + 1):
        started = time.time()
        train_result = _run(model, train_loader, criteria, args.mode, device, optimizer)
        val_result = _run(model, val_loader, criteria, args.mode, device)
        scores = []
        for task in ("seat", "person"):
            if task in val_result:
                if multilabel:
                    scores.append(_bit_accuracy(*val_result[task], threshold))
                    continue
                count = len(SEAT_NAMES) if task == "seat" else n_people
                ignore = -1 if task == "person" and args.mode == "joint" else None
                score, _ = _metrics(*val_result[task], count, ignore)
                if score is not None:
                    scores.append(score)
        metric = float(np.mean(scores)) if scores else -val_result["loss"]
        improved = metric > best_metric
        if improved:
            best_metric, best_state, stale = metric, copy.deepcopy(model.state_dict()), 0
        else:
            stale += 1
        print(f"epoch {epoch}/{args.epochs} train_loss={train_result['loss']:.4f} "
              f"val_loss={val_result['loss']:.4f} val_metric={metric:.4f} "
              f"seconds={time.time() - started:.1f}{' *' if improved else ''}")
        if stale >= args.patience:
            print(f"early stopping after {epoch} epochs")
            break
    if best_state is None:
        best_state = copy.deepcopy(model.state_dict())
    model.load_state_dict(best_state)
    test_result = _run(model, test_loader, criteria, args.mode, device)
    if multilabel:
        logits, targets = test_result["seat"]
        probs = torch.sigmoid(logits).numpy()
        bits_true = targets.numpy().astype(np.int64)
        label_names = (np.asarray([str(value) for value in test_npz["label_name"]])
                       if "label_name" in test_npz else
                       np.asarray([combo_name(row) for row in bits_true]))
        persons = (np.asarray([str(value) for value in test_npz["person"]])
                   if "person" in test_npz else np.full(len(bits_true), "unknown"))
        metrics = _multilabel_report(probs, bits_true, label_names, persons,
                                     MULTILABEL_SEAT_NAMES, threshold)
    else:
        metrics = {}
        tasks = (["seat"] if args.mode == "seat" else ["person"] if args.mode == "person"
                 else ["seat", "person"])
        for task in tasks:
            if task in test_result:
                count = len(SEAT_NAMES) if task == "seat" else n_people
                ignore = -1 if task == "person" and args.mode == "joint" else None
                accuracy, confusion = _metrics(*test_result[task], count, ignore)
            else:
                count = len(SEAT_NAMES) if task == "seat" else n_people
                accuracy, confusion = None, np.zeros((count, count), dtype=np.int64)
            metrics[f"{task}_accuracy"] = accuracy
            metrics[f"{task}_confusion"] = confusion

    variant = os.path.basename(os.path.normpath(data_dir)).replace("data_", "")
    link_order = np.asarray(train_npz.get("link_order", []), dtype=np.int64)
    suffix = "_shuffled-labels" if args.shuffle_labels else ""
    if args.tag:
        suffix += f"_{args.tag}"
    if multilabel:
        variant_stem = (variant[:-len("_multilabel")]
                        if variant.endswith("_multilabel") else variant)
        link_mode, taps_left, taps_right, crop = _dataset_geometry(train_npz, n_taps)
        metadata = {
            "model_mode": args.mode, "architecture": args.architecture,
            "seat_names": MULTILABEL_SEAT_NAMES, "person_names": [],
            "link_order": link_order, "link_mode": link_mode,
            "taps_left": taps_left, "taps_right": taps_right,
            "n_links": n_links, "n_taps": n_taps, "norm_mean": mean, "norm_std": std,
            "db_floor": DB_FLOOR, "db_ceil": DB_CEIL,
            # The service selects calibration by exact string equality, so
            # "variant" is the base raw/calibrated marker; the full folder stem
            # is preserved separately.
            "variant": "calibrated" if variant_stem.startswith("calibrated") else "raw",
            "dataset_variant": variant, "crop": crop, "threshold": threshold,
            "db_params": {"floor": DB_FLOOR, "ceil": DB_CEIL, "epsilon": 1e-6},
            "schema_version": 2, **metrics,
        }
        stem = f"classifier_multilabel_{args.architecture}_{variant_stem}{suffix}"
    else:
        metadata = {
            "model_mode": args.mode, "architecture": args.architecture,
            "seat_names": SEAT_NAMES, "person_names": model_person_names,
            "link_order": link_order, "link_mode": str(train_npz.get("link_mode", "directed")),
            "taps_left": int(train_npz.get("taps_left", 0)),
            "taps_right": int(train_npz.get("taps_right", n_taps - 1)),
            "n_links": n_links, "n_taps": n_taps, "norm_mean": mean, "norm_std": std,
            "db_floor": DB_FLOOR, "db_ceil": DB_CEIL, "variant": variant,
            "db_params": {"floor": DB_FLOOR, "ceil": DB_CEIL, "epsilon": 1e-6},
            "schema_version": 2, **metrics,
        }
        # Single-label datasets from the car builders are full-window features;
        # without this declaration the service would apply a marker-centered
        # crop and feed the model a different feature space. Datasets from
        # build_seat_dataset.py carry taps keys and stay untouched.
        if _dataset_geometry(train_npz, n_taps)[3] == "full":
            metadata["crop"] = "full"
        stem = f"classifier_{args.mode}_{args.architecture}_{variant}{suffix}"
    os.makedirs(MODEL_DIR, exist_ok=True)
    model_path = os.path.abspath(os.path.join(MODEL_DIR, stem + ".pt"))
    manifest_path = os.path.abspath(os.path.join(MODEL_DIR, stem + ".manifest.json"))
    torch.save({**metadata, "state_dict": model.state_dict()}, model_path)
    manifest = {key: value for key, value in metadata.items()
                if key not in ("norm_mean", "norm_std")}
    manifest.update({"checkpoint_filename": os.path.basename(model_path),
                     "features": {"normalization": "per-link-tap", "input": "magnitude"}})
    with open(manifest_path, "w", encoding="ascii") as stream:
        json.dump(_json_safe(manifest), stream, separators=(",", ":"), sort_keys=True)
    if multilabel:
        result = {"model_path": model_path, "manifest_path": manifest_path,
                  "model_mode": "multilabel", "seat_names": MULTILABEL_SEAT_NAMES,
                  "person_names": [], "threshold": threshold, **metrics}
    else:
        result = {"model_path": model_path, "manifest_path": manifest_path,
                  "seat_names": SEAT_NAMES if "seat" in tasks else [],
                  "person_names": model_person_names if "person" in tasks else [], **metrics}
    print("HEIMDALL_RESULT " + json.dumps(_json_safe(result), separators=(",", ":")))


if __name__ == "__main__":
    main()
