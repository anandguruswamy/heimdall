# Heimdall

Heimdall is the serious execution project for a room-scale, multi-node UWB
sensing system.

## Goal

Multiple fixed DWM3001 nodes exchange scheduled UWB beacons. Each node records
peer observations, including timestamps, CFO, range metadata, and CIR windows.
One node is attached to an Arduino UNO Q over USB CDC. The UNO Q acts as the
fusion hub: it receives the UWB backhaul, validates and archives observations,
estimates geometry, and runs the sensing pipeline.

## Current scope

1. Prove one gateway and one peer exchange a stable beacon stream.
2. Forward decoded observations over native USB CDC to the UNO Q.
3. Add capture/replay so the fusion pipeline is testable without radios.
4. Implement the fixed-slot multi-node beacon schedule.
5. Add range/CIR fusion, geometry calibration, and sensing outputs.

The first implementation uses the open Zephyr + DW3000 driver path. The closed
FiRa/BLE experiments remain useful for lessons and fixtures, but BLE is not the
Heimdall data plane.

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
