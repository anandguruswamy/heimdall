"""Extract the wednesday-new-test session as a cross-session test set.

Source clips live in folders named by seat (frontleft/frontright/backright/
backleft); the folder name is taken as ground truth per the data owner.
NOTE: for the two front clips the capture-time names inside manifest.json are
crossed relative to the folder names (folder frontleft = capture
'test1frontright' and vice versa); both names are stored per sample
(`folder`, `capture_name`) so the labels can be audited later.

This session has its OWN board freeze (references differ from the training
session — verified and reported): the calibrated variant subtracts this
session's frozen references, which is what its live CIRs were aligned
against. Same frame logic and matrix layout as build_seat_dataset.py.

Output: wednesday_test_dataset.npz in dataset/data_raw and
dataset/data_calibrated (same keys as the existing sets, plus
folder/capture_name).
"""

import hashlib
import json
import os

import numpy as np

from build_seat_dataset import (LABELS, LINK_ORDER, load_complete_frames,
                                load_references)

SOURCE_DIR = r"C:\Users\qc_de\Downloads\heimdall-main\heimdall-main\datasets\wednesday-new-test"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_ROOT = os.path.join(SCRIPT_DIR, "..", "dataset")
OUT_NAME = "wednesday_test_dataset.npz"

# Folder name -> seat label (folder names are the declared ground truth).
FOLDER_LABELS = {"frontleft": "FrontLeft", "frontright": "FrontRight",
                 "backright": "BackRight", "backleft": "BackLeft"}
TRAIN_REFS_HASH = "495ec447347c"  # 2026-08-04 training session, for comparison


def refs_hash(clip_dir):
    with open(os.path.join(clip_dir, "metadata.json")) as f:
        refs = json.load(f)["board_freeze"]["references"]
    return hashlib.sha256(
        json.dumps(refs, sort_keys=True).encode()).hexdigest()[:12]


def main():
    X_raw, X_cal, y = [], [], []
    person, clip_id, frame_id, folder_arr, capture_name = [], [], [], [], []
    ref_check, session_hash = None, None

    for folder, label_name in FOLDER_LABELS.items():
        clip_dir = os.path.join(SOURCE_DIR, folder)
        with open(os.path.join(clip_dir, "manifest.json")) as f:
            manifest = json.load(f)

        h = refs_hash(clip_dir)
        session_hash = session_hash or h
        refs = load_references(clip_dir)
        ref_matrix = np.stack([refs[lk] for lk in LINK_ORDER])
        if ref_check is None:
            ref_check = ref_matrix
        elif not np.array_equal(ref_check, ref_matrix):
            raise ValueError(f"frozen references differ within session: {folder}")

        frames = load_complete_frames(clip_dir)
        for fid in sorted(frames):
            cirs = np.stack([frames[fid][lk] for lk in LINK_ORDER])
            X_raw.append(np.abs(cirs).astype(np.float32))
            X_cal.append(np.abs(cirs - ref_matrix).astype(np.float32))
            y.append(LABELS[label_name])
            person.append("unknown")
            clip_id.append(manifest["clip_id"])
            frame_id.append(fid)
            folder_arr.append(folder)
            capture_name.append(manifest["name"])
        print(f"{folder:12s} clip={manifest['clip_id']} "
              f"capture_name={manifest['name']!r} label={LABELS[label_name]} "
              f"({label_name}) complete frames: {len(frames)}")

    print(f"\nsession refs hash {session_hash} "
          f"(training session was {TRAIN_REFS_HASH}"
          f"{' — SAME' if session_hash == TRAIN_REFS_HASH else ' — DIFFERENT: new board freeze'})")

    arrays = dict(
        y=np.asarray(y, dtype=np.int64),
        person=np.asarray(person),
        clip=np.asarray(clip_id, dtype=np.int64),
        frame=np.asarray(frame_id, dtype=np.int64),
        folder=np.asarray(folder_arr),
        capture_name=np.asarray(capture_name),
        link_order=np.asarray(LINK_ORDER, dtype=np.int64))
    for variant, X in (("data_raw", X_raw), ("data_calibrated", X_cal)):
        out_path = os.path.join(DATASET_ROOT, variant, OUT_NAME)
        np.savez_compressed(out_path, X=np.stack(X), **arrays)
        print(f"wrote {len(X)} samples to {os.path.normpath(out_path)}")
    print("class counts:", np.bincount(arrays["y"]))


if __name__ == "__main__":
    main()
