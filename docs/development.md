# Heimdall Development

Everything needed to build, deploy, run, and test Heimdall from scratch on the
validated Windows ARM64 host. The repository's README is a compact overview;
this file is the from-scratch guide.

## Prerequisites

### Hardware

- 5x DWM3001CDK nodes by default (DWM3001CDK/nRF52833, DW3110 radio); the
  project supports 2–8 nodes.
- 1x Arduino UNO Q as the fusion hub/gateway host.
- 1x J-Link (J9 connection) for programming boards, optionally hosted on the
  UNO Q.

### Host toolchain

The validated build machine is a Windows ARM64 Copilot+ PC. Tools used:

- Python 3.12 (for west/Zephyr and the reference Python suite).
- Rust 1.93.1 with the `aarch64-pc-windows-gnullvm` host toolchain and the
  `aarch64-unknown-linux-gnu` target, plus Zig 0.15.2 for cross-linking.
- Node.js 24.x and npm 11.x (dashboard build).
- For firmware: west 1.5.0, CMake 3.31.10, Ninja 1.13.x, DeviceTree compiler
  1.6.x, Zephyr SDK 0.17.4 with the `arm-zephyr-eabi` toolchain, 7-Zip, and
  wget.

Every host tool and the exact cached assets and checksums are documented in
[tools/README.md](../tools/README.md). Installers are cached under the ignored
`tools/installers/` directory.

## 1. Firmware

From the repository root, initialize the west workspace and build the radio
application:

```powershell
cd firmware
west init -l radio
west update
west build -p always --no-sysbuild -b nrf52833dk/nrf52833 radio/app -d build-radio -- "-DOVERLAY_CONFIG=radio/app/usb.conf"
```

### Build profiles

- The default build uses the 8 MHz SPI rollback overlay.
- The 32 MHz SPIM3 profile used by the validated deployment adds the absolute
  overlay path documented in [firmware-onboarding.md](firmware-onboarding.md).
- Node-specific images are bound at build time to a node ID, the board's FICR
  `DEVICEID` words, and per-board antenna delays; see
  `deployment/node-roster.lab.yaml` and `deployment/beacon-config.n5.json` for
  the current N=5/M=2 profile.

### Flashing

Flash each board through J9 with J-Link:

```bash
printf 'connect\nloadfile /tmp/heimdall-boardN.hex\nr\ng\nq\n' \
  | JLinkExe -device nRF52833_xxAA -if SWD -speed 4000 -SelectEmuBySN <jlink-serial>
```

The J-Link can be hosted on the UNO Q and driven with its native Linux ARM64
tools.

## 2. UNO Q Rust service

Build the Debian ARM64 release on the Windows host (never on the UNO Q):

```powershell
.\tools\build-linux-arm64.ps1 -Release
```

The deployable binary is written to
`target/aarch64-unknown-linux-gnu/release/heimdall-service`. The Svelte
dashboard is embedded at build time; rebuild it first if the UI changed:

```sh
cd unoq/dashboard
npm install
npm run check
npm run build
```

## 3. Deployment

Transfer the binary to the UNO Q and deploy with the tracked launcher:

```bash
scp target/aarch64-unknown-linux-gnu/release/heimdall-service \
  arduino@<unoq-ip>:/home/arduino/.local/bin/heimdall-service
```

- `unoq/deploy/run-heimdall.sh` starts the service.
- A systemd unit (`unoq/deploy/heimdall.service`) and a rootless `@reboot`
  crontab launcher are provided.
- The UNO Q connects over SSH as `arduino@<unoq-ip>`.
- Split path: run `heimdall-service agent` on the UNO Q and
  `heimdall-service server` on the Windows host to view the dashboard locally.

## 4. Run and usage

### Live system

1. Power the DWM3001 nodes and the gateway node; the gateway node must be
   attached to the UNO Q over native USB CDC (J20).
2. Start `heimdall-service` on the UNO Q (`deploy/run-heimdall.sh`).
3. Open the dashboard at `http://<host>:8080`.
4. Health and topology: `GET /api/health` and `GET /api/topology`.

Runtime data is stored on the UNO Q under `/home/arduino/heimdall-data`.

### Capture and replay

- Arm a protected 30-second capture: `POST /api/v1/clips`; poll with
  `GET /api/v1/clips`; download or delete with `GET|DELETE /api/v1/clips/{id}`.
- Replay canonical observations from a `.husb` capture through the reference
  Python path:

```bash
python3 -m heimdall.replay_ingest data/raw/connection-000001 data/replay.sqlite3
python3 -m heimdall.verify_h3 data/heimdall.sqlite3 data/raw data/replay.sqlite3
```

- `host-tools/radar-map/` replays captures into 3D multistatic backprojection
  volumes and serves XY/XZ/YZ slices.

See [operations.md](operations.md) for run metadata requirements and recovery
procedures.

## 5. Tests

- Protocol/contract suite (Python): `python -m pytest tests/`
- Rust unit tests: `.\tools\test-host.ps1`
- Cross-compiled UNO Q tests: `.\tools\test-linux-arm64.ps1`
- Frontend checks and build: `npm run check && npm run build` in
  `unoq/dashboard`
- Live acceptance audit across desktop and phone: from `unoq/dashboard`,
  `npm run audit:live -- "http://<host>:8080" /tmp/heimdall-tab-audit`

## Troubleshooting

- **Ranging accuracy.** Per-board antenna-delay and phase-center calibration is
  outstanding; current boards use the bring-up value. Do not treat timestamps
  as accurate metric ranges until calibrated.
- **USB throughput.** Linux captures must put the ACM TTY in raw mode;
  canonical-line-discipline artifacts invalidate throughput measurements.
- **Build environment.** Confirm every tool version against the manifest in
  [tools/README.md](../tools/README.md); the validated host is a Windows ARM64
  Copilot+ PC.

## Related documents

- [architecture.md](architecture.md) — data flow and design guarantees.
- [protocol.md](protocol.md) — UWB beacon and USB-CDC contracts.
- [firmware-onboarding.md](firmware-onboarding.md) — Zephyr/DW3000 bring-up.
- [operations.md](operations.md) — run metadata and recovery.
