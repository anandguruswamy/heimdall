# Operations

## Board roles

- `gateway`: one fixed node attached to UNO Q USB CDC.
- `fixed`: stationary UWB node participating in every scheduled round.
- `mobile`: future optional node; not part of the first acceptance target.

## Required run metadata

Every run records date/time and layout, node IDs and hardware serials, firmware
hashes and build profiles, PHY settings and CIR window, slot plan and round
period, USB device path and UNO Q software version, packet counts, sequence
gaps, CRC failures, and producer drops.

## Recovery

If the gateway is unavailable, preserve the radio capture and replay it through
the UNO Q pipeline. If the UNO Q is unavailable, nodes must continue operating
without blocking their radio schedule.
