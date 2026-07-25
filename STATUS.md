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
- A clean firmware build is currently blocked by project configuration: the
  SPIM3 overlay defines duplicate `dw3000` labels, and the current Kconfig
  configuration emits warnings treated as errors.
- The current board target is `nrf52833dk/nrf52833`; underscore-form names are
  retained only for overlay/devicetree filenames.

## Next executable checkpoint

Build a two-board Heimdall beacon profile, flash one as gateway and one as peer,
and verify stable scheduled transmission/reception, gateway USB CDC heartbeat
and framed records, no radio timing degradation when USB is disconnected or
slow, and deterministic capture/replay on the UNO Q.
