# Heimdall

Heimdall is a room-scale, multi-node UWB sensing and scene-reconstruction
system built around DWM3001 radios and an Arduino UNO Q gateway.

## Goal

Multiple fixed DWM3001 nodes exchange scheduled UWB beacons. Each node records
peer observations, including timestamps, CFO, range metadata, and CIR windows.
One node is attached to an Arduino UNO Q over USB CDC. The UNO Q acts as the
fusion hub: it receives the UWB backhaul, validates and archives observations,
estimates geometry, and runs the sensing pipeline.

## Current status

- The N=5/M=2 radio profile is hardware-qualified with all five nodes active.
  Each 35 ms cycle provides 20 directed links at 28.571 Hz with 64 complex CIR
  taps per observation.
- The gateway exports the full roster over native USB CDC without steady-state
  loss at the modeled 187,200 B/s load while keeping radio timing independent
  of USB backpressure.
- The Rust service validates, archives, and replays observations and serves the
  live DSP dashboard. The deployed split path can forward validated records
  from the UNO Q agent to the server over direct Ethernet or Wi-Fi.
- Range/CIR processing, board-geometry estimation, and experimental 3D
  multistatic backprojection are implemented. Compact neural scene
  reconstruction is specified as a research direction, not yet a validated
  sensing result.
- Per-board antenna-delay and phase-center calibration remain outstanding, so
  current timestamps must not be presented as accurate metric ranges.

Radio firmware uses the open Zephyr + DW3000 driver path. The closed FiRa/BLE
experiments remain useful for lessons and fixtures, but BLE is not the Heimdall
data plane. See [STATUS.md](STATUS.md) for detailed build, deployment, and
hardware-validation records.

## Read first

- [docs/architecture.md](docs/architecture.md)
- [docs/protocol.md](docs/protocol.md)
- [docs/lessons-learned.md](docs/lessons-learned.md)
- [docs/bring-up-plan.md](docs/bring-up-plan.md)
- [STATUS.md](STATUS.md)

## Project map

- `firmware/`: node and gateway radio firmware.
- `contracts/`: versioned wire and semantic contracts.
- `unoq/`: UNO Q ingest, fusion, storage, and dashboard.
- `host-tools/`: capture, decode, and analysis utilities.
- `tests/`: protocol, replay, and fusion tests.
- `captures/`: local raw and processed data; ignored by default.
- `deployment/`: node roster, slot plan, and UNO Q configuration.

## Technical papers

- [Offset-Free Joint Time-of-Flight, Clock-Rate, and Clock-Drift Estimation](docs/papers/joint-tof-clock-estimation.html)
- [Geometry-Conditioned Neural 3D Scene Reconstruction from Multistatic UWB Channel Impulse Responses](docs/papers/neural-uwb-scene-reconstruction.html)

## License

Heimdall is licensed under the [MIT License](LICENSE).
