# Seat And Person Classification

This directory contains the Python dataset, training, evaluation, and live
inference layer for cropped UWB CIR magnitude matrices. It has no dependencies
beyond those already listed in `requirements.txt`.

## Dataset Contract

Capture folders are named `clip-<id>-<Seat><Person>`. Seats are exactly
`FrontLeft`, `FrontRight`, `BackRight`, `BackLeft`, and `Empty`. Occupied clips
must include a person suffix; `Empty` must not. Service exports also include
`training-label.json`, which preserves the exact person name (including spaces
and punctuation) and takes precedence over the legacy folder suffix. Build both targets with:

```sh
python scripts/build_seat_dataset.py --dataset-dir <clips> --out-root dataset \
  --link-mode canonical --taps-left 8 --taps-right 24
```

`canonical` is the default and retains the ten links where `from < to`;
`directed` retains all twenty directed links. A frame needs only the links
selected by this mode. Each link's complex IQ is centered using deterministic
half-away-from-zero rounding of `marker_aligned`, cropped inclusively to
`[center-left, center+right]`, and complex-zero-padded before magnitude is
taken. `data_raw` is always written. `data_calibrated` is written only when all
selected frozen full-complex references exist and are consistent across clips;
the reference is subtracted before cropping.

Each split stores `X`, `seat_y`, compatibility alias `y`, `person_y` (`-1` for
Empty), `person_names`, `person`, `clip`, `frame`, `link_order`, `link_mode`,
`taps_left`, and `taps_right`.

## Multi-Label Dataset Contract

`build_car_dataset_multilabel.py` labels each frame with a 4-bit multi-hot
vector — one independent occupied bit per seat, in bit order `FrontLeft`,
`FrontRight`, `BackRight`, `BackLeft`; Empty is the all-zero vector. It accepts
every clip: `<Seat><Person>` (one bit), `Empty` (no bits), and
`<Seat><Seat>TwoPeople` (one bit per named seat, person recorded as
`multiple`). A `training-label.json` sidecar with a `"seats"` list is
authoritative when present; folder-name parsing (substring containment) is the
fallback, which is why person labels must never contain a seat class name or
`TwoPeople`. Features are the full 64 aligned CIR taps over all 20 directed
links with no marker-centered cropping, declared in the npz by `crop="full"`.

```sh
python scripts/build_car_dataset_multilabel.py --dataset-dir <clips> \
  --out-root dataset [--stamp ""]
```

The default `--stamp 2026-08-05` reproduces the original
`data_{raw,calibrated}_2026-08-05_multilabel` folders (the default
`--dataset-dir` documents the original capture location on the team member's
PC); `--stamp ""` writes `data_{raw,calibrated}_multilabel`, which the service
uses. Each split stores `X`, multi-hot `y (N,4)`, `label_name`, `person`
(`none`/`multiple` where identity is unavailable), `clip`, `frame`,
`link_order`, `seat_names`, `link_mode`, and `crop`. `build_car_dataset.py`
and `build_car_dataset_with_empty.py` are the single-label 4-/5-class variants
of the same source data, kept for parity.

## Training

```sh
python scripts/train_seat_classifier.py --dataset-root dataset --data-dir data_raw \
  --mode seat|person|separate|joint|multilabel --architecture standard|lite \
  --epochs 30 --patience 5 [--device cpu] [--shuffle-labels] [--tag <suffix>]
```

`seat` predicts five seats. `person` predicts captured people plus an `n/a`
class for Empty. `separate` trains those tasks with independent backbones in
one run. `joint` shares a backbone and ignores Empty (`person_y=-1`) in person
loss. Both architectures infer link/tap dimensions from `X`; `lite` uses
16/32/32 channels and a smaller dense layer.

`multilabel` trains four independent per-seat sigmoid detectors with
`BCEWithLogitsLoss` on a multi-hot dataset (`--multi-label` is an accepted
legacy alias, and `--data-dir data_raw` resolves `data_raw_multilabel` when it
exists). `--threshold` sets the per-seat decision threshold (default 0.5).
Metrics are per-seat precision/recall/F1/accuracy, subset (exact-match)
accuracy, mean bit accuracy, and a combination-level confusion matrix; the
manifest additionally declares `crop:"full"` and `threshold`, and `variant` is
the exact `raw`/`calibrated` string the service matches for calibration.

Every run writes one schema-v2 `.pt` bundle and matching `.manifest.json` under
`models/`. Bundles include preprocessing, feature geometry, classes, weights,
and test metrics/confusions. The manifest is the JSON-safe inference contract
without weights or normalization arrays. The trainer's final line is compact
JSON prefixed by `HEIMDALL_RESULT `.

Legacy team-trained multilabel checkpoints (`seat_cnn_*_multilabel.pt`, marked
only by `multi_label: true`) get their manifests generated with:

```sh
python scripts/write_multilabel_manifest.py --checkpoint models/<bundle>.pt
```

## Inference And Evaluation

```sh
python scripts/live_infer_seats.py --checkpoint models/<bundle>.pt
python scripts/evaluate_seat_classifier.py --checkpoint models/<bundle>.pt \
  --dataset-root dataset --data-dir data_raw --split test
```

Live inference reads NDJSON containing `frame_id`, `ts`, and a cropped
`magnitude` matrix matching the readiness feature shape. It supports all
schema-v2 modes and legacy seat checkpoints. Outputs use `raw_seat*` and/or
`raw_person*`; seat-capable models also retain `seat`, `seat_index`, and `probs`
aliases. A predicted Empty seat forces `raw_person` to `n/a`. Evaluation uses
the seat head for seat, separate, joint, and legacy bundles, and fails clearly
for person-only and multilabel bundles.

Multilabel checkpoints (schema-v2 `model_mode: multilabel`, or legacy bundles
with `multi_label: true`) emit sigmoid bits instead of softmax classes:
`raw_seat_bits` (four probabilities in bit order), `raw_seat_occupied`
(booleans at the threshold), `raw_occupied_seats`, and `raw_occupied_count` —
never `seat`/`probs` keys. The readiness line reports `mode: "multilabel"`,
`seat_bits`, the effective `threshold` (`--threshold` overrides the checkpoint
value, default 0.5), and `features.crop`.

Run unit tests with:

```sh
python -m unittest scripts.test_classifier_pipeline
```
