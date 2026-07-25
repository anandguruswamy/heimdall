# Bring-up Plan

## Gate H0: repeatable firmware

Build and flash the copied open-radio application on both DWM3001CDKs. Record
board serial, firmware hash, PHY profile, and console mapping.

## Gate H1: radio pair

Run gateway/peer beacon exchange at two-board bench distance. Require stable
DEV_ID, IRQ-driven RX, scheduled TX, RX timestamps, CFO, and a 64-tap CIR on
each successful reception.

## Gate H2: USB export

Flash the gateway USB profile. Verify heartbeat, framed records, CRC rejection,
sequence-gap reporting, and behavior while the UNO Q reader is unplugged.

## Gate H3: UNO Q replayable ingest

Run the CDC reader on the UNO Q, persist raw frames, decode them into canonical
records, and replay the same capture through the fusion interface.

## Gate H4: multi-node schedule

Add reserved slots, then a third node. Measure collision rate, round duration,
payload size, and per-pair observation rate before attempting six nodes.

## Gate H5: fusion

Start with range quality and geometry calibration. Add CIR change-energy
backprojection only after observation completeness and clock metadata are
validated.
