"""Build shared seat/person classifier datasets from capture clips."""

import argparse
import glob
import json
import math
import os

import numpy as np

DATASET_DIR = r"C:\Users\qc_de\OneDrive\Desktop\heimdall-main\heimdall-main\datasets\chair-occupancy-2026-08-04"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(SCRIPT_DIR, "..", "dataset")
SEAT_NAMES = ["FrontLeft", "FrontRight", "BackRight", "BackLeft", "Empty"]
N_NODES = 5
TRAIN_FRACTION = 0.8
SEED = 42


def make_link_order(mode="canonical", n_nodes=N_NODES):
    if mode == "canonical":
        return [(a, b) for a in range(n_nodes) for b in range(a + 1, n_nodes)]
    if mode == "directed":
        return [(a, b) for a in range(n_nodes) for b in range(n_nodes) if a != b]
    raise ValueError(f"unknown link mode: {mode}")


def deterministic_round(value):
    """Round halves away from zero, without Python's ties-to-even behavior."""
    value = float(value)
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def crop_complex(cir, center, taps_left, taps_right):
    cir = np.asarray(cir)
    width = taps_left + 1 + taps_right
    out = np.zeros(width, dtype=np.result_type(cir.dtype, np.complex64))
    start = int(center) - taps_left
    src_start = max(start, 0)
    src_end = min(start + width, len(cir))
    if src_end > src_start:
        out[src_start - start:src_end - start] = cir[src_start:src_end]
    return out


def parse_clip_name(clip_dir):
    """Return (seat name, person string) from clip-<id>-<Seat><Person>."""
    name = os.path.basename(os.path.normpath(clip_dir))
    parts = name.split("-", 2)
    if len(parts) != 3 or parts[0] != "clip":
        raise ValueError(f"invalid clip folder name: {name}")
    tag = parts[2]
    for seat in SEAT_NAMES:
        if tag.startswith(seat):
            person = tag[len(seat):]
            if seat == "Empty" and person:
                raise ValueError(f"Empty clip must not name a person: {name}")
            if seat != "Empty" and not person:
                raise ValueError(f"occupied clip must name a person: {name}")
            return seat, person
    raise ValueError(f"no seat label in clip name: {name}")


def parse_clip_label(clip_dir):
    """Prefer the exact service-exported label; retain legacy folder parsing."""
    path = os.path.join(clip_dir, "training-label.json")
    if not os.path.isfile(path):
        return parse_clip_name(clip_dir)
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream)
    seat = str(value.get("seat", ""))
    person = str(value.get("person", "")).strip()
    if seat not in SEAT_NAMES:
        raise ValueError(f"invalid seat label in {path}: {seat!r}")
    if seat == "Empty":
        return seat, ""
    if not person:
        raise ValueError(f"occupied clip must name a person: {path}")
    return seat, person


def _complex_iq(iq):
    if len(iq) and isinstance(iq[0], dict):
        return np.asarray([complex(v["re"], v["im"]) for v in iq])
    values = np.asarray(iq, dtype=np.float64)
    if values.ndim == 2 and values.shape[1] == 2:
        return values[:, 0] + 1j * values[:, 1]
    raise ValueError(f"invalid IQ shape: {values.shape}")


def load_references(clip_dir, link_order):
    path = os.path.join(clip_dir, "metadata.json")
    with open(path, encoding="utf-8") as stream:
        values = (json.load(stream).get("board_freeze") or {}).get("references") or {}
    refs = {}
    for key, value in values.items():
        a, b = key.split(">")
        refs[(int(a), int(b))] = _complex_iq(value["iq"])
    if any(link not in refs for link in link_order):
        return None
    return refs


def load_complete_frames(clip_dir, link_order):
    frames = {}
    path = os.path.join(clip_dir, "aligned-cirs.ndjson")
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            link = (int(record["from"]), int(record["to"]))
            if link not in link_order:
                continue
            frames.setdefault(int(record["round"]) // 5, {})[link] = (
                _complex_iq(record["iq"]), deterministic_round(record["marker_aligned"]))
    return {frame: links for frame, links in frames.items()
            if all(link in links for link in link_order)}


def _save_split(path, X, indices, fields):
    sample_fields = {"seat_y", "y", "person_y", "person", "clip", "frame"}
    payload = {key: value[indices] if key in sample_fields else value
               for key, value in fields.items()}
    payload["X"] = X[indices]
    np.savez_compressed(path, **payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", default=DATASET_DIR)
    parser.add_argument("--out-root", default=OUT_ROOT)
    parser.add_argument("--link-mode", choices=("canonical", "directed"),
                        default="canonical")
    parser.add_argument("--taps-left", type=int, default=8)
    parser.add_argument("--taps-right", type=int, default=24)
    args = parser.parse_args()
    if args.taps_left < 0 or args.taps_right < 0:
        parser.error("tap counts must be non-negative")

    link_order = make_link_order(args.link_mode)
    clip_dirs = sorted(glob.glob(os.path.join(args.dataset_dir, "clip-*")))
    if not clip_dirs:
        raise SystemExit(f"no clips found under {args.dataset_dir}")
    parsed = [(path, *parse_clip_label(path)) for path in clip_dirs]
    person_names = sorted({person for _, seat, person in parsed if seat != "Empty"})
    person_map = {name: index for index, name in enumerate(person_names)}

    raw, calibrated, seats, people, persons, clips, frame_ids = [], [], [], [], [], [], []
    reference_check = None
    calibration_ok = True
    for clip_dir, seat, person in parsed:
        references = load_references(clip_dir, link_order)
        if references is None:
            calibration_ok = False
        else:
            current = [references[link] for link in link_order]
            if reference_check is None:
                reference_check = current
            elif any(not np.array_equal(a, b) for a, b in zip(reference_check, current)):
                calibration_ok = False

        frames = load_complete_frames(clip_dir, link_order)
        clip_id = int(os.path.basename(clip_dir).split("-")[1])
        for frame_id in sorted(frames):
            links = frames[frame_id]
            raw_rows, calibrated_rows = [], []
            for link in link_order:
                cir, center = links[link]
                raw_rows.append(crop_complex(cir, center, args.taps_left, args.taps_right))
                if references is not None:
                    if len(references[link]) != len(cir):
                        calibration_ok = False
                    else:
                        calibrated_rows.append(crop_complex(
                            cir - references[link], center, args.taps_left, args.taps_right))
            raw.append(np.abs(np.stack(raw_rows)).astype(np.float32))
            calibrated.append(np.abs(np.stack(calibrated_rows)).astype(np.float32)
                              if len(calibrated_rows) == len(link_order) else None)
            seats.append(SEAT_NAMES.index(seat))
            people.append(-1 if seat == "Empty" else person_map[person])
            persons.append(person)
            clips.append(clip_id)
            frame_ids.append(frame_id)
        print(f"{os.path.basename(clip_dir):40s} complete frames: {len(frames):4d}")

    if not raw:
        raise SystemExit(f"no complete frames found with all {len(link_order)} selected links")
    X_raw = np.stack(raw)
    seat_y = np.asarray(seats, dtype=np.int64)
    clip_ids = np.asarray(clips, dtype=np.int64)
    fields = {
        "seat_y": seat_y, "y": seat_y, "person_y": np.asarray(people, dtype=np.int64),
        "person_names": np.asarray(person_names), "person": np.asarray(persons),
        "clip": clip_ids, "frame": np.asarray(frame_ids, dtype=np.int64),
        "link_order": np.asarray(link_order, dtype=np.int64),
        "taps_left": np.asarray(args.taps_left), "taps_right": np.asarray(args.taps_right),
        "link_mode": np.asarray(args.link_mode),
    }
    rng = np.random.RandomState(SEED)
    train, test = [], []
    for clip_id in np.unique(clip_ids):
        indices = rng.permutation(np.flatnonzero(clip_ids == clip_id))
        upper = len(indices) if len(indices) == 1 else len(indices) - 1
        cut = min(max(int(round(TRAIN_FRACTION * len(indices))), 1), upper)
        train.extend(indices[:cut])
        test.extend(indices[cut:])
    train = np.sort(np.asarray(train, dtype=np.int64))
    test = np.sort(np.asarray(test, dtype=np.int64))

    variants = [("raw", X_raw)]
    if calibration_ok and all(value is not None for value in calibrated):
        variants.append(("calibrated", np.stack(calibrated)))
    else:
        print("calibrated dataset skipped: incomplete or inconsistent frozen references")
    for variant, X in variants:
        out_dir = os.path.join(args.out_root, f"data_{variant}")
        os.makedirs(out_dir, exist_ok=True)
        _save_split(os.path.join(out_dir, "train_dataset.npz"), X, train, fields)
        _save_split(os.path.join(out_dir, "test_dataset.npz"), X, test, fields)
        print(f"{variant}: wrote {len(train)} train / {len(test)} test samples to {out_dir}")


if __name__ == "__main__":
    main()
