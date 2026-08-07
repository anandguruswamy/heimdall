"""Build single-label seat datasets from the single-occupant car-occupancy clips.

Same frame/matrix logic as the original car-occupancy builder: full 64
aligned CIR taps over all 20 directed links, no marker-centered cropping.
Only the 16 single-occupant clips are used; Empty and TwoPeople clips are
skipped explicitly rather than silently mis-parsed (a
FrontLeftFrontRightTwoPeople tag would otherwise match FrontLeft via
startswith). See build_car_dataset_with_empty.py for the 5-class variant and
build_car_dataset_multilabel.py for the multi-hot variant that uses ALL clips.

The default --dataset-dir points at the original capture session on the team
member's PC (D:\\Heimdall\\...; not present on this machine).

Labels: FrontLeft=0, FrontRight=1, BackRight=2, BackLeft=3. Outputs
data_{raw,calibrated}[_<stamp>]/{train,test}_dataset.npz with keys
X (N,20,64) f32, y (N,) i64, person, clip, frame, link_order.
"""

import argparse
import glob
import os

import numpy as np

try:
    from build_seat_dataset import (SEAT_NAMES as ALL_SEAT_NAMES, load_complete_frames,
                                    load_references, make_link_order)
except ImportError:  # imported as part of the scripts package (unit tests)
    from scripts.build_seat_dataset import (SEAT_NAMES as ALL_SEAT_NAMES,
                                            load_complete_frames, load_references,
                                            make_link_order)

SEAT_NAMES = ALL_SEAT_NAMES[:4]
LABELS = {name: index for index, name in enumerate(SEAT_NAMES)}
DATASET_DIR = r"D:\Heimdall\data\Car-Occupancy-08052026\Car-Occupancy-08052026"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(SCRIPT_DIR, "..", "dataset")
DATE_STAMP = "2026-08-05"
TRAIN_FRACTION = 0.8
SEED = 42


def parse_clip_name(clip_dir):
    """'clip-000036-FrontRightJin' -> (label_name, person), or None for
    Empty / TwoPeople clips that don't fit the single-seat scheme."""
    tag = os.path.basename(os.path.normpath(clip_dir)).split("-", 2)[2]
    if tag == "Empty" or "TwoPeople" in tag:
        return None
    for label in LABELS:
        if tag.startswith(label):
            return label, tag[len(label):]
    raise ValueError(f"no seat label in clip name: {clip_dir}")


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

    X_raw, X_cal, y, persons, clip_ids, frame_ids = [], [], [], [], [], []
    reference_check = None
    calibration_ok = True
    n_skipped = 0
    for clip_dir in clip_dirs:
        parsed = parse_clip_name(clip_dir)
        if parsed is None:
            print(f"{os.path.basename(clip_dir):40s} skipped (not a single-occupant clip)")
            n_skipped += 1
            continue
        label_name, who = parsed
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
            cirs = np.stack([frames[frame_id][link][0] for link in link_order])
            X_raw.append(np.abs(cirs).astype(np.float32))
            if references is not None and all(len(references[link]) == cirs.shape[1]
                                              for link in link_order):
                reference = np.stack([references[link] for link in link_order])
                X_cal.append(np.abs(cirs - reference).astype(np.float32))
            else:
                calibration_ok = False
                X_cal.append(None)
            y.append(LABELS[label_name])
            persons.append(who)
            clip_ids.append(clip_id)
            frame_ids.append(frame_id)
        print(f"{os.path.basename(clip_dir):40s} complete frames: {len(frames):4d} "
              f"label={LABELS[label_name]} ({label_name}, {who})")

    if not X_raw:
        raise SystemExit(f"no complete frames found with all {len(link_order)} links")
    print(f"\n{len(clip_dirs) - n_skipped} clips used, {n_skipped} skipped (Empty/TwoPeople)")

    X_raw = np.stack(X_raw)
    y = np.asarray(y, dtype=np.int64)
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
    infix = f"_{args.stamp}" if args.stamp else ""
    for variant, X in variants:
        out_dir = os.path.join(args.out_root, f"data_{variant}{infix}")
        os.makedirs(out_dir, exist_ok=True)
        for split, indices in (("train", train_idx), ("test", test_idx)):
            np.savez_compressed(
                os.path.join(out_dir, f"{split}_dataset.npz"),
                X=X[indices], y=y[indices], person=persons[indices],
                clip=clip_ids[indices], frame=frame_ids[indices],
                link_order=np.asarray(link_order, dtype=np.int64))
        print(f"{variant}: wrote {len(train_idx)} train / {len(test_idx)} test "
              f"samples to {out_dir}")

    print("label map:", LABELS)
    print("train class counts:", np.bincount(y[train_idx]))
    print("test  class counts:", np.bincount(y[test_idx]))


if __name__ == "__main__":
    main()
