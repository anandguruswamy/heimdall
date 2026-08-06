"""Train seat, person, separate, or joint CIR classifiers."""

import argparse
import copy
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
            if mode in ("seat", "joint"):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("seat", "person", "separate", "joint"),
                        default="seat")
    parser.add_argument("--architecture", choices=("standard", "lite"), default="standard")
    parser.add_argument("--dataset-root", default=DATASET_ROOT)
    parser.add_argument("--data-dir", default="data_raw")
    parser.add_argument("--epochs", type=int, default=EPOCHS)
    parser.add_argument("--patience", type=int, default=5)
    parser.add_argument("--device", default=None)
    parser.add_argument("--shuffle-labels", action="store_true")
    args = parser.parse_args()

    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(SEED)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    device = torch.device(args.device or ("cuda" if torch.cuda.is_available() else "cpu"))

    data_dir = args.data_dir if os.path.isabs(args.data_dir) else os.path.join(args.dataset_root, args.data_dir)
    train_npz = np.load(os.path.join(data_dir, "train_dataset.npz"))
    test_npz = np.load(os.path.join(data_dir, "test_dataset.npz"))
    X_all, X_test = to_db(train_npz["X"]), to_db(test_npz["X"])
    seat_all = np.asarray(train_npz["seat_y"] if "seat_y" in train_npz else train_npz["y"], dtype=np.int64)
    seat_test = np.asarray(test_npz["seat_y"] if "seat_y" in test_npz else test_npz["y"], dtype=np.int64)
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
        if args.mode in ("seat", "separate", "joint"):
            seat_all = rng.permutation(seat_all)
        if args.mode in ("person", "separate", "joint"):
            person_all = rng.permutation(person_all)

    mean, std = X_all.mean(axis=0), X_all.std(axis=0) + 1e-6
    X_all, X_test = (X_all - mean) / std, (X_test - mean) / std
    if args.mode in ("joint", "separate"):
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

    train_loader = loader(X_all, seat_all, person_all, train_idx, True)
    val_loader = loader(X_all, seat_all, person_all, val_idx)
    test_loader = loader(X_test, seat_test, person_test)
    n_people = len(model_person_names)
    model = build_model(args.mode, args.architecture, n_links, n_taps,
                        len(SEAT_NAMES), n_people).to(device)
    criteria = {}
    if args.mode in ("seat", "separate", "joint"):
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
    suffix = "_shuffled-labels" if args.shuffle_labels else ""
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
    result = {"model_path": model_path, "manifest_path": manifest_path,
              "seat_names": SEAT_NAMES if "seat" in tasks else [],
              "person_names": model_person_names if "person" in tasks else [], **metrics}
    print("HEIMDALL_RESULT " + json.dumps(_json_safe(result), separators=(",", ":")))


if __name__ == "__main__":
    main()
