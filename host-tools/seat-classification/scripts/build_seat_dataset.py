"""Build seat-classification datasets from heimdall chair-occupancy capture clips.

Reads all clip-*/aligned-cirs.ndjson files, keeps only complete frames
(a frame = 5 consecutive TDMA rounds, round // 5, containing all 20 directed
links), and produces per-frame 20x64 CIR magnitude matrices with a consistent
link row order.

Two datasets are written:
  data_raw/        X[i] = |aligned CIR|                       (20, 64) float32
  data_calibrated/ X[i] = |aligned CIR - frozen link reference| (20, 64) float32

The frozen per-link reference CIRs come from each clip's metadata.json
(board_freeze.references); they are byte-identical across all 16 clips of the
2026-08-04 session, so calibration is consistent dataset-wide.

Labels: FrontLeft=0, FrontRight=1, BackRight=2, BackLeft=3.

Split: per-clip stratified 80/20 (each clip's frames shuffled with a fixed
seed, first 80% -> train, rest -> test), so every clip/person/seat appears in
both sets in equal proportion. NOTE: adjacent frames from the same clip are
highly correlated, so this split measures in-session accuracy only; for a
generalization test, re-split by the saved `person` array (leave-one-person-out).

Usage: python build_seat_dataset.py [--dataset-dir DIR] [--out-root DIR]
--dataset-dir defaults to the original 2026-08-04 session path; --out-root
defaults to ../dataset next to this script (gitignored). The heimdall
dashboard's Training tab passes both explicitly. If any clip lacks frozen
board references (board never frozen during capture) or the references
differ between clips, the calibrated dataset is skipped with a warning
instead of failing; the raw dataset is always written.

Each .npz contains:
  X          (N, 20, 64) float32   CIR magnitude matrices
  y          (N,)        int64     seat label
  person     (N,)        str       who was seated
  clip       (N,)        int64     source clip id (13..28)
  frame      (N,)        int64     frame id (round // 5) within the session
  link_order (20, 2)     int64     (from, to) node ids for each matrix row
"""

import argparse
import glob
import json
import os

import numpy as np

# Original 2026-08-04 chair-occupancy session (extracted clip folders); the
# heimdall dashboard always passes --dataset-dir explicitly.
DATASET_DIR = r"C:\Users\qc_de\OneDrive\Desktop\heimdall-main\heimdall-main\datasets\chair-occupancy-2026-08-04"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(SCRIPT_DIR, "..", "dataset")

LABELS = {"FrontLeft": 0, "FrontRight": 1, "BackRight": 2, "BackLeft": 3}
N_NODES = 5
N_TAPS = 64
LINK_ORDER = [(a, b) for a in range(N_NODES) for b in range(N_NODES) if a != b]
TRAIN_FRACTION = 0.8
SEED = 42


def parse_clip_name(clip_dir):
    """'clip-000013-FrontLeftSimarjit' -> (label_name, person)."""
    tag = os.path.basename(clip_dir).split("-", 2)[2]
    for label in LABELS:
        if tag.startswith(label):
            return label, tag[len(label):]
    raise ValueError(f"no seat label in clip name: {clip_dir}")


def load_references(clip_dir):
    """Frozen per-link reference CIRs as {(from, to): complex (64,)}, or None
    when the clip has no complete set of frozen board references."""
    with open(os.path.join(clip_dir, "metadata.json")) as f:
        refs_json = (json.load(f).get("board_freeze") or {}).get("references") or {}
    refs = {}
    for key, val in refs_json.items():
        a, b = key.split(">")
        refs[(int(a), int(b))] = np.array(
            [complex(t["re"], t["im"]) for t in val["iq"]], dtype=np.complex128)
    if any(lk not in refs for lk in LINK_ORDER):
        return None
    return refs


def load_complete_frames(clip_dir):
    """{frame_id: {(from, to): complex CIR (64,)}} keeping only 20/20 frames."""
    frames = {}
    with open(os.path.join(clip_dir, "aligned-cirs.ndjson")) as f:
        for line in f:
            rec = json.loads(line)
            iq = np.asarray(rec["iq"], dtype=np.float64)
            cir = iq[:, 0] + 1j * iq[:, 1]
            if cir.shape != (N_TAPS,):
                raise ValueError(f"unexpected CIR length {cir.shape} in {clip_dir}")
            frames.setdefault(rec["round"] // 5, {})[(rec["from"], rec["to"])] = cir
    return {fid: links for fid, links in frames.items() if len(links) == len(LINK_ORDER)}


def main():
    parser = argparse.ArgumentParser(
        description="Build seat-classification datasets from heimdall capture clips.")
    parser.add_argument("--dataset-dir", default=DATASET_DIR,
                        help="directory containing extracted clip-<id>-<Label><Person>/ "
                             "folders (default: the 2026-08-04 session)")
    parser.add_argument("--out-root", default=OUT_ROOT,
                        help="output root; data_raw/ and data_calibrated/ are written "
                             "beneath it (default: the SeatClassification dataset dir)")
    args = parser.parse_args()
    out_dirs = {"raw": os.path.join(args.out_root, "data_raw"),
                "calibrated": os.path.join(args.out_root, "data_calibrated")}

    clip_dirs = sorted(glob.glob(os.path.join(args.dataset_dir, "clip-*")))
    if not clip_dirs:
        raise SystemExit(f"no clips found under {args.dataset_dir}")

    X_raw, X_cal, y, person, clip_id, frame_id = [], [], [], [], [], []
    ref_check = None
    calibration_ok = True
    for clip_dir in clip_dirs:
        label_name, who = parse_clip_name(clip_dir)
        cid = int(os.path.basename(clip_dir).split("-")[1])
        refs = load_references(clip_dir)
        ref_matrix = None
        if refs is None:
            if calibration_ok:
                print(f"{os.path.basename(clip_dir)}: no complete frozen link "
                      "references; the calibrated dataset will be skipped")
            calibration_ok = False
        else:
            ref_matrix = np.stack([refs[lk] for lk in LINK_ORDER])
            if ref_check is None:
                ref_check = ref_matrix
            elif not np.array_equal(ref_check, ref_matrix):
                if calibration_ok:
                    print(f"{os.path.basename(clip_dir)}: frozen references differ "
                          "from earlier clips; the calibrated dataset will be skipped")
                calibration_ok = False

        frames = load_complete_frames(clip_dir)
        for fid in sorted(frames):
            cirs = np.stack([frames[fid][lk] for lk in LINK_ORDER])
            X_raw.append(np.abs(cirs).astype(np.float32))
            if ref_matrix is not None:
                X_cal.append(np.abs(cirs - ref_matrix).astype(np.float32))
            y.append(LABELS[label_name])
            person.append(who)
            clip_id.append(cid)
            frame_id.append(fid)
        print(f"{os.path.basename(clip_dir):40s} complete frames: {len(frames):4d} "
              f"label={LABELS[label_name]} ({label_name}, {who})")

    if not X_raw:
        raise SystemExit(
            "no complete frames found: every frame needs all "
            f"{len(LINK_ORDER)} directed links within one 5-round cycle")

    X_raw = np.stack(X_raw)
    y = np.asarray(y, dtype=np.int64)
    person = np.asarray(person)
    clip_id = np.asarray(clip_id, dtype=np.int64)
    frame_id = np.asarray(frame_id, dtype=np.int64)

    rng = np.random.RandomState(SEED)
    train_idx, test_idx = [], []
    for cid in np.unique(clip_id):
        idx = rng.permutation(np.flatnonzero(clip_id == cid))
        n_train = int(round(TRAIN_FRACTION * len(idx)))
        train_idx.extend(idx[:n_train])
        test_idx.extend(idx[n_train:])
    train_idx = np.sort(np.asarray(train_idx))
    test_idx = np.sort(np.asarray(test_idx))

    link_order = np.asarray(LINK_ORDER, dtype=np.int64)
    variants = [("raw", X_raw)]
    if calibration_ok:
        variants.append(("calibrated", np.stack(X_cal)))
    else:
        print("calibrated dataset skipped: missing or inconsistent frozen "
              "board references (freeze the board before capturing to enable it)")
    for variant, X in variants:
        out_dir = out_dirs[variant]
        os.makedirs(out_dir, exist_ok=True)
        for split, idx in (("train", train_idx), ("test", test_idx)):
            np.savez_compressed(
                os.path.join(out_dir, f"{split}_dataset.npz"),
                X=X[idx], y=y[idx], person=person[idx], clip=clip_id[idx],
                frame=frame_id[idx], link_order=link_order)
        print(f"{variant}: wrote {len(train_idx)} train / {len(test_idx)} test "
              f"samples to {out_dir}")

    print("label map:", LABELS)
    print("train class counts:", np.bincount(y[train_idx]))
    print("test  class counts:", np.bincount(y[test_idx]))


if __name__ == "__main__":
    main()
