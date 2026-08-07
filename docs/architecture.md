# Heimdall Architecture

## Data flow

```text
Node radio
  -> scheduled beacon
  -> peer RX timestamp/CFO/CIR
  -> next beacon report payload
  -> gateway receives the same radio traffic
  -> gateway USB CDC stream
  -> UNO Q decoder and validator
  -> append-only observation store
  -> fusion engine
  -> dashboard/API and derived products
```

The gateway is a normal UWB node plus a USB exporter. It must not become a
special timing authority merely because it is tethered to Linux.

## Guarantees

- **Radio timing never waits for the USB link.** When the host falls behind,
  the gateway drops the newest records from its bounded queues with
  sequence-visible producer drops rather than stalling the radio schedule.
- Every record carries a protocol version, node identity, round identity,
  sequence information, and integrity check; see [protocol.md](protocol.md) and
  `contracts/`.

## Modules and seams

### Radio firmware module

Owns PHY setup, slot timing, beacon construction, RX diagnostics, CIR reads,
and bounded queues. Its external interface is the versioned beacon contract.

### USB gateway module

Owns CDC framing, record export, heartbeat, firmware identity, and loss
counters. USB writes are queued and may be dropped or summarized when the
radio-side queue is at risk.

### UNO Q ingest module

Owns serial discovery, framing, CRC validation, version negotiation, duplicate
handling, sequence-gap detection, and durable append of valid records.

### Fusion module

Consumes normalized observations, not serial packets. It produces geometry,
range quality, CIR-derived features, motion energy, and later vital-signal
estimates. It must also accept replayed observations.

### Dashboard module

Reads derived state and health metrics. It is not responsible for decoding
firmware packets or making radio decisions.
