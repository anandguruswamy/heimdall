# Heimdall

> Entry for the **Snapdragon Multiverse Hackathon 2026** (Qualcomm, San Diego).

Heimdall is a room-scale, multi-node UWB sensing system built around five
DWM3001 radios and an Arduino UNO Q gateway — a privacy-first radar you can
build from a handful of $30 dev boards. It senses people and space without
cameras, running the whole sensing pipeline on the UNO Q.

![Heimdall system topology: a Snapdragon host and Arduino UNO Q gateway linked to a five-node DWM3001 UWB mesh](docs/assets/topology.svg)

## Key numbers

- 5 UWB nodes → **20 directed links** every 35 ms cycle
- **28.571 Hz** per link, **64** complex CIR taps per observation
- **Lossless 187 kB/s** USB-CDC export from the gateway
- All sensing and fusion on the **UNO Q** (Qualcomm QRB2210) — the live
  dashboard is served on-device; no cloud

## How it works

Five fixed radios talk on a custom beacon protocol: each node takes a turn
sending a ping, and every other node captures the ping's reflections — the RF
echoes that bounce off walls, furniture, and people. Each echo profile (a
channel impulse response, or CIR) is like a mini radar image of the room.

One node is the gateway: it streams the full set of observations to an Arduino
UNO Q over a plain USB cable. The UNO Q validates and archives every record,
fuses the links into range, CIR, and spectrum products, and serves a live
dashboard. No cameras, no cloud — the radar is five radios and one board.

## Application description

Multiple fixed DWM3001 nodes exchange scheduled UWB beacons. Each node records
peer observations, including timestamps, CFO, range metadata, and CIR windows.
One node is attached to an Arduino UNO Q over USB CDC. The UNO Q acts as the
fusion hub: it receives the UWB backhaul, validates and archives observations,
estimates geometry, and runs the sensing pipeline. A live dashboard shows
distance, CIR, and spectrum streams, a 3D board-position solve, and a radar-map
reconstruction of the environment, all computed on-device.

## Current status

The N=5/M=2 radio profile is hardware-qualified with all five nodes active at
28.571 Hz per link and lossless steady-state gateway export at 187 kB/s. See
[STATUS.md](STATUS.md) for the full build, deployment, and hardware-validation
log.

## Team

| Name | Email (@gmail.com)|
| --- | --- |
| Anand Guruswamy | anand.me |
| Ehsan Hosseini | ehsan.hosseini |
| Jianxiu Li | lijianxiu2019 |
| Saisundar Sridharan | saisundar2  |
| Simarjit Singh | simar.rajput |

## Setup instructions

Heimdall has three buildable parts: node/gateway radio firmware, the UNO Q
Linux runtime (Rust service + embedded Svelte dashboard), and host-side
analysis tools. The full from-scratch guide, complete toolchain manifest with
checksums, and troubleshooting are in [docs/development.md](docs/development.md).

### Prerequisites

Hardware:

- 5x DWM3001CDK nodes by default (DWM3001CDK/nRF52833, DW3110 radio); the
  project supports 2–8 nodes.
- 1x Arduino UNO Q as the fusion hub/gateway host.
- 1x J-Link (J9 connection) for programming boards, optionally hosted on the
  UNO Q.

Host: a Windows ARM64 Copilot+ PC is the validated build machine. The complete
toolchain (Python 3.12, Rust 1.93.1 + Zig 0.15.2 for cross-linking, Node.js
24.x, and the Zephyr toolchain) is listed with exact versions and checksums in
[docs/development.md](docs/development.md) and
[tools/README.md](tools/README.md).

### 1. Firmware

From the repository root, initialize the west workspace and build the radio
application:

```powershell
cd firmware
west init -l radio
west update
west build -p always --no-sysbuild -b nrf52833dk/nrf52833 radio/app -d build-radio -- "-DOVERLAY_CONFIG=radio/app/usb.conf"
```

Node images are bound at build time to a node ID, the board's FICR `DEVICEID`
words, and per-board antenna delays; see `deployment/node-roster.lab.yaml` and
`deployment/beacon-config.n5.json`. The 32 MHz SPIM3 profile used by the
validated deployment is documented in
[docs/firmware-onboarding.md](docs/firmware-onboarding.md). Flash each board
through J9 with J-Link (commands in [docs/development.md](docs/development.md)).

### 2. UNO Q Rust service

Build the Debian ARM64 release on the Windows host (never on the UNO Q):

```powershell
.\tools\build-linux-arm64.ps1 -Release
```

Rebuild the embedded Svelte dashboard first if the UI changed:

```sh
cd unoq/dashboard
npm install
npm run check
npm run build
```

The deployable binary is written to
`target/aarch64-unknown-linux-gnu/release/heimdall-service`.

### 3. Deployment

Transfer the binary to the UNO Q and deploy with the tracked launcher:

```bash
scp target/aarch64-unknown-linux-gnu/release/heimdall-service \
  arduino@<unoq-ip>:/home/arduino/.local/bin/heimdall-service
```

`unoq/deploy/run-heimdall.sh` starts the service; a systemd unit
(`unoq/deploy/heimdall.service`) and a rootless `@reboot` crontab launcher are
provided. The UNO Q connects over SSH as `arduino@<unoq-ip>`; see AGENTS.md.

## Run and usage instructions

1. Power the DWM3001 nodes and the gateway node; the gateway node must be
   attached to the UNO Q over native USB CDC (J20).
2. Start `heimdall-service` on the UNO Q (`deploy/run-heimdall.sh`), or in the
   split path run `heimdall-service agent` on the UNO Q and `heimdall-service
   server` on the Windows host to view the dashboard locally.
3. Open the dashboard at `http://<host>:8080`. Health and topology:
   `GET /api/health` and `GET /api/topology`.

Capture and replay: arm a protected 30-second capture with
`POST /api/v1/clips`, download or delete with `GET|DELETE /api/v1/clips/{id}`,
and replay `.husb` captures through the reference Python path. Full commands,
run metadata requirements, and recovery procedures are in
[docs/operations.md](docs/operations.md) and
[docs/development.md](docs/development.md).

## Tests

- Protocol/contract suite (Python): `python -m pytest tests/`
- Rust unit tests: `.\tools\test-host.ps1`; cross-compiled UNO Q tests:
  `.\tools\test-linux-arm64.ps1`
- Frontend checks and build: `npm run check && npm run build` in
  `unoq/dashboard`

See [docs/development.md](docs/development.md) for the full test guide.

## Notes

- Per-board antenna-delay and phase-center calibration remain outstanding, so
  current timestamps must not be presented as accurate metric ranges.
- Design guarantees — radio timing kept independent of USB backpressure via
  bounded, drop-newest queues with sequence-visible producer drops, and
  per-record version/node/round/sequence/integrity fields — are detailed in
  [docs/architecture.md](docs/architecture.md) and
  [docs/protocol.md](docs/protocol.md).

## Project map

- `firmware/`: node and gateway radio firmware.
- `contracts/`: versioned wire and semantic contracts.
- `unoq/`: UNO Q ingest, fusion, storage, and dashboard.
- `host-tools/`: capture, decode, and analysis utilities.
- `tests/`: protocol, replay, and fusion tests.
- `captures/`: local raw and processed data; ignored by default.
- `deployment/`: node roster, slot plan, and UNO Q configuration.

## Read more

- [Architecture](docs/architecture.md) — data flow, modules, and design
  guarantees.
- [Protocol](docs/protocol.md) and
  [beacon-protocol-explained.md](docs/beacon-protocol-explained.md) — the
  UWB beacon and USB-CDC contracts.
- [Topology](docs/topology.md) — node roster and radio profile.
- [Development guide](docs/development.md) — build, deploy, run, and test from
  scratch.
- [Firmware onboarding](docs/firmware-onboarding.md) — Zephyr/DW3000 build
  paths and board bring-up.
- [Operations](docs/operations.md) — running the live system and recovery.
- [DSP and sensing](docs/live-cir-alignment.md),
  [multistatic UWB backprojection](docs/multistatic-uwb-mgbp-cgbp.md), and
  [seat classification](host-tools/seat-classification/README.md).
- [Hardware validation log](STATUS.md).
- [Lessons learned](docs/lessons-learned.md).

## Technical papers

- [Offset-Free Joint Time-of-Flight, Clock-Rate, and Clock-Drift Estimation](https://htmlpreview.github.io/?https://github.com/anandguruswamy/heimdall/blob/main/docs/papers/joint-tof-clock-estimation.html)
- [Geometry-Conditioned Neural 3D Scene Reconstruction from Multistatic UWB Channel Impulse Responses](https://htmlpreview.github.io/?https://github.com/anandguruswamy/heimdall/blob/main/docs/papers/neural-uwb-scene-reconstruction.html)

## References

- Zephyr RTOS and the open DW3000 driver (pinned in `firmware/radio/west.yml`).
- DWM3001CDK and DW3110 documentation from Qorvo.
- [docs/papers/](docs/papers/) for the ranging and reconstruction techniques
  behind the processing pipeline.

## License

Heimdall is licensed under the [MIT License](LICENSE).
