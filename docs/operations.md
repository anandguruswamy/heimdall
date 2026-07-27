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

The ingest service writes raw bytes before parsing and uses a separate SQLite
commit cadence. Stop it with `SIGTERM`; it completes the record currently being
read and checkpoints both stores. After an abrupt exit, the next start marks
stale runs and connections interrupted and catalogs every surviving segment
before opening a new epoch. Only one process may own a database.

Run `python3 -m heimdall.verify_h3 LIVE_DB ARCHIVE_ROOT REPLAY_DB` after replay.
Require `equivalent: true`, `integrity: ok` for both databases, no segment
issues, matching raw and observation digests, and no unexplained sequence gaps.
The first summary after attaching a reader may report drops accumulated while
the gateway had no host; subsequent summaries establish steady-state behavior.
