# UWB Beacon Contract v0

Status: **superseded by `beacon-v1.md`.** Retained for history only. Do not
implement against this document.

v0 sketched the required semantic fields before any of the widths, timing, or
failure behaviour had been worked out. The design review that produced v1 is
recorded in `docs/protocol-decisions.md`, and an accessible explanation of the
resulting scheme is in `docs/beacon-protocol-explained.md`.

## Original text

Each scheduled slot contains one beacon. The beacon identifies the sender and
round, carries the sender's scheduled TX timestamp, and carries bounded reports
from prior receptions. CIR data may be fragmented across beacons.

Required semantic fields:

```text
protocol_version
sender_node_id
round_id
slot_id
beacon_sequence
scheduled_tx_timestamp
report_count
fragment_index
fragment_count
payload_crc
```

The implementation must define exact binary widths before flashing a protocol
profile. Payload budget is a hard constraint: six-peer CIR data cannot be
assumed to fit in one extended frame.

## What changed in v1

| v0 | v1 |
| --- | --- |
| `sender_node_id` u16 | u8 `src_addr` in an 802.15.4 MAC header; `N_MAX = 8` |
| `round_id`, `slot_id` | `k` (u32 superslot counter) and `m`; schedule is `k mod N` |
| `fragment_index`, `fragment_count` | Removed. Fixed `M` frames per superslot with balanced byte-stream packing. |
| `payload_crc` over the beacon | Per-subreport CRC32, end-to-end from the observing node to the host |
| `report_count` | `subreport_count` plus `peer_observed_bitmap` |
| Widths undefined | Fully specified |
| No configuration binding | `config_hash`, roster device-ID binding, build-time verification |

The v0 warning about payload budget was correct and drove the outcome: a
six-peer report is 1480 B at 64 taps and 2760 B at 128 taps, against a 990 B
per-frame capacity, so multi-frame reports are structural rather than
exceptional.
