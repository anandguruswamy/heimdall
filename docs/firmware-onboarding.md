# Firmware Onboarding

This guide gets a new contributor from an identified DWM3001CDK board to the
Heimdall radio firmware workspace.

## Mental model

The firmware runs on the nRF52833 inside a DWM3001CDK and controls its DW3110
UWB radio. Zephyr supplies the RTOS and board support. Heimdall adds custom
DeviceTree overlays, Kconfig roles, radio behavior, and eventually the beacon
and USB protocols.

The main path is:

```text
DWM3001CDK hardware
  -> Zephyr board support and DeviceTree overlays
  -> DW3000 driver
  -> app/src/main.c role dispatch
  -> scheduled TX / sensing RX / TWR
  -> USB CDC or J-Link VCOM output
```

Start with `docs/firmware-glossary.md` if any term in this guide is unfamiliar.

## Hardware identification

1. Connect one board at a time through **J9**, not J20.
2. Read the J-Link serial number and record it in
   `deployment/board-inventory.md`.
3. Mark the physical board with its inventory label.
4. Treat the VCOM COM number as a temporary observation, not a stable ID.

J9 is used for programming, debugging, and J-Link VCOM. J20 is reserved for
testing the native USB CDC data path.

## Workspace layout

The repository root contains the project documentation and source. The west
workspace root is `firmware/`:

```text
firmware/
  .west/
  radio/                         Heimdall manifest and application
  zephyr/                        pinned Zephyr checkout
  modules/lib/                   external Zephyr modules
  build-radio/                   generated build output
```

Generated west dependencies and build output must remain ignored and must not
be committed.

## Windows tooling

The tested ARM64 Windows setup uses:

- Python 3.12 recommended by Zephyr; keep all packages in one environment
- west 1.5.0
- CMake 3.31.10
- Ninja 1.13.x
- DeviceTree compiler 1.6.x
- Zephyr SDK 0.17.4
- `arm-zephyr-eabi` toolchain
- 7-Zip and `wget` for SDK installation

Zephyr SDK 0.17.4 provides Windows x86-64 bundles rather than Windows ARM64
bundles. Windows ARM can run that SDK under emulation; the nRF52833 compiler
has been verified to execute this way.

The pinned Zephyr tree is not compatible with CMake 4.4.0. Use CMake 3.31.10.

## Fetch the workspace

From the repository root, initialize the local manifest once:

```powershell
cd firmware
west init -l radio
west update
```

The manifest pins the Zephyr revision, the DW3000 driver, CMSIS, Nordic HAL,
and SEGGER module revisions. Use `west list` to verify the fetched projects.

## Build the baseline application

From `firmware/`, use the current Zephyr board target:

```powershell
$usbOverlay = (Resolve-Path radio/app/boards/nrf52833dk_nrf52833_usb.overlay).Path.Replace('\', '/')
west build -p always --no-sysbuild -b nrf52833dk/nrf52833 radio/app -d build-radio -- "-DOVERLAY_CONFIG=radio/app/usb.conf" "-DEXTRA_DTC_OVERLAY_FILE=$usbOverlay"
```

The default board overlay is the 8 MHz SPI rollback profile. The 32 MHz SPIM3
profile is selected by adding an absolute overlay path, for example:

```powershell
west build -p always --no-sysbuild -b nrf52833dk/nrf52833 radio/app -d build-radio -- "-DOVERLAY_CONFIG=radio/app/usb.conf" "-DEXTRA_DTC_OVERLAY_FILE=C:/path/to/Heimdall/firmware/radio/app/boards/nrf52833dk_nrf52833_spim3.overlay"
```

The absolute path avoids CMake resolving the extra overlay relative to the
generated build directory. Include the USB overlay in any build that enables
`usb.conf`.

## Understand the build inputs

- `app/src/main.c`: hardware initialization and role dispatch
- `app/src/primitives.c`: scheduled TX and sensing RX
- `app/src/twr.c`: single-sided ranging
- `app/src/usb_cir_stream.c`: bounded USB CIR output queue and writer thread
- `app/Kconfig`: role and feature choices
- `app/prj.conf`: base configuration
- `app/usb.conf`: USB configuration fragment
- `app/boards/*.overlay`: board pins, buses, radio, LED, and USB hardware
- `west.yml`: pinned dependency manifest

## Flashing and serial output

Flashing uses the J-Link connection on J9. On ARM64 Windows, the UNO Q can be
used as a remote Linux ARM64 flashing host: connect J9 to the UNO Q, copy the
`.hex` image there, and invoke its native `JLinkExe` with the board's J-Link
serial number. This avoids the Windows ARM USB-driver path.

The current verified host command sequence is equivalent to:

```bash
printf 'connect\nloadfile /tmp/heimdall-board1-8mhz.hex\nr\ng\nq\n' \\
  | JLinkExe -device nRF52833_xxAA -if SWD -speed 4000 \\
      -SelectEmuBySN 760223921
```

The J-Link package is documented in `tools/README.md`.

Do not use J20 as a substitute for J9 when programming the board. J20 is the
native USB CDC path intended for gateway transport testing.

## Current build notes

Both the 8 MHz rollback profile and the 32 MHz SPIM3 profile now build. The
baseline fixes were fetching the missing west driver module, including the USB
DeviceTree overlay, and deleting the inactive SPI1 radio node from the SPIM3
overlay before defining the SPI3 instance.

The builds still emit non-fatal warnings for an empty console library, unused
role functions, and a deprecated SPI driver macro. These should be cleaned up
separately from protocol work.

## Where to continue

- Read `firmware/radio/BRINGUP-NOTES.md` for proven Phase 1 measurements.
- Read `docs/architecture.md` for the complete system data flow.
- Read `docs/beacon-protocol-explained.md` for how the beacon scheme works and
  why.
- Read `contracts/beacon-v1.md` and `contracts/usb-cdc-v1.md` before changing
  wire formats. The `v0` files are superseded and retained only for history.
- Read `docs/protocol-decisions.md` for the rationale behind any protocol
  decision, and the alternatives that were rejected.
- Sizing is derived, not hand-written. Change
  `deployment/beacon-config.example.json`, not the constants; the build verifies
  it against `tools/config/heimdall_config.py` and fails on any disagreement.
- Use `STATUS.md` to record board IDs, build profiles, PHY settings, and test
  results for hardware changes.
