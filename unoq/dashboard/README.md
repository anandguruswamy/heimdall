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

## Simulator tab

The Simulator tab renders a Three.js cabin-occupancy scene for four seats (FL, FR, RL, RR). Seat states arrive through the `SeatFeed` interface in `src/lib/simulator-feed.ts`; the bundled `MockSeatFeed` emits changing multi-seat states and powers the temporary test-control panel. Replacing it with a real backend client only requires passing a different `SeatFeed` implementation to `SimulatorScene` in `App.svelte` — the scene component consumes `SeatState` updates exclusively through `subscribe()`.

The scene attempts to load a Tesla Model Y model from `public/models/tesla-model-y.glb` (not bundled). If you add one, it must be license-safe (CC0 or CC-BY) and its author, source, and license must be recorded here. The loader normalizes glTF's +Z-forward convention to the scene's nose-at-minus-Z frame (`MODEL_YAW`) and clips all model geometry above `CUTAWAY_Y` (1.15 m) so the seats and occupants stay visible regardless of the asset's roof materials. Until a model is provided, a stylized primitive SUV body renders as the fallback; the render loop and mock-feed timer run only while the tab is active and the document is visible.
