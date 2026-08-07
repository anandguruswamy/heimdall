"""Build a multi-label seat-occupancy dataset from car-occupancy capture clips.

Unlike build_car_dataset.py / build_car_dataset_with_empty.py (one mutually
exclusive class per frame), this script labels each frame with a 4-bit
multi-hot vector — one independent occupied/vacant bit per seat — so a frame
can carry any seat combination: Empty is the all-zero vector, a
single-occupant clip sets one bit, a TwoPeople clip sets two. Future 3-/4-
person clips need zero changes here.

Bit order: [FrontLeft, FrontRight, BackRight, BackLeft].

Clip labels come from the service-exported training-label.json sidecar when
present (authoritative; a "seats" list names the occupied seats), else from
the folder name clip-<id>-<tag> where <tag> is one of:
  <Seat><Person>              e.g. FrontRightJin                  -> one bit
  Empty                                                           -> all zero
  <Seat><Seat>TwoPeople       e.g. FrontLeftFrontRightTwoPeople   -> two bits

The default --dataset-dir points at the original capture session on the
team member's PC (D:\\Heimdall\\...; not present on this machine) — pass the
folder of exported clips instead. Features are the FULL 64 aligned CIR taps
for all 20 directed links, with no marker-centered cropping; the npz declares
this via crop="full".

Outputs (per variant): data_{raw,calibrated}[_<stamp>]_multilabel/
  {train,test}_dataset.npz with keys:
  X (N,20,64) f32, y (N,4) i64 multi-hot, label_name (N,) str,
  person (N,) str ("none" for Empty, "multiple" for 2+ seats), clip (N,) i64,
  frame (N,) i64, link_order (20,2) i64, seat_names (4,) str,
  link_mode "directed", crop "full"
"""

import argparse
import glob
import json
import os

import numpy as np

try:
    from build_seat_dataset import (SEAT_NAMES as ALL_SEAT_NAMES, load_complete_frames,
                                    load_references, make_link_order)
except ImportError:  # imported as part of the scripts package (unit tests)
    from scripts.build_seat_dataset import (SEAT_NAMES as ALL_SEAT_NAMES,
                                            load_complete_frames, load_references,
                                            make_link_order)

SEAT_NAMES = ALL_SEAT_NAMES[:4]  # bit order
DATASET_DIR = r"D:\Heimdall\data\Car-Occupancy-08052026\Car-Occupancy-08052026"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(SCRIPT_DIR, "..", "dataset")
DATE_STAMP = "2026-08-05"
TRAIN_FRACTION = 0.8
SEED = 42


def parse_clip_name(clip_dir):
    """'clip-000036-FrontRightJin' -> (bits, label_name, person)
    'clip-000048-Empty'            -> ([0,0,0,0], 'Empty', 'none')
    'clip-000053-FrontLeftFrontRightTwoPeople' -> ([1,1,0,0], 'FrontLeftFrontRight', 'multiple')
    """
    name = os.path.basename(os.path.normpath(clip_dir))
    parts = name.split("-", 2)
    if len(parts) != 3 or parts[0] != "clip":
        raise ValueError(f"invalid clip folder name: {name}")
    tag = parts[2]
    bits = [1 if seat in tag else 0 for seat in SEAT_NAMES]
    if sum(bits) == 0 and tag != "Empty":
        raise ValueError(f"no seat label in clip name: {name}")
    person = tag.replace(SEAT_NAMES[bits.index(1)], "", 1) if sum(bits) == 1 else ""
    return bits, *_finalize(bits, person)


def _finalize(bits, person_raw):
    label_name = "".join(seat for seat, bit in zip(SEAT_NAMES, bits) if bit) or "Empty"
    n_seats = sum(bits)
    if n_seats == 0:
        person = "none"
    elif n_seats == 1:
        person = person_raw.strip() or "unknown"
    else:
        person = "multiple"
    return label_name, person


def parse_clip_label(clip_dir):
    """Prefer the exact service-exported sidecar; fall back to folder parsing."""
    path = os.path.join(clip_dir, "training-label.json")
    if not os.path.isfile(path):
        return parse_clip_name(clip_dir)
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream)
    if "seats" in value:
        seats = [str(seat) for seat in value["seats"]]
        unknown = sorted(set(seats) - set(SEAT_NAMES))
        if unknown:
            raise ValueError(f"unknown seats {unknown} in {path}")
        bits = [1 if seat in seats else 0 for seat in SEAT_NAMES]
    else:
        seat = str(value.get("seat", ""))
        if seat == "Empty":
            bits = [0] * len(SEAT_NAMES)
        elif seat in SEAT_NAMES:
            bits = [1 if name == seat else 0 for name in SEAT_NAMES]
        else:
            raise ValueError(f"invalid seat label in {path}: {seat!r}")
    return bits, *_finalize(bits, str(value.get("person") or ""))


def out_dirs(out_root, stamp):
    infix = f"_{stamp}" if stamp else ""
    return {variant: os.path.join(out_root, f"data_{variant}{infix}_multilabel")
            for variant in ("raw", "calibrated")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", default=DATASET_DIR)
    parser.add_argument("--out-root", default=OUT_ROOT)
    parser.add_argument("--stamp", default=DATE_STAMP,
                        help="dataset folder name stamp; pass '' for unstamped folders")
    args = parser.parse_args()

    link_order = make_link_order("directed")
    clip_dirs = sorted(path for path in glob.glob(os.path.join(args.dataset_dir, "clip-*"))
                       if os.path.isdir(path))
    if not clip_dirs:
        raise SystemExit(f"no clips found under {args.dataset_dir}")

    X_raw, X_cal, y, label_names, persons, clip_ids, frame_ids = [], [], [], [], [], [], []
    reference_check = None
    calibration_ok = True
    for clip_dir in clip_dirs:
        bits, label_name, person = parse_clip_label(clip_dir)
        clip_id = int(os.path.basename(clip_dir).split("-")[1])
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
        for frame_id in sorted(frames):
            links = frames[frame_id]
            cirs = np.stack([links[link][0] for link in link_order])
            X_raw.append(np.abs(cirs).astype(np.float32))
            if references is not None and all(len(references[link]) == cirs.shape[1]
                                              for link in link_order):
                reference = np.stack([references[link] for link in link_order])
                X_cal.append(np.abs(cirs - reference).astype(np.float32))
            else:
                calibration_ok = False
                X_cal.append(None)
            y.append(bits)
            label_names.append(label_name)
            persons.append(person)
            clip_ids.append(clip_id)
            frame_ids.append(frame_id)
        print(f"{os.path.basename(clip_dir):40s} complete frames: {len(frames):4d} "
              f"bits={bits} ({label_name}, {person})")

    if not X_raw:
        raise SystemExit(f"no complete frames found with all {len(link_order)} links")
    print(f"\n{len(clip_dirs)} clips used (all — single, Empty, and TwoPeople)")

    X_raw = np.stack(X_raw)
    y = np.asarray(y, dtype=np.int64)
    label_names = np.asarray(label_names)
    persons = np.asarray(persons)
    clip_ids = np.asarray(clip_ids, dtype=np.int64)
    frame_ids = np.asarray(frame_ids, dtype=np.int64)

    rng = np.random.RandomState(SEED)
    train_idx, test_idx = [], []
    for clip_id in np.unique(clip_ids):
        indices = rng.permutation(np.flatnonzero(clip_ids == clip_id))
        upper = len(indices) if len(indices) == 1 else len(indices) - 1
        cut = min(max(int(round(TRAIN_FRACTION * len(indices))), 1), upper)
        train_idx.extend(indices[:cut])
        test_idx.extend(indices[cut:])
    train_idx = np.sort(np.asarray(train_idx, dtype=np.int64))
    test_idx = np.sort(np.asarray(test_idx, dtype=np.int64))

    variants = [("raw", X_raw)]
    if calibration_ok and all(value is not None for value in X_cal):
        variants.append(("calibrated", np.stack(X_cal)))
    else:
        print("calibrated dataset skipped: incomplete or inconsistent frozen references")
    dirs = out_dirs(args.out_root, args.stamp)
    for variant, X in variants:
        out_dir = dirs[variant]
        os.makedirs(out_dir, exist_ok=True)
        for split, indices in (("train", train_idx), ("test", test_idx)):
            np.savez_compressed(
                os.path.join(out_dir, f"{split}_dataset.npz"),
                X=X[indices], y=y[indices], label_name=label_names[indices],
                person=persons[indices], clip=clip_ids[indices], frame=frame_ids[indices],
                link_order=np.asarray(link_order, dtype=np.int64),
                seat_names=np.asarray(SEAT_NAMES),
                link_mode=np.asarray("directed"), crop=np.asarray("full"))
        print(f"{variant}: wrote {len(train_idx)} train / {len(test_idx)} test "
              f"samples to {out_dir}")

    print("seat bit order:", SEAT_NAMES)
    combos, counts = np.unique(label_names, return_counts=True)
    print("combination counts (all frames):")
    for combo, count in sorted(zip(combos, counts), key=lambda pair: -pair[1]):
        print(f"  {combo:24s} {count:5d}")
    print("train per-seat positive counts:", y[train_idx].sum(axis=0))
    print("test  per-seat positive counts:", y[test_idx].sum(axis=0))


if __name__ == "__main__":
    main()
