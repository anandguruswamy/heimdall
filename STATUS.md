# Heimdall Status

## Project state

Scaffold created. No Heimdall-specific multi-node beacon protocol has been
flashed yet.

## Starting point

- Open DW3000/Zephyr Phase 1 bring-up is proven on two DWM3001CDKs.
- SPIM3 at 32 MHz passed DEV_ID, bidirectional traffic, CIR reads, and
  scheduled-TX tests on both boards.
- Native nRF52833 USB CDC is proven and suitable for the gateway path.
- Existing captures and live-CIR tools are available under `firmware/tools`.

## Current host-tooling state

- Native ARM64 Windows setup is working with the Windows x86-64 Zephyr SDK
  0.17.4 under emulation.
- Zephyr, the pinned DW3000 driver, CMSIS, Nordic HAL, and SEGGER modules are
  fetched in the `firmware/` west workspace.
- CMake 3.31.10, Ninja, DTC, west 1.5.0, and the `arm-zephyr-eabi` compiler
  are installed and detected.
- The 8 MHz rollback and 32 MHz SPIM3 profiles both build successfully with
  USB CDC enabled. The build outputs are under `firmware/build-radio/` and
  `firmware/build-radio-spim3/`.
- The baseline fixes included fetching the missing DW3000 west module,
  including the USB DeviceTree overlay, and deleting the inactive SPI1 radio
  node before defining the SPIM3 radio node.
- Builds still emit non-fatal warnings for an empty console library, unused
  role functions, and a deprecated SPI driver macro.
- The current board target is `nrf52833dk/nrf52833`; underscore-form names are
  retained only for overlay/devicetree filenames.

## Hardware validation

- Board 1 (`760223921`) was flashed successfully through the UNO Q using its
  native Linux ARM64 J-Link tools and the J9 connection.
- Image: 8 MHz rollback profile, USB CDC-enabled `zephyr.hex`.
- J-Link reported successful flash download and verification (`O.K.`) for the
  nRF52833 target. D9 is blinking green, confirming the firmware reaches its
  heartbeat loop after radio initialization.
- The 32 MHz SPIM3 image was then flashed and verified on the same board; D9
  continued blinking green after the switch.
- Board 2 (`760197419`) was flashed with the 32 MHz SPIM3 image through the
  UNO Q and also shows the green D9 heartbeat.
- Board 1 (`760223921`) was flashed with the scheduled-TX role image from
  `firmware/build-radio-tx/`; J-Link reported successful flash download and
  verification (`O.K.`).
- Board 2 (`760197419`) was flashed with the sensing-RX role image from
  `firmware/build-radio-rx/`; J-Link reported successful flash download and
  verification (`O.K.`).
- Both role images use the nRF52833 target, 32 MHz SPIM3 radio overlay, and
  USB CDC configuration.
- The previously proven continuous `ROLE_TX`/`ROLE_RX` pair was rebuilt with
  the 8 MHz rollback overlay and J-Link VCOM console enabled. Board 1
  (`760223921`) reached TX IRQ 500/500 with zero start failures or timeouts;
  board 2 (`760197419`) received more than 400 frames with three RX errors
  during the capture. Both reported DEV_ID `0xDECA0302`, channel 9, 6.8 Mbps,
  and successful radio initialization. This confirms the board wiring and
  PHY path; the regression is in the newer scheduled-TX/sensing-RX role pair
  or its USB-only runtime path.
- The same continuous `ROLE_TX`/`ROLE_RX` test was repeated with the 32 MHz
  SPIM3 overlay. Board 1 (`760223921`) reached TX IRQ 500/500 with zero start
  failures or timeouts; board 2 (`760197419`) received more than 400 frames
  with zero RX errors during the capture. Both reported successful 32 MHz
  radio initialization.
- The current scheduled/sensing pair was then tested with J-Link VCOM console
  enabled at 32 MHz SPIM3. Board 1 (`760223921`) completed at least 800
  scheduled TX IRQs with zero reported timing error; board 2 (`760197419`)
  completed at least 700 sensing RX frames and CIR reads with zero RX errors.
  Scheduled radio operation is therefore verified; native USB CDC export is
  the remaining runtime-path issue to resolve before beacon framing.
- Native USB CDC export was then verified at 32 MHz SPIM3. The missing
  configuration was `CONFIG_PHASE1_CIR_DUMP=y`; without it, the sensing RX
  callback never queued CDC records. With it enabled in `radio/app/usb.conf`,
  the UNO Q captured 16,384 bytes containing 59 `CIR2` records from board 2.

## Next executable checkpoint

Define and implement the first Heimdall beacon frame over the verified
scheduled-TX and USB CDC paths, then add gateway heartbeat, validation, and
capture/replay coverage.
