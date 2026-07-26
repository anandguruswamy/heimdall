# USB CDC Contract v0

Status: **superseded by `usb-cdc-v1.md`.** Retained for history only. Do not
implement against this document.

v0 sketched the framing shape and the intended record names before the radio
contract existed. v1 defines exact offsets, widths, and record payloads, and
its overheads feed the gateway throughput budget that governs which radio
configurations may be built.

## Original text

```text
SYNC | VERSION | TYPE | FLAGS | LENGTH | SEQUENCE | PAYLOAD | CRC32
```

The gateway exports `HELLO`, `HEARTBEAT`, `BEACON`, `RANGE_RECORD`, `CIR_RECORD`,
`ROUND_SUMMARY`, and `ERROR` messages. The UNO Q validates length and CRC before
decoding. Invalid frames are counted and discarded; they never reach fusion.

The gateway maintains bounded queues and reports producer drops separately from
USB transport gaps.

## What changed in v1

| v0 | v1 |
| --- | --- |
| Framing shape only | Exact byte offsets and widths |
| `BEACON`, `RANGE_RECORD`, `CIR_RECORD` | `RADIO_FRAME` (received frames forwarded verbatim) and `LOCAL_OBS` (the gateway's own measurements) |
| `ROUND_SUMMARY` | `CYCLE_SUMMARY` with the full counter set |
| No transmit record | `TX_RECORD`, required for ranging on links the gateway transmits |
| Overheads unspecified | 16 B outer framing, 8 B `RADIO_FRAME` wrapper, 5 B `LOCAL_OBS` wrapper |

The v0 requirement to report "producer drops separately from USB transport gaps"
was correct and is now realised as three distinct mechanisms: `usb_queue_drops`
counted at the producer, `sequence` gaps, and `crc32` failures.

The decision to forward frames verbatim rather than decode them into
`CIR_RECORD` messages follows from per-subreport CRC32 being computed by the
observing node. Re-encoding at the gateway would leave the host unable to verify
it.
