# USB CDC Contract v1

Status: **normative and implemented.** Supersedes `usb-cdc-v0.md`.

`usb_contract_version = 1`.

Defines the byte stream from the gateway to the UNO Q over the DWM3001 native
USB CDC connection. The companion radio contract is `contracts/beacon-v1.md`;
design rationale is in `docs/protocol-decisions.md`, principally items 26 and 27.

Requirement keywords MUST, MUST NOT, SHOULD, and MAY are used in the RFC 2119
sense. All multi-byte fields are little-endian. `u40` is five bytes, least
significant first.

The link is unidirectional in v1: the gateway transmits, the host receives. No
host-to-gateway commands are defined.

---

## 1. Why this contract constrains the radio

The record sizes below are not merely descriptive. They feed the gateway
throughput calculation in `tools/config/heimdall_config.py`, which is compared
against `budget.usb_budget_bytes_per_s`, which determines whether a beacon
configuration may be exported at all.

Changing any overhead here changes the set of admissible radio configurations.
The constants are therefore mirrored in the model and MUST be kept in step.

---

## 2. Outer framing

Every record is wrapped identically.

```
offset  size  field       notes
------  ----  ----------  --------------------------------------------------
   0    u16   sync        0xA5C3
   2    u8    version     usb_contract_version, 1
   3    u8    type        section 3
   4    u8    flags       section 2.2
   5    u8    reserved    MUST be 0
   6    u16   length      payload length in bytes, excluding this header
                          and the trailing CRC
   8    u32   sequence    monotonic, allocated once per attempted record;
                          dropped attempts therefore create observable gaps
  12    ...   payload     length bytes
12+len  u32   crc32       IEEE CRC-32 over offsets 2 .. 11+len inclusive
```

Framing overhead is **16 bytes** per record: 12 header, 4 CRC.

The CRC deliberately excludes `sync`, so that a resynchronising receiver can
scan for the sync word without it participating in the checksum.

`crc32` uses the reflected IEEE polynomial `0xEDB88320`, initial value
`0xFFFFFFFF`, and final XOR `0xFFFFFFFF`, matching `crc32_ieee()` in firmware
and `zlib.crc32()` on the host.

### 2.1 Receiver behaviour

A receiver MUST:

- Locate `sync`, read `length`, and verify `crc32` before interpreting any
  payload byte.
- Discard and count records failing CRC, then resynchronise by scanning forward
  for the next `sync`.
- Treat a gap in `sequence` as lost records and account for it separately from
  CRC failures. These have different causes: a `sequence` gap means the gateway
  dropped records at the producer, whereas a CRC failure means the USB transport
  corrupted them.
- Ignore unrecognised `type` values, skipping `length` bytes.

A receiver MUST NOT pass any record to fusion before CRC validation.

`sync` MAY occur inside a payload by coincidence. Receivers MUST rely on
`length` and `crc32` for framing and MUST treat sync scanning purely as a
recovery mechanism.

### 2.2 `flags`

| Bit | Meaning |
| --- | --- |
| 0 | One or more records were dropped immediately before this one. |
| 1 | Gateway is not synchronised to the radio schedule. |
| 2-7 | Reserved, MUST be transmitted as 0 and ignored on receipt. |

Bit 0 is a hint only. `sequence` is authoritative for loss accounting.

---

## 3. Record types

| Value | Name | Payload | Cadence |
| --- | --- | --- | --- |
| 0x01 | `HELLO` | 36 B | At startup and every 1 s |
| 0x02 | `HEARTBEAT` | 12 B | Every 500 ms |
| 0x03 | `RADIO_FRAME` | 8 + `frame_len` | Per frame received over the air |
| 0x04 | `LOCAL_OBS` | 5 + `subreport_bytes` | Per peer observed, per cycle |
| 0x05 | `CYCLE_SUMMARY` | 34 B | Once per cycle |
| 0x06 | `ERROR` | 8 + optional text | On error |
| 0x07 | `TX_RECORD` | 13 B | Per frame the gateway transmitted |
| 0x08-0xFF | Reserved | | |

---

## 4. `HELLO`, 0x01

Everything the host needs to decode the stream. A host MUST NOT decode
`RADIO_FRAME` or `LOCAL_OBS` before receiving a `HELLO`, because subreport and
frame sizes depend on it.

```
u8   heimdall_protocol_version   beacon contract version
u8   usb_contract_version        this contract, 1
u8   n_nodes                     N
u8   m_slots_per_superslot       M
u8   node_id                     the gateway's own schedule position
u8   master_node_id
u8   cir_taps
u8   cir_left_taps
u16  config_hash
u16  subreport_bytes
u16  frame_payload_bytes
u16  max_frame_bytes
u32  slot_duration_us
u32  cycle_us
u64  device_id                   FICR->DEVICEID
u32  firmware_id                 build identifier, 0 if unavailable
```

Total 36 bytes.

Repeating it every second lets a host attach mid-capture. A host MUST treat a
change in `config_hash` as invalidating all prior decoding state.

---

## 5. `HEARTBEAT`, 0x02

```
u32  uptime_ms
u32  cycles_completed
u8   sync_state         0 unsynchronised, 1 synchronised, 2 master bootstrapping
u8   evidence_age       as defined in beacon-v1.md section 8.4
u16  reserved           MUST be 0
```

Total 12 bytes.

`HEARTBEAT` MUST continue during radio silence. Its absence indicates the
gateway itself has failed, which is distinct from the radio having gone quiet.

---

## 6. `RADIO_FRAME`, 0x03

One record per frame received over the air, forwarded **verbatim**.

```
u40  rx_timestamp     gateway-local RX timestamp, 40-bit
u8   rx_flags         section 6.1
u16  frame_len        bytes of frame data that follow
...  frame            frame_len bytes, exactly as received
```

Wrapper 8 bytes, so the record costs `16 + 8 + frame_len` bytes on the wire.

The frame body begins at the 802.15.4 `fctrl` field and ends at the last payload
byte. The hardware FCS is **not** included; it has already been verified by the
radio and carrying it would waste bandwidth on the binding constraint.

The gateway MUST NOT modify, reorder, decode, or re-encode the frame body.
Per-subreport CRC32 values are computed by the *observing* node
(`beacon-v1.md` section 9); any re-encoding would leave the host unable to
verify them and would destroy end-to-end integrity.

The gateway MUST NOT strip padding beyond `pooled_total_bytes`, so that the host
can detect firmware faults that leave non-zero padding.

### 6.1 `rx_flags`

| Bit | Meaning |
| --- | --- |
| 0 | Frame passed all validation stages in `beacon-v1.md` section 10.2. |
| 1 | `config_hash` mismatch. Frame is forwarded for diagnosis but MUST NOT be fused. |
| 2 | `protocol_version` unrecognised. |
| 3 | Frame body was truncated because it exceeded the receive buffer. |
| 4-7 | Reserved, MUST be 0. |

Frames rejected by the hardware frame filter never reach the callback and
therefore produce no record. They are counted in `CYCLE_SUMMARY` only.

---

## 7. `LOCAL_OBS`, 0x04

The gateway measures its peers exactly as any other node does, but those
measurements never cross the radio. They are exported directly.

```
u8   reporting_node_id   always the gateway's node_id
u32  k                   superslot in which the observed frame was transmitted
...  subreport           subreport_bytes, byte-identical to beacon-v1.md section 5
```

Wrapper 5 bytes, so the record costs `16 + 5 + subreport_bytes`.

The subreport MUST be byte-identical in layout to one carried over the radio, so
that a host has exactly one subreport decoder and one CRC32 check regardless of
whether an observation arrived directly or via relay.

`reporting_node_id` is carried explicitly rather than implied, so that host code
handling relayed and direct observations is identical.

---

## 8. `TX_RECORD`, 0x07

One record per frame the gateway transmits.

```
u32  k
u8   m
u40  tx_timestamp     programmed transmit time, as placed in the frame header
u16  frame_len
u8   flags            bit 0: transmission confirmed by TXFRS
```

Total 13 bytes.

The gateway cannot receive its own transmissions, so without this record the
host would learn the gateway's transmit timestamps only indirectly, from
`observed_tx_timestamp` inside peers' subreports, and only when some peer heard
it. Ranging on links where the gateway is the transmitter depends on this.

---

## 9. `CYCLE_SUMMARY`, 0x05

Emitted once per cycle. This is the authoritative loss and health record;
it resolves defect D13 in the decision log.

```
u32  k_cycle_start
u32  cycle_index
u16  frames_received
u16  frames_expected            (N-1) * M
u16  fcs_errors
u16  filter_rejects             rejected by hardware frame filtering
u16  validation_rejects         passed FF, failed Heimdall validation
u16  subreport_crc_failures
u16  usb_queue_drops            producer-side, since the previous summary
u16  rx_callback_max_us         longest RX callback observed this cycle
u8   peer_m0_miss[8]            per-peer count of missed m=0 frames
u8   evidence_age
u8   flags                      bit 0: any counter saturated
```

Total 34 bytes.

`peer_m0_miss` is always 8 entries regardless of `N`; entries at or above `N`
MUST be 0. Fixed length keeps the record size independent of configuration, so
the throughput model does not depend on `N` twice.

Counters are per-cycle, not cumulative, and MUST saturate rather than wrap.

`rx_callback_max_us` is the field that closes bring-up gate 1. It makes the RX
callback duration observable in the field rather than measured once on a bench,
and lets the host verify that the configured `slot_duration_us` retains its
intended margin.

`subreport_crc_failures` is zero in the thin-gateway implementation because the
gateway does not parse relayed pooled reports. The UNO Q decoder owns this count
for live ingestion and replay. The field remains reserved in v1 so captures from
older validating gateways retain their meaning without a wire-format change.

---

## 10. `ERROR`, 0x06

```
u16  code
u16  detail
u32  k          0 if not applicable
...  text       optional UTF-8, not NUL-terminated, length implied by the record
```

Minimum 8 bytes.

| Code | Meaning |
| --- | --- |
| 0x0001 | Configuration invariant failed at boot |
| 0x0002 | Device ID does not match the roster entry for this `node_id` |
| 0x0003 | Radio initialisation failed |
| 0x0004 | Frame received with `src_addr` equal to own `node_id` |
| 0x0005 | Transmission inhibited by `config_hash` mismatch |
| 0x0006 | Transmission inhibited by liveness rule |
| 0x0007 | Scheduled transmission missed its deadline |
| 0x0008 | Receive buffer overflow |

An implementation MUST emit `ERROR` for any condition that inhibits
transmission, so that a silent network is always explained.

---

## 11. Backpressure

The radio MUST NOT block on USB. This is a hard requirement from `AGENTS.md`:
radio timing is independent of USB backpressure.

The gateway therefore snapshots received frame bytes and any local observation
into a bounded preallocated FIFO. One export thread serializes every record type,
assigns USB sequence numbers, computes outer CRC32 values, and submits complete
records to the CDC queue. This single producer preserves wire order without
performing USB framing in the DW3000 callback.

The gateway maintains a bounded queue with **drop-newest** behaviour: when the
queue is full the producer fails to enqueue and increments `usb_queue_drops` at
the point of loss. Drops MUST NOT be silent. Summary creation atomically claims
the accumulated count so multiple queued summaries cannot acknowledge the same
drops; a failed summary enqueue restores its claimed count.

Records MUST NOT be partially written. A record is either fully enqueued or
dropped whole.

Drop-newest was chosen over drop-oldest so that loss is counted precisely at the
producer and archived captures end with a quantified tail gap rather than
interior holes. The cost is that a real-time dashboard lags during a transient.

### 11.1 Throughput

Per cycle the gateway emits:

```
(N-1) * M  RADIO_FRAME   at 16 + 8 + (frame_bytes - 2) each
(N-1)      LOCAL_OBS     at 16 + 5 + subreport_bytes each
M          TX_RECORD     at 16 + 13 each
1          CYCLE_SUMMARY at 16 + 34
```

plus `HELLO` and `HEARTBEAT`, which are negligible.

At N=6, 64 taps, `M=2`, `frame_bytes=773`, `subreport_bytes=296`, cycle 24.0 ms,
this is about **403 kB/s**. Across N=2..8 it stays within 234-433 kB/s, because
the radio runs saturated regardless of configuration.

The interrupt-driven FIFO path has sustained a verified 475 kB/s offered load,
which covers the current 433 kB/s model maximum with limited headroom. The
configuration tool MUST still reject configurations whose calculated load
exceeds `budget.usb_budget_bytes_per_s`.

---

## 12. Capture and replay

A capture is the raw byte stream, unmodified. Replay MUST be byte-exact and MUST
reproduce identical fusion output, which requires that no decoding step depend on
arrival wall-clock time.

`sequence` gaps in a capture MUST be preserved rather than renumbered, so replay
exercises the same loss-handling paths as live operation.

---

## 13. Changes from v0

| v0 | v1 |
| --- | --- |
| `SYNC \| VERSION \| TYPE \| FLAGS \| LENGTH \| SEQUENCE \| PAYLOAD \| CRC32` sketch | Exact offsets and widths, section 2 |
| `BEACON`, `RANGE_RECORD`, `CIR_RECORD` | Replaced by `RADIO_FRAME` (verbatim relay) and `LOCAL_OBS` (gateway's own measurements) |
| `ROUND_SUMMARY` | `CYCLE_SUMMARY`, with the full counter set from decision item 26 |
| No transmit record | `TX_RECORD`, required for ranging on gateway-transmitted links |
| "reports producer drops separately from USB transport gaps" | `usb_queue_drops` versus `sequence` gaps versus `crc32` failures, three distinct mechanisms |
| Overheads undefined | 16 B outer, plus 8 B for `RADIO_FRAME` and 5 B for `LOCAL_OBS`, feeding the configuration budget |
