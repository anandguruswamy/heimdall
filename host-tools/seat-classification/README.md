# Seat Classification

CNN seat-occupancy classifier trained on aligned CIR magnitude matrices from
heimdall capture clips. This folder is the canonical home of the training
toolkit; the heimdall-service Training tab drives the same scripts, so their
CLI contract (arguments, folder-name label encoding, printed output format)
must stay in sync with `unoq/crates/heimdall-service/src/training.rs`.

## Layout

```text
scripts/    build_seat_dataset.py, train_seat_classifier.py,
            evaluate_seat_classifier.py, live_infer_seats.py,
            extract_wednesday_test.py
dataset/    generated .npz train/test splits (gitignored)
models/     trained seat_cnn_<variant>.pt checkpoints (gitignored)
results/    evaluation heatmap PNGs (gitignored)
```

`live_infer_seats.py` is the persistent stdin/stdout NDJSON worker behind the
dashboard Simulator tab's LIVE INFERENCE toggle: heimdall-service spawns it
once per run, streams assembled `(20, 64)` CIR-magnitude frames in, and reads
one `{seat, seat_index, probs, frame_id, ts}` prediction per line back. It
exits on stdin EOF and deliberately avoids matplotlib for fast startup.

Seat labels are `FrontLeft=0, FrontRight=1, BackRight=2, BackLeft=3`
everywhere; the dashboard displays "Rear Left/Right" but always sends these
identifiers. Dataset clips are extracted folders named
`clip-<id>-<Label><Person>/` containing `aligned-cirs.ndjson` and
`metadata.json`.

## Usage

```sh
python scripts/build_seat_dataset.py --dataset-dir <extracted-clips> [--out-root DIR]
python scripts/train_seat_classifier.py --data-dir data_raw|data_calibrated \
    [--dataset-root DIR] [--epochs N]
python scripts/evaluate_seat_classifier.py [--split test] [--checkpoint <path.pt>]
```

`--out-root`/`--dataset-root` default to `dataset/` next to `scripts/`, and
models save to `models/`. The dataset builder writes both raw and calibrated
variants; the calibrated variant is skipped with a warning when clips lack a
consistent frozen board reference. Requires the pinned interpreter with torch
(see `requirements.txt`); the service defaults to
`HEIMDALL_PYTHON=C:\Users\qc_de\AppData\Local\Programs\Python\Python311\python.exe`
and `HEIMDALL_SEATCLASS_ROOT=<this folder>` — see `unoq/dashboard/README.md`.

`extract_wednesday_test.py` is a one-off cross-session test-set builder for
the wednesday-new-test captures (`datasets/wednesday-new-test/` holds the
zipped originals; the script expects them extracted, see its docstring).
