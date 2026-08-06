# Heimdall Dashboard

Pinned Svelte 5, TypeScript, and Vite instrument UI for the UNO Q service. Install and verify with:

```sh
npm install
npm run check
npm run build
```

The browser uses `/api/health`, `/api/topology`, `/api/settings`, and `/api/calibration` for bootstrap state. Settings are written with `PUT /api/settings`. Calibration starts one independent 10-second collection with `POST /api/calibration/snapshot`, refreshes with `GET /api/calibration`, solves local tape entries through `POST /api/calibration/solve` using `{references_m}`, and applies through `POST /api/calibration/apply` with `{}`.

`/api/stream` carries FlatBuffers `Envelope` messages with identifier `HMT1`. The dashboard includes a bounds-checked decoder for schema version 1 and parses the envelope payload as UTF-8 JSON. Topic payloads identify directed links with zero-based `from` and `to` fields. Plot arrays are normalized to `Float32Array`; scalar distance and CFO samples are retained as rolling 256-sample histories.

Recognized payload fields include:

| Topic | Fields |
| --- | --- |
| distance | metre-valued scalar `raw_ss`, `smoothed_ss`, `raw_ds`, `smoothed_ds` histories |
| cir | top-level `magnitude` and 16x `resampled` arrays |
| waterfall | incremental `row` and `width`, retained as a bounded 64-row per-link ring |
| slow-fft | flat `values`, `width`, and `height` |
| fast-fft | `magnitude`, `phase` |
| cfo | `raw_ppm`, `filtered_ppm` |
| calibration | GET state `{live,applied}` and solve output `rank`, `condition_number`, `recommended_next_pair`, `board_offsets`, and `residuals` |

When the backend is disconnected or a selected topic/link has not produced data, realistic synthetic frames remain available. Live frames always take precedence.

## Presence Detection tab

The Presence Detection tab renders a Three.js cabin-occupancy scene for four seats. Seat states arrive through the `SeatFeed` interface in `src/lib/simulator-feed.ts`; live five-class predictions map `Empty` to no occupied chair and carry the stable person name on the occupied seat. The cabin overlay shows that name only above an occupied chair. Person-only checkpoints are also accepted: all chairs remain unassigned and the stable person name appears in the center of the cabin. The bundled `MockSeatFeed` still powers the temporary test controls.

The scene attempts to load a Tesla Model Y model from `public/models/tesla-model-y.glb` (not bundled). If you add one, it must be license-safe (CC0 or CC-BY) and its author, source, and license must be recorded here. The loader normalizes glTF's +Z-forward convention to the scene's nose-at-minus-Z frame (`MODEL_YAW`) and clips all model geometry above `CUTAWAY_Y` (1.15 m) so the seats and occupants stay visible regardless of the asset's roof materials. Until a model is provided, a stylized primitive SUV body renders as the fallback; the render loop and mock-feed timer run only while the tab is active and the document is visible.

### Live model inference

The LIVE INFERENCE toggle runs live CIR data through a trained classifier and drives the 3D occupancy from its predictions. `POST /api/inference/start` (`{model}`, chosen from `GET /api/inference/models`) spawns one persistent `live_infer_seats.py` process. Schema-v2 checkpoints have a matching `.manifest.json` that defines model mode, raw/calibrated variant, selected link order, LOS window, and input shape. The default feature contract keeps the 10 links where `from < to` and 8 taps left plus 24 taps right of `marker_aligned`; 20 directed links and other windows remain selectable. The service rejects inconsistent manifests, assembles only the selected links, applies complex reference subtraction before cropping for calibrated models, and broadcasts predictions on `seat-inference`.

The backend publishes every raw snapshot plus smoothed seat/person output. Presence Detection exposes one common majority-window slider (1-300 snapshots, default 5); inference startup applies that window to both rolling seat probabilities and person-name voting. Stable seat aliases use the five classes `FrontLeft`, `FrontRight`, `BackRight`, `BackLeft`, and `Empty`; Empty maps to no physical occupied seat and forces person to `n/a`. The browser treats backend stable predictions as authoritative and retains its older confidence-gated majority vote only for legacy raw-only checkpoints, avoiding double smoothing. Inference status reports prediction rate, dropped frames, and rolling average/p95 latency. STOP closes the worker stdin and kills it after a 2 s grace if needed; closing every subscribed dashboard stops an unused run after 30 s.

## Training tab

The Training tab captures labeled CIR clips and trains seat/person classifiers without leaving the dashboard. Tags use the five seat classes above; occupied clips require a person label, while Empty clears and disables person. Tags persist in backend SQLite metadata; `seat: null` removes a tag and `exclude: true` leaves it out of training. Untagged and CIR-less clips never train.

The Captured Clips table uses a leading checkbox as the explicit training-set selector. Selecting one or more rows enables TRAIN and reveals SELECT ALL, UNSELECT, and DELETE bulk actions. The training request sends `clip_ids`; the backend trains only those records and rejects selected captures that are incomplete, untagged, missing CIR data, or absent. Legacy requests without `clip_ids` retain the older all-eligible/non-excluded behavior.

TRAIN accepts variant, mode (`seat`, `person`, `separate`, or `joint`), architecture (`standard` or `lite`), link mode, LOS taps left/right, epochs, and early-stopping patience. One shared dataset stores seat and person targets. Separate mode uses independent backbones in one bundle; joint mode shares a backbone and masks person loss for Empty. Training uses class-weighted loss, deterministic seeds, and patience-based early stopping, then emits a structured result containing per-head accuracy/confusion and the saved checkpoint/manifest paths. Only one run may be active; logs continue to stream through `GET /api/training/status`.

The training scripts live in this repository under `host-tools/seat-classification/scripts/`. Two environment variables configure the backend paths (defaults target this development laptop):

| Variable | Default | Purpose |
| --- | --- | --- |
| `HEIMDALL_PYTHON` | Auto-discovered | Optional interpreter override. Without it, the service probes the toolkit `.venv`, Conda installs, and PATH candidates for working Torch/NumPy interop. |
| `HEIMDALL_SEATCLASS_ROOT` | Auto-discovered | Optional toolkit override. Without it, the service searches ancestors of its working directory and executable for `host-tools/seat-classification`. |
