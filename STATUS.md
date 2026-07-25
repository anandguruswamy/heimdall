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

## Next executable checkpoint

Build a two-board Heimdall beacon profile, flash one as gateway and one as peer,
and verify stable scheduled transmission/reception, gateway USB CDC heartbeat
and framed records, no radio timing degradation when USB is disconnected or
slow, and deterministic capture/replay on the UNO Q.
