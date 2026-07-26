# Beacon Protocol Decision Log

Status: **all 30 agenda items resolved.** This is the working record of the
superslot beacon protocol design review and is the input to the next revision of
`contracts/beacon-v0.md`. The contract itself is not updated until these
decisions are approved.

Do not build or flash against this document. Several decisions depend on
measurements that have not been taken; see **Bring-up gates** at the end.

---

## Protocol sketch under review

`N` boards `B_0 .. B_(N-1)`. Time is divided into **slots**; `M` consecutive
slots form a **superslot**; superslots are counted by a free-running counter
`k`. Board `B_i` transmits only in superslots where `k mod N == i`.

When `B_i` transmits it emits a **pooled report** `P_i` spanning the `M` slots
of its superslot, containing subreports `U_(i,j)` for every `j != i`, plus its
transmit timestamp, `k`, and integrity check.

`U_(i,j)` is `B_i`'s PHY observation of `B_j`'s transmission in superslot
`k - ((i - j) mod N)`, carrying the CIR, first-path index, RX timestamp, CFO,
and gain state.

Full cycle = `N` superslots = `N * M` slots. Every ordered link `(i,j)` is
reported once per cycle.

---

## Resolved wire format

### Frame, 31 B header

```text
--- 802.15.4 MAC header, 9 B (item 29) ----------------------
0    u16  fctrl                 0x8841
2    u8   mac_seq               free per-frame counter
3    u16  dest_pan              = network_id, matched by hardware FF
5    u16  dest_addr             = 0xFFFF broadcast
7    u16  src_addr              = node_id (0..N-1)
--- Heimdall header, 22 B -----------------------------------
9    u8   protocol_version      offset must never move (item 14)
10   u8   frame_type
11   u8   m                     slot index within superslot
12   u32  k                     superslot counter (item 8)
16   u8   N
17   u8   M
18   u16  config_hash           (items 10, 27)
20   u40  tx_timestamp          programmed, not actual (item 13)
25   u8   subreport_count       subreports beginning in this frame
26   u16  pooled_total_bytes    valid bytes; rest is padding (items 17, 18)
28   u8   peer_observed_bitmap  bit j = subreport for j present (item 25)
29   u8   evidence_age          liveness (item 21)
30   u8   flags
--- payload -------------------------------------------------
31   pooled report bytes [m * frame_payload, (m+1) * frame_payload)
--- FCS, 2 B ------------------------------------------------
```

### Subreport `U_(i,j)`, 40 B + CIR

```text
u8   observed_node_id       j
u8   obs_flags              cir_valid (item 30), truncated, fp_valid
u8   observed_m             always 0 (item 15), retained as assertion
u8   round_delta            = (i-j) mod N, retained as check
u40  observed_tx_timestamp  copied from the observed frame's header
u40  rx_timestamp           local
i16  cfo_raw
u16  fp_index_q10_6         raw Q10.6, not rounded
u24  F1
u24  F2
u24  F3
u24  ip_power
u16  accum_count
u8   dgc_decision
u16  cir_start_offset       absolute accumulator index
u8   cir_taps               <= 128 (item 3)
i16  cir[2 * cir_taps]      int16 I/Q, >>2 from 18-bit (item 4)
u32  crc32                  end-to-end, computed by B_i (item 11)
```

### Derived sizing

```text
subreport_size = 40 + 4 * cir_taps
P_max          = (N - 1) * subreport_size
capacity_max   = max_frame_bytes - 31 - 2
M              = ceil(P_max / capacity_max)
frame_payload  = ceil(P_max / M)              <- balanced (item 18)
frame_bytes    = frame_payload + 33
T_slot         >= airtime(frame_bytes) + rx_processing(cir_taps) + margin
                  quantised to 0.1 ms (item 6)
cycle          = N * M * T_slot               packed, no idle (item 1)
per_link_rate  = 1 / cycle
```

### Worked examples, N=6, `max_frame_bytes` = 1023

| | 64 taps | 128 taps |
| --- | --- | --- |
| `subreport_size` | 296 B | 552 B |
| `P_max` | 1480 B | 2760 B |
| `M` | 2 | 3 |
| `frame_payload` | 740 B | 920 B |
| `frame_bytes` | 773 B | 953 B |
| Airtime | 1.03 ms | 1.28 ms |
| `T_slot` | 1.7 ms | 2.2 ms |
| Cycle | 20.4 ms | 39.6 ms |
| Per-link rate | 49.0 Hz | 25.3 Hz |
| Gateway USB | ~451 KB/s | ~431 KB/s |

USB load is roughly invariant in `N` and `cir_taps`, as predicted: the radio runs
saturated regardless, so throughput is approximately
`frame_payload / T_slot`. Both examples fit the interrupt-driven CDC path's
verified 475 kB/s offered load, although the 64-tap case leaves little headroom.

---

## Reference data (measured or derived, not decisions)

### Airtime

Channel 9, PRF 64 MHz, PLEN 128, 8-symbol SFD, 6.8 Mb/s, standard PHR.

```text
SHR   = 128 * 1017.63 ns + 8 * 1017.63 ns  = 138.4 us
PHR   = 21.5 us
DATA  = ceil(bits / 330) * 378 * 128.21 ns
```

The RS(63,55) code adds 48 parity bits per 330 data bits.

**Correction, caught by `tests/test_beacon_config.py`.** It was asserted several
times during this review that dividing byte count by 6.8 Mb/s overestimates
airtime by roughly 14 percent through double-counting RS parity. That is wrong
and backwards. The nominal 6.8 Mb/s is already the **net** rate after coding: the
coded symbol rate is `1 / 128.21 ns = 7.8 Mb/s`, and `7.8 * 330/378 = 6.81 Mb/s`.
Dividing bytes by 6.8 Mb/s therefore approximates the data field to within
final-block padding, 0.67 percent at 1023 B.

The real error in a naive estimate is **omitting SHR and PHR**, about 160 us
independent of frame size — 12 percent of a 1023 B frame and 29 percent of a
329 B one. This is now covered by unit tests so it cannot recur silently.

| Frame bytes | Airtime |
| --- | --- |
| 32 | 0.21 ms |
| 600 | 0.89 ms |
| 890 | 1.18 ms |
| 1023 | 1.37 ms |

### Hardware constraints

- Max frame is 1023 B and requires `DWT_PHRMODE_EXT`. The app is currently
  `DWT_PHRMODE_STD` (`firmware/radio/app/src/main.c:74`), capping frames at
  127 B. RX buffers are `RX_BUFFER_LEN 127` and must grow.
- CIR accumulator reads are chunked at 16 samples
  (`CHUNK_CIR_NB_SAMP`), one SPI transaction of `1 + 6*16 = 97` B per chunk.
  64 taps = 4 chunks, 128 taps = 8 chunks.
- CIR sample spacing at PRF 64 is ~1.0016 ns = ~0.3 m of path length.
- All app callbacks run in the Zephyr system workqueue, not an ISR
  (`dw3000_hw.c:66-96`). Frame `n` must be fully processed before frame
  `n+1`'s IRQ is serviced.
- `primitives.c:286` re-arms RX **before** reading the CIR. With a frame in
  every slot the next reception overwrites the accumulator mid-read. Latent
  today at a 20 ms period; a real bug under this protocol.
- Scheduled TX programmed-vs-actual timestamp error measured at **0 DTU**
  across 1000 frames at both 8 and 32 MHz SPI. This is what makes broadcasting
  the *programmed* TX timestamp viable.
- Measured CFO ~ -1.8 ppm, giving ~43 ns of drift per 24 ms cycle.
- No RX callback duration has ever been measured. The only indirect bound is
  the SS-TWR result where a 1 ms turnaround failed at 8 MHz SPI and 3 ms
  succeeded.

### Scaling

Per-link rate falls as roughly `1/N^2`, because `M` itself grows with `N`.
Gateway USB load is approximately `frame_payload / T_slot` and is therefore
**largely independent of both N and `cir_taps`** — the radio runs saturated
regardless.

Computed by `tools/config/heimdall_config.py` at 64 taps, `max_frame_bytes` =
1023, balanced packing, 32 MHz SPI, with `T_slot` set to the feasibility floor.
Includes the report-assembly constraint of item 6a and the USB record overheads
of `contracts/usb-cdc-v1.md`:

| N | `P_max` | M | `frame_bytes` | airtime | `T_slot` floor | binds | cycle | per-link Hz | links | USB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | 296 B | 1 | 329 B | 0.55 ms | 1.4 ms | assembly | 2.8 ms | 357.1 | 2 | 268 KB/s |
| 3 | 592 B | 1 | 625 B | 0.94 ms | 2.0 ms | assembly | 6.0 ms | 166.7 | 6 | 335 KB/s |
| 4 | 888 B | 1 | 921 B | 1.28 ms | 2.5 ms | assembly | 10.0 ms | 100.0 | 12 | 387 KB/s |
| 5 | 1184 B | 2 | 625 B | 0.94 ms | 1.6 ms | reception | 16.0 ms | 62.5 | 20 | 411 KB/s |
| 6 | 1480 B | 2 | 773 B | 1.08 ms | 1.8 ms | reception | 21.6 ms | 46.3 | 30 | 447 KB/s |
| 7 | 1776 B | 2 | 921 B | 1.28 ms | 2.1 ms | reception | 29.4 ms | 34.0 | 42 | 454 KB/s |
| 8 | 2072 B | 3 | 724 B | 1.03 ms | 1.7 ms | reception | 40.8 ms | 24.5 | 56 | 443 KB/s |

Three non-obvious effects:

- **`M = 1` configurations are bound by report assembly, not reception** (item
  6a). N=2, 3, and 4 are all limited by how fast a node can package the
  observation it just made, not by how fast it can receive.
- **`T_slot` is not monotonic in `N`.** Balanced packing shrinks `frame_payload`
  whenever `M` increments, so N=5 has a *shorter* slot than N=4 despite having
  more peers. The tool should surface these discontinuities rather than let a
  configuration sit just above one.
- **USB load stays within 268-454 KB/s** across the whole range, because the
  radio runs saturated regardless. It is not monotonic either.

---

## Decision agenda

### Sizing constraints

| # | Item | Status |
| --- | --- | --- |
| 1 | Required per-link observation rate | Resolved |
| 2 | Maximum N (`N_MAX`) | Resolved |
| 3 | CIR taps and left/right window | Resolved |
| 4 | CIR sample format | Resolved |
| 5 | Extended frames (`DWT_PHRMODE_EXT`) | Resolved |
| 6 | Slot duration and its gating measurement | Resolved |

### Frame and field layout

| # | Item | Status |
| --- | --- | --- |
| 7 | Node ID base and width | Resolved via #2 |
| 8 | `k` width and wrap semantics | Resolved |
| 9 | Per-frame header contents | Resolved |
| 10 | `config_hash` presence and mismatch policy | Resolved |
| 11 | CRC granularity | Resolved |
| 12 | Subreport field list | Resolved |
| 13 | Timestamp encoding | Resolved |
| 14 | Endianness and versioning | Resolved |

### Superslot mechanics

| # | Item | Status |
| --- | --- | --- |
| 15 | Which frame yields `U_(i,j)`; CIR read cadence | Resolved |
| 16 | Note 2 relaxation | Resolved |
| 17 | Fixed vs variable frame count per superslot | Resolved |
| 18 | Subreport-to-frame packing | Resolved |
| 19 | Subreport ordering | Resolved |
| 20 | Gateway pooled-report exemption | Resolved |

### Liveness, sync, failure

| # | Item | Status |
| --- | --- | --- |
| 21 | Liveness rule | Resolved |
| 22 | `master_node_id` configurable | Resolved |
| 23 | Resync policy and conflicting `k` | Resolved |
| 24 | Two-master and stale-config detection | Resolved |
| 25 | Never-heard peer behaviour | Resolved |

### Plumbing

| # | Item | Status |
| --- | --- | --- |
| 26 | USB record types and backpressure policy | Resolved |
| 27 | Config format, build integration, hash scope | Resolved |
| 28 | PHY parameters | Resolved |
| 29 | Frame filtering | Resolved (pulled forward) |
| 30 | RX double-buffering vs callback reorder | Resolved |

---

## Resolved decisions

### 1. Per-link observation rate

**Question.** Should the schedule pack slots back-to-back, or include idle
time so report rate becomes an explicit input?

**Decision.** Packed. `cycle_period = N * M * T_slot` with no idle. Per-link
rate is an **output**, not an input. The configuration tool enforces a USB
budget and refuses to export a configuration that exceeds it.

**Consequences.**

- The USB budget becomes a hard number the tool depends on, so it must be
  **measured**. The current byte-at-a-time `uart_poll_out()` export path
  (`usb_cir_stream.c:81-83`) is not a valid basis for that number. This is now
  a gating measurement alongside the RX callback timing.
- **Sub-question resolved under item 27.** No separate idle or `cycle_period`
  parameter is needed. `slot_duration_us` is bounded below by `slot_floor_us`
  but is otherwise free, so raising it above the floor reduces report rate and
  USB load while keeping every slot occupied and the schedule arithmetic
  uniform. A budget-exceeding configuration can therefore be fixed by
  lengthening the slot, not only by cutting taps.

### 2. Maximum N

**Question.** What maximum N must the encoding support, given node ID width
is paid once per subreport?

**Decision.** **`N_MAX = 8`, u8 node IDs, 0-based.**

This supersedes the earlier `u16 sender_node_id` direction recorded in
`contracts/beacon-v0.md`.

**`N` and `N_MAX` are distinct and must not be conflated.**

- `N_MAX = 8` is an *encoding and sizing ceiling only*. It bounds field
  widths and worst-case buffers. It never appears in the schedule.
- `N` is the actual number of boards in the deployment. The cycle is exactly
  `N` superslots — no padding, no reserved or empty superslots for absent
  boards. At `N = 4` the cycle is 4 superslots, each pooled report carries 3
  subreports, and `M` is derived from 3 subreports rather than 7.

Consequently `M`, cycle length, per-link rate, and USB load are all functions
of the deployed `N`, and changing `N` changes the schedule for every board.

**Node ID is a schedule position, not a board identity.** IDs must be
contiguous `0 .. N-1`, because the schedule is `k mod N == i`. Persistent
board identity (J-Link serial, board label) is separate and is mapped to a
schedule position by the deployment roster. Item 27 must specify that mapping.

**Rationale.** N=8 is the last value where the pooled report still fits M=2 at
64 taps int16; N=9 forces M=3 and nearly halves per-link rate. Designing to 8
keeps the schedule in its efficient regime and makes worst-case buffer sizing
tight and provable.

**Consequences.**

- Fixes defect #1: the original `k mod N == i` with `i` in `1..N` left `B_N`
  unable to ever transmit. Node IDs are now 0-based and the condition is
  exact.
- Worst-case pooled report is 7 subreports.
- `subreport_count`, `m`, `N`, and `M` all fit in single bytes.
- For N in {2,4,8}, `2^32 mod N == 0`, so a u32 `k` wraps with **zero**
  schedule discontinuity. For N in {3,5,6,7} one board skips a turn once per
  `2^32` superslots. See item 8 for the corrected wrap intervals: `k` counts
  **superslots**, not cycles, so the relevant period is `M * T_slot` (4 ms at
  N=6), not the cycle length.

### 3. CIR taps and window

**Question.** How many taps, and what left/right split around the CIA
first-path index?

**Decision.** The encoding must support **up to 128 taps**. The operating tap
count is a configuration parameter; the build adapts by issuing only as many
16-sample chunked SPI reads as required (`ceil(taps / 16)`, so 4 chunks at 64
taps and 8 at 128).

**Consequences.**

- `cir_taps` and the left-window offset both fit in u8.
- Maximum subreport is ~30 B metadata + 512 B CIR = ~542 B at int16, which
  fits inside a single 1023 B frame. A subreport therefore never *needs* to be
  split, at any supported tap count.
- **New finding, affects items 17 and 18.** Although a subreport always fits a
  frame, the "never fragment a subreport" rule causes severe *internal*
  fragmentation at high tap counts. At 128 taps int16 a 542 B subreport leaves
  461 B of a 1003 B payload unusable, so only one subreport fits per frame and
  `M` degrades to `N-1`:

  | 128 taps, N=6 | M | cycle | per-link Hz | USB |
  | --- | --- | --- | --- | --- |
  | Never fragment, int16 | 5 | 60 ms | 16.7 | 271 KB/s |
  | Byte-stream fragmentation, int16 | 3 | 36 ms | 27.8 | 452 KB/s |
  | Never fragment, int8 block-float | 2 | 24 ms | 41.7 | 357 KB/s |

  Allowing the pooled report to be fragmented as a byte stream across the `M`
  frames, with per-subreport CRC and a `pooled_report_byte_offset` in each
  frame header, recovers 66 percent of the rate at 128 taps. This is the
  strongest argument yet for per-subreport CRC over a whole-message CRC:
  it makes partially received pooled reports usable.

### 4. CIR sample format

**Question.** What on-wire format for the 18-bit signed I/Q accumulator
samples?

**Decision.** **int16 I/Q, retaining the existing `>>2` shift** applied after
sign extension from 18 bits (`usb_cir_stream.c:62-63`). 4 B per tap: 256 B at
64 taps, 512 B at 128.

**Rationale.** Roughly 96 dB of intra-subreport dynamic range, which
comfortably covers a 40-60 dB CIR tail. An int8 block-float format would have
halved the payload and held `M = 2` at N=6 with 128 taps, but its ~48 dB range
would bury the weak multipath taps that environmental sensing depends on.
Retaining int16 also preserves compatibility with the validated `CIR2` record
and the existing host decoder, so no host rework is required.

**Consequences.**

- Payload reduction must now come from tap count, slot duration, or packing
  efficiency — not from sample precision.
- The 2 bits discarded by `>>2` remain discarded. This is a known, accepted
  deviation from strict raw preservation, inherited from the working firmware.
- Format is fixed rather than negotiated, so no `cir_format` enum is required
  in the header. If a lossless or reduced profile is ever needed it becomes a
  protocol version bump, not a configuration change.

### 5. Extended frames

**Question.** Enable `DWT_PHRMODE_EXT` for frames above 127 B, and if so at
what maximum?

**Decision.** **Enable `DWT_PHRMODE_EXT`. Maximum frame size is a
configuration parameter bounded 127-1023 B, defaulting to 1023.** The
configuration tool exposes the efficiency-versus-robustness curve so the value
can be tuned per deployment.

**Rationale.** Payload efficiency after SHR, PHR, and RS overhead is 302 KB/s
at 127 B, 473 KB/s at 256 B, 623 KB/s at 512 B, and 732 KB/s at 1023 B.
Staying with STD framing would require three frames per 64-tap subreport and
roughly triple the number of RX callbacks per cycle, which is very likely
unimplementable given callbacks are serialised in the system workqueue.
Against that, packet error rate scales roughly with frame length, so a fixed
1023 B leaves no tuning lever if range performance disappoints.

**Consequences.**

- **Bring-up gate:** EXT framing must be verified board-to-board on real
  hardware before any protocol firmware is flashed. This has never been
  exercised on this target.
- `RX_BUFFER_LEN` grows from 127 to `max_frame_bytes` in both `main.c:21` and
  `primitives.c:16`. Combined with any RX double-buffering decision (item 30)
  this is a material RAM commitment on the nRF52833.
- TX writes above offset 127 use the driver's indirect-pointer path
  (`dw3000_device.c:2218-2246`), which is untested here.
- `max_frame_bytes` joins the set of parameters covered by `config_hash`
  (item 10). A mismatch between boards is fatal: the receiver would truncate.

### 6. Slot duration

**Question.** How is `T_slot` determined and validated, and at what
granularity?

**Decision.** `T_slot` is a **configuration parameter in 0.1 ms steps**. The
tool computes a feasibility floor from measured constants and applies two
thresholds:

- **Hard floor** = `airtime(max_frame_bytes) + rx_processing(taps)`. Below
  this the schedule cannot physically work; export is blocked.
- **Warning band** = hard floor scaled by a safety factor. Feasible but with
  thin margin; the tool warns and still exports.

**Bring-up gate.** The processing model must be calibrated against a real
measurement — GPIO toggle or cycle counter around the RX callback — before it
is trusted. No RX callback duration has ever been measured on this hardware.

**RSTU investigated and rejected as a constraint.** It was proposed that the
board only supports slot durations in multiples of 1 ms (1200 RSTU). This is
not correct:

- `RSTU` appears nowhere in the driver. 1 RSTU = 416 chips / 499.2 Mcps =
  833.33 ns, so 1200 RSTU = 1 ms exactly, but RSTU is an IEEE 802.15.4z
  MAC-layer ranging-round unit. Heimdall builds its own TDMA directly on
  delayed TX and never uses it.
- The real scheduling primitive is `dwt_setdelayedtrxtime()`, which takes the
  high 32 bits of the 40-bit system time with bit 0 ignored
  (`dw3000_device.c:4942-4958`, masked at `primitives.c:95`). Granularity is
  **512 DTU = 8.013 ns**.
- The genuine constraint is arithmetic exactness. `DWT_DTU_PER_MS =
  63897600`, so 1 us = 63897.6 DTU is **not** an integer. The finest exactly
  representable step is **5 us** (319488 DTU = 624 x 512). 0.1 ms and 1 ms are
  also exact.
- Forcing integer ms would round the ~2.3 ms 128-tap minimum up to 3 ms,
  costing ~19 percent of report rate (N=6, M=3: 54 ms cycle at 18.5 Hz versus
  45 ms at 22.2 Hz) for no hardware reason.

**Consequences.**

- Slot duration is uniform across all slots. Per-slot-type durations were
  rejected: they would recover only ~100-200 us per superslot while making
  slot-phase arithmetic non-uniform and complicating sync.

### 6a. Amendment — the report-assembly constraint

The original decision assumed TX preparation (`dwt_writetxdata`) could be
pipelined into the preceding frame's airtime, when the CPU is idle while the
radio receives, and noted that "if that assumption fails, the floor rises by
~270 us". **The assumption does fail, and it was never checked.**

Node `i`'s pooled report must contain `U(i, i-1)`, observed from the peer whose
superslot immediately precedes its own. The bytes being written into the TX
buffer therefore *include the measurement of the frame still being received*.
They cannot exist before that frame has arrived and been processed.

The full chain must complete in `M * T_slot - airtime`:

```text
end of peer m=0 frame
  -> read RX data -> read diagnostics -> read CIR -> compute subreport CRC32
  -> assemble pooled report -> write TX buffer -> program delayed TX
deadline: start of our own m=0 frame
```

**The floor is therefore the larger of two constraints:**

```text
floor_rx       = airtime + margin * rx_processing
floor_assembly = (airtime + margin * (rx_processing + assembly + tx_write)) / M
slot_floor     = quantise_up(max(floor_rx, floor_assembly))
```

Constraint 2 binds **exactly when `M = 1`**. For `M >= 2` the extra slot supplies
ample slack.

| N | M | `floor_rx` | `floor_assembly` | floor | binding |
| --- | --- | --- | --- | --- | --- |
| 2 | 1 | 1100 | 1400 | 1400 | assembly |
| 3 | 1 | 1600 | 2000 | 2000 | assembly |
| 4 | 1 | 2100 | 2500 | 2500 | assembly |
| 5 | 2 | 1600 | 1000 | 1600 | reception |
| 6 | 2 | 1800 | 1100 | 1800 | reception |
| 7 | 2 | 2100 | 1300 | 2100 | reception |
| 8 | 3 | 1700 | 800 | 1700 | reception |

The six-board target is unaffected. N=4 loses 17 percent of its rate.

**Second correction in the same area.** `rx_processing` omitted the subreport
CRC32, which item 11 places in the observing node's callback. At roughly 8 bytes
per microsecond a 296 B subreport costs about 37 us, which the 80 us fixed
overhead allowance did not cover. It is now modelled explicitly via
`budget.crc32_bytes_per_us`. This raised `floor_rx` by 100 us at N=4 and N=7
independently of the assembly constraint.

**Escape hatch, not adopted.** An implementation unable to meet constraint 2
could defer the `round_delta = 1` peer's observation by one full cycle, using the
existing `round_delta` field to express the latency. This costs no time and no
rate. It must be uniform across all cycles rather than conditional, since a
conditional deferral would reintroduce exactly the sampling jitter item 15 exists
to prevent. Recorded in `contracts/beacon-v1.md` section 13.2 as permitted.

**Covered by tests.** `tests/test_beacon_config.py::ReportAssemblyConstraint`
asserts that the floor is the max of both constraints, that assembly binds
exactly when `M = 1`, and that the full chain fits the window at the floor for
every N and tap count.

### 8. Superslot counter width

**Question.** How wide is `k`, and what happens at wrap?

**Decision.** **`k` is u32, free-running. Schedule is `k mod N`.**

**Correction to earlier arithmetic in this log.** `k` counts **superslots**,
not cycles. The relevant period is `M * T_slot` — 4 ms at N=6, M=2, T_slot=2
ms — not the 24 ms cycle. Wrap intervals are therefore u16 = 4.4 minutes,
u24 = 18.6 hours, u32 = 545 years.

**Rationale.** At 545 years the wrap case is unreachable in practice, and for
N in {2,4,8} it cannot occur at all because `2^32 mod N == 0`. The 2 B saved
by u16 is 0.2 percent of a 1023 B frame and would force modulo-2^16
comparison logic into firmware, host decoder, capture format, and every
analysis script for a wrap every 4.4 minutes. u32 also gives the host an
unambiguous global time index across a whole capture with no unwrapping.

Requiring `N` to divide the wrap modulus was rejected: it adds a
non-power-of-two modulo to the hot path and to every host derivation, to fix a
one-instant fairness glitch that occurs once in 545 years.

**Retires defect D2.**

### 9. Per-frame header

**Question.** Does every frame carry a full header, or do config and identity
fields appear only in the `m = 0` frame of a superslot?

**Decision.** **Full 22 B header on every frame.**

**Superseded by item 29.** `sender_node_id` moved to the 802.15.4 source
short address and `network_id` is carried as the PAN ID, so the custom portion
is 21 B behind a 9 B MAC header. The current layout is:

```text
--- 802.15.4 MAC header, 9 B, see item 29 -------------------
u16  fctrl              0x8841
u8   mac_seq            free per-frame counter
u16  dest_pan           = network_id, matched by hardware FF
u16  dest_addr          = 0xFFFF broadcast
u16  src_addr           = node_id (0..N-1)
--- Heimdall header, 21 B -----------------------------------
u8   protocol_version
u8   frame_type
u8   m                  slot index within the superslot
u32  k                  superslot counter
u8   N
u8   M
u16  config_hash
u40  tx_timestamp       programmed, not actual
u8   subreport_count    subreports beginning in this frame
u16  pooled_total_bytes valid pooled-report bytes; rest is padding (item 18)
u8   peer_observed_bitmap  bit j set = subreport for j present (item 25)
u8   evidence_age       liveness, see item 21
u8   flags
```

Custom portion is 22 B, total header **31 B**.

`pooled_offset` was replaced by `pooled_total_bytes` under item 18: with fixed
`M` and a config-constant frame payload size, the offset of frame `m` is
derivable as `m * frame_payload`.

**Rationale.** Any single received frame is then sufficient to sync a cold
node, validate configuration compatibility, and locate its payload within the
pooled report. This makes partially received pooled reports fully usable,
which matters because item 3 showed byte-stream fragmentation is worth 66
percent of the report rate at 128 taps. The tiered alternative saves 10 B per
superslot, 0.5 percent, and degrades exactly the case where robustness
matters: a node that misses `m = 0` could neither sync nor validate from
`m = 1`.

**Retires defect D4.** The original sketch carried a single `Tx_timestamp` for
the pooled report as a whole, but the report spans `M` frames transmitted at
different times. `tx_timestamp` and `m` are necessarily per-frame.

**Consequences.**

- Overhead is 44 B of 2046 B at N=6, M=2, or 2.2 percent.
- `tx_timestamp` is the **programmed** delayed-TX value computed before
  `dwt_writetxdata`, not a measured one. A frame cannot contain its own actual
  TX time. This is viable only because programmed-versus-actual error measures
  0 DTU on this hardware.
- `config_hash` is u16. Collision risk is 1 in 65536 between two genuinely
  different configurations; accepted because the hash is generated at build
  time from a canonical configuration and a collision would be visible then.
- `pooled_offset` and `evidence_age` are provisional pending items 18 and 21.

### 10. Configuration mismatch policy

**Question.** What does a node do when a received frame's `config_hash`
disagrees with its own?

**Decision.** **Stop transmitting, keep receiving, auto-recover.** On a small
number of consecutive mismatched frames the node ceases all transmission while
continuing to receive, log, and (if gateway) export. It resumes automatically
after seeing only matching hashes for a defined interval.

**Rationale.** The failure being prevented is severe: a stale-config board
computes a different `k mod N` schedule, transmits inside another board's
superslot, and corrupts frames indefinitely. Requiring a manual reset was
rejected as impractical during staggered reflashing of six boards. Acting on a
single mismatched frame was rejected because one FCS-passing frame with
corrupted header bytes would silence a healthy node, and a silenced node also
stops contributing liveness evidence to its peers.

The cascade is safe in the important direction: if the master has a stale
config, every peer detects the mismatch and goes silent, so the network stops
cleanly rather than half-running.

**Amendment — mismatch policy must be scoped.** As originally stated this
decision created a denial-of-service vector. A foreign transmitter whose frame
happened to pass the payload tag check but carried a different hash would
silence a healthy node, and a repeating foreign source would take down the
whole network. Validation is therefore **layered**, and the hash policy
engages only at the last stage:

1. PHY acquisition (preamble code, SFD type)
2. `magic` and `network_id`
3. `sender_node_id < N` and plausible `k`
4. `config_hash` comparison, and only here does the transmit-inhibit apply

`network_id` is added to the frame header for stage 2.

### 11. CRC granularity

**Question.** Where does integrity checking live?

**Decision.** **Per-subreport CRC32. No whole-message CRC.** Computed by the
observing node at observation time, carried unchanged through the relay,
verified by the host.

**Rationale.** The 802.15.4 FCS already covers each radio hop and `RXFCG` only
fires on a good FCS, so a frame-level CRC is largely redundant. What the FCS
does not cover is the relay: a subreport sits in `B_i`'s RAM, is copied into a
TX buffer, crosses the air, then crosses USB at the gateway. A per-subreport
CRC covers exactly that window, which is the one hop unique to this protocol.

A whole-report CRC was rejected on two grounds. A pooled report spans `M`
frames, so one lost frame would fail the CRC and force discarding frames that
arrived perfectly. And it would block the byte-stream fragmentation strategy
that item 3 showed is worth 66 percent of the report rate at 128 taps.

CRC32 over CRC16 costs 10 additional bytes per pooled report at N=6, 0.5
percent. Chosen because a silently corrupted CIR would poison a scientific
dataset rather than merely be lost.

**Retires defect D5.**

**Available refinement, not adopted.** Computing the CRC over a *virtual*
header — `reporting_node_id` and `k` prepended but not transmitted — would
bind each observation to its reporting context at zero wire cost, detecting a
relay that mis-attributes a subreport. Remains available.

### 12. Subreport layout

**Question.** What does `U_(i,j)` contain, and how deep should the CIA
diagnostic data go?

**Decision.** **Raw CIA inputs only; the host computes derived values.** 40 B
of metadata plus the CIR.

```text
u8   observed_node_id       j
u8   obs_flags              cir_valid, truncated, fp_valid, ...
u8   observed_m             which frame of B_j's superslot this describes
u8   round_delta            superslots back from current k
u40  observed_tx_timestamp  copied from the observed frame's header
u40  rx_timestamp           local
i16  cfo_raw
u16  fp_index_q10_6         raw Q10.6, not rounded
u24  F1
u24  F2
u24  F3
u24  ip_power
u16  accum_count
u8   dgc_decision
u16  cir_start_offset       absolute accumulator index of the first tap
u8   cir_taps
i16  cir[2 * cir_taps]
u32  crc32
```

Subreport totals: **296 B at 64 taps**, **552 B at 128 taps**.

**Rationale.** `dwt_readdiagnostics_acc()` fills `dwt_cirdiags_t`
(`deca_device_api.h:893-899`) with `F1`, `F2`, `F3` (22-bit), `FpIndex`
(Q10.6), `accumCount`, and `power`. The two derived values consume exactly
those inputs plus `dgc_decision` and `rx_pcode`:

- `dwt_calculate_rssi()` uses `power`, `accumCount`, `dgc_decision`,
  `rx_pcode` (`dw3000_device.c:7958`)
- `dwt_calculate_first_path_power()` uses `F1`, `F2`, `F3`, `accumCount`,
  `dgc_decision`, `rx_pcode` (`dw3000_device.c:7988`)

`rx_pcode` is config-known and costs nothing per subreport. Carrying the raw
inputs lets the host reproduce both values exactly and re-derive them later
under corrected calibration, which carrying only the Q8.8 results would
permanently foreclose. Cost is 10 B over computed-only, 3.5 percent at 64
taps. It also removes two calculation calls from the RX callback, which item 6
identified as the scarce budget.

**Retires defect D8.**

**Field notes.**

- `fp_index_q10_6` is the **raw** Q10.6 value. The firmware currently rounds
  it to an integer sample at `primitives.c:228`; sub-sample first-path
  resolution is exactly what environmental sensing needs and must not be
  discarded.
- `cir_start_offset` is an **absolute** accumulator index, not an offset
  relative to the first path. Unambiguous, and handles clamping at index 0.
- `cir_taps` is per-subreport rather than config-wide, so a window truncated
  near an accumulator edge remains self-describing.
- `observed_tx_timestamp` is copied from the observed frame's header, making
  the subreport self-contained. Without it, a link not involving the gateway
  would be uninterpretable whenever the gateway missed `B_j`'s own
  transmission.
- `round_delta` is derivable as `(i-j) mod N` under the strict rule that only
  the expected superslot may be reported. Retained as a 1 B consistency check
  and to leave room to relax that rule later.

### 13. Timestamp encoding

**Question.** How are the 40-bit DW3000 timestamps encoded, and are 40 bits
actually required?

**Decision.** **Packed 40-bit, 5 bytes little-endian, with a mandated
wrap-safe difference helper** in both firmware and host that all timestamp
arithmetic must route through, mirroring the existing pattern at
`primitives.c:31-39`.

**Bit-width analysis.** Three requirements set the width independently.

*Low bits — resolution.* 1 DTU = 15.65 ps = **4.7 mm** of path length.
Truncating even 4 low bits gives 7.5 cm quantisation, which would dominate the
DW3000's ~10 cm ranging error. No low bits may be dropped.

*High bits — differencing span.* The host never differences timestamps across
clock domains; the domains free-run with an unbounded offset. It differences
only within one node's domain:

```text
round = R_ij - T_i      both in B_i's clock
reply = T_j - R_ji      both in B_j's clock
```

Both span a node's own transmission to its reception of the peer's reply,
which is `(N-1) * M * T_slot`. Truncated timestamps still difference correctly
modulo `2^width` provided the true span fits. A 32-bit field holds
`2^32 * 15.65 ps = 67.2 ms`:

| Config | Span | 32-bit sufficient |
| --- | --- | --- |
| N=6, M=2, 2 ms | 20 ms | yes |
| N=8, M=2, 2 ms | 28 ms | yes |
| N=8, M=2, 3 ms | 42 ms | yes |
| N=6, M=5, 2.5 ms | 62.5 ms | marginal |
| N=8, M=5, 3 ms | 105 ms | **no** |
| N=8, M=7, 2.5 ms | 122 ms | **no** |

32 bits fails in exactly the high-N, high-tap configurations the tool is meant
to explore, and it fails *silently*, producing plausible but wrong ranges.

*What 32-bit would buy.* Two timestamps per subreport plus one per frame
header. At N=6, M=2, 64 taps: `5*2 + 2*1 = 12 B` of ~1540 B, or **0.8
percent**. A 0.8 percent saving for a config-dependent silent failure is a bad
trade.

**Rejected alternatives.**

- *Dropping the redundant low bits of `tx_timestamp`.* DX_TIME ignores bit 0
  and uses the high 32 bits (`primitives.c:95`), so the low 9 bits of a
  programmed TX time are fixed by `TX_ANT_DLY`. Technically redundant, but
  exploiting it saves only 1 byte.
- *Schedule-relative delta encoding.* Could reach ~18 bits while sync holds,
  but overflows into garbage precisely when sync degrades.
- *u64 containers.* Natural alignment and matches the `le64` in the existing
  CIR2 record (`usb_cir_stream.c:55`), but costs 2.4 percent and relies on
  documentation rather than the type system to enforce modulo-2^40 arithmetic.
  Note the radio contract and the USB contract are separate and need not
  agree.

### 14. Endianness and versioning

**Question.** Byte order, and how does the protocol evolve?

**Decision.**

- **Little-endian throughout.** Matches the Cortex-M4, the driver's
  `sys_put_le*` helpers, and the existing CIR2 record.
- **Strict versioning, no extension mechanism.**

The two existing mechanisms have cleanly separated roles:

| Mechanism | Covers | On mismatch |
| --- | --- | --- |
| `protocol_version` (u8) | Field layout and semantics | Stop TX, keep RX, auto-recover |
| `config_hash` (u16) | Parameter *values*: N, M, taps, `T_slot`, preamble code, `max_frame_bytes` | Stop TX, keep RX, auto-recover |

Any field addition, removal, or reinterpretation bumps `protocol_version`.
There is no TLV, no reserved-field padding, and no flags-gated optional field.
`protocol_version` occupies the first byte after the MAC header and that
offset must never move across versions.

**Rationale.** All boards are flashed from one config file by one build
system, so heterogeneous versions are a bug to be detected rather than a
scenario to support. TLV parsing would consume the RX callback budget that
item 6 showed is already tight, and would add a length-field parser that must
be robust against corrupt input. Reserved bytes cost the scarcest resource and
are usually never used.

### 15. CIR read cadence and observation attribution

**Question.** Which of `B_j`'s `M` frames does `U_(i,j)` describe?

**Decision.** **Strictly `m = 0`.** `B_i` reads the CIR on the `m = 0` frame of
`B_j`'s superslot. If that frame is not received with a good FCS, `U_(i,j)` is
omitted for that cycle.

**Rationale — sampling regularity.** Every observation of link `(i,j)` is then
taken at exactly one cycle interval, producing a uniformly sampled time
series. Allowing fallback to a later frame would jitter the sampling instant
by `T_slot`, which is 2 ms on a 24 ms cycle at N=6, M=2 — 8.3 percent. For
phase-based sensing that is material: 2 ms of motion at 1 m/s is 2 mm, roughly
16 degrees of phase at 6.5 GHz. Jitter is technically correctable given
`observed_m` and `rx_timestamp`, but only by moving the host pipeline from FFT
to non-uniform spectral methods. **A clean gap is more useful than a jittered
sample.**

Reading the CIR on every frame was rejected on a prior constraint: only one
CIR per peer per cycle can ever be reported, because payload allows no more.
Selecting the best of `M` would perform `M` times the SPI work for one
reported result, and coherent averaging of `M` frames would require CFO and
timing alignment inside the RX callback for a 1.4x SNR gain at M=2.

**Correction to an earlier claim in this log.** Reading the CIR once per
superslot was previously described as relaxing the slot budget. That is true
only on average. Item 6 fixed slot duration as uniform and sized to the worst
case, so the slot containing the CIR read still sets the minimum. The real
benefits are lower average CPU and power, and spare headroom in `m > 0` slots
for TX preparation and workqueue jitter.

**Consequences.**

- `observed_m` is now always 0. Retained as a 1 B assertion that the relay did
  not misattribute the observation.
- Link `(i,j)` is lost for a whole cycle whenever `m = 0` of `B_j`'s superslot
  is lost. At 1 percent frame error rate that is 1 percent of samples, lost as
  clean gaps.
- **Deferred lever, pending frame-error-rate measurement.** Because packet
  error rate scales with frame length and `m = 0` is now the critical frame,
  making it the *smallest* frame of the pooled report is a free robustness
  gain — pack the fewest subreports into `m = 0`. At N=6, 64 taps this means
  splitting 1480 B as 477 + 1003 rather than 1003 + 477, halving the loss
  probability of the frame that carries the observation instant, with no
  change to `M`. Belongs to item 18. A dedicated *short* `m = 0` beacon was
  also considered but costs a full increment of `M` (24 ms cycle to 36 ms at
  N=6) and should not be adopted without measured FER justifying it.

### 16. Note 2 relaxation

**Question.** Does losing a frame with `m > 0` invalidate `U_(i,j)`?

**Decision.** **No. Only loss of `m = 0` omits `U_(i,j)`.**

**Rationale.** A frame with `m > 0` carries `B_j`'s *relayed* subreports, which
is a different concern from `B_i`'s own PHY observation of `B_j`. Losing it
degrades links `j -> others`, not link `i -> j`. The original Note 2 would
discard a complete, valid measurement because an unrelated frame was lost, and
the penalty scales with `M`: at 1 percent frame error rate the loss is `M`
percent of observations instead of 1 percent, so it becomes expensive exactly
in the 128-tap configurations where `M` is large.

**Retires defect D3.**

### 17. Frames per superslot

**Question.** Does a node always transmit exactly `M` frames, padding when it
has fewer subreports, or a variable number?

**Decision.** **Fixed `M` frames, padded.**

**Rationale.** Every superslot looks identical on air, so timing analysis and
power profiling are uniform, and there is never any ambiguity between a frame
that was lost and one that was never sent. The receiver state machine always
expects exactly `M` frames, and every slot in the schedule always carries a
frame.

**Costs accepted.** Transmitted padding consumes power and airtime for no
informational benefit, and the worst case is startup, when a node with no
observations yet transmits `M` frames that are almost entirely padding.

**Consequence — the header may simplify.** With `M` fixed and every frame at
`max_frame_bytes`, the byte offset of frame `m` within the pooled report is
derivable as `m * payload_capacity`. The `pooled_offset` field from item 9
becomes redundant and can be replaced at zero net cost by a
`pooled_total_bytes` u16, which together with the derivable offset fully
defines the byte stream and locates the padding boundary. Confirm under item
18, since it depends on whether all `M` frames are transmitted at maximum
size.

**Tension with item 15.** Item 15 noted a free robustness gain from making
`m = 0` the *smallest* frame of the pooled report, since packet error rate
scales with frame length and `m = 0` now carries the observation instant. That
is incompatible with all `M` frames being maximum size. Resolved under item
18.

### 18. Subreport packing

**Question.** How are subreports packed into the `M` frames?

**Decision.** **Byte-stream, balanced across `M` frames.** The pooled report is
split into `M` equal byte runs and a subreport may cross a frame boundary.

**Sizing algorithm** (executed by the config tool, producing constants):

```text
subreport_size   = 40 + 4 * cir_taps
P_max            = (N - 1) * subreport_size
capacity_max     = max_frame_bytes - 31 (header) - 2 (FCS)
M                = ceil(P_max / capacity_max)
frame_payload    = ceil(P_max / M)          <- config constant
frame_bytes      = frame_payload + 33
T_slot           >= airtime(frame_bytes) + rx_processing(cir_taps) + margin
```

`frame_payload` derives from `P_max`, not the actual report size, so it is a
config constant and `T_slot` is therefore also constant. Short reports are
padded (item 17).

**Rationale.** Balancing minimises the *maximum* frame size, which
simultaneously lowers PER for every frame and — because item 6 sizes slots to
max-frame airtime — shortens the slot. At N=6, 64 taps:

| Packing | Max frame | Airtime | `T_slot` | Cycle | Rate |
| --- | --- | --- | --- | --- | --- |
| Greedy (991 + 489) | 1023 B | 1.37 ms | 2.0 ms | 24 ms | 41.7 Hz |
| Balanced (740 + 740) | 772 B | 1.03 ms | 1.7 ms | 20.4 ms | 49.0 Hz |

**About 15 percent more report rate at zero cost.** The gain ranges from 0 to
~33 percent depending where `P_max` sits relative to `M * capacity_max`, and is
largest just after `M` increments. The tool should display it.

This is safe only because item 11 placed CRC32 at subreport granularity; a
whole-report CRC would make a split subreport unverifiable.

**Resolves the item 15 / item 17 tension.** Front-loading the smallest frame
into `m = 0` to protect the observation instant was rejected: it raises the
maximum frame size above balanced, so the slot lengthens and every other
frame's PER worsens in order to protect one frame. Balanced packing already
reduces `m = 0`'s PER substantially (772 B versus 1023 B) as a side effect.

**Subreport-aligned packing rejected.** It avoids all reassembly and keeps
subreports contiguous, and at 64 taps it is workable (3+2, 920 B max frame).
But at 128 taps a 552 B subreport allows only one per 991 B frame, forcing `M`
from 3 to 5 and nearly halving the rate — the finding from item 3.

**Consequence.** `pooled_offset` is removed from the header and replaced by
`pooled_total_bytes`, since frame offset is now `m * frame_payload`. Net zero
bytes.

### 19. Subreport ordering

**Question.** In what order are subreports placed within the pooled report?

**Decision.** **Ascending `j`, rotated by cycle index.** The list starts at
`j = (floor(k/N) + offset) mod N`, skipping `j = i`. No wire bytes.

**Rationale.** Under balanced byte-stream packing exactly one subreport per
cycle straddles a frame boundary and therefore requires both frames to survive,
carrying roughly double the loss probability. At N=6, 64 taps with
`frame_payload = 740`:

```text
subreport 0  [0,296)      frame 0 only
subreport 1  [296,592)    frame 0 only
subreport 2  [592,888)    STRADDLES 740, needs both frames
subreport 3  [888,1184)   frame 1 only
subreport 4  [1184,1480)  frame 1 only
```

With fixed ordering that penalty lands on the same link permanently: at 1
percent frame error rate, four links lose 1 percent of samples and one loses 2
percent. Rotation moves each link through the straddle position roughly 1 cycle
in `N-1`, converging all links to ~1.2 percent. Systematic per-link bias is
considerably worse for a sensing dataset than uniform random loss.

Ordering by freshness (`round_delta` ascending) was rejected because it is
merely a fixed permutation relative to `i` and does not address the bias.
Padding to avoid straddling was rejected because it costs up to one subreport
per frame — 296 B of 1480 B at 64 taps, 20 percent — largely undoing the 15
percent gain from balanced packing.

**Recording the rotation offset on the wire was rejected.** Subreports are
self-describing via `observed_node_id`, so the host never needs the rotation to
decode. Its only use is **loss attribution**: when a frame is lost,
`pooled_total_bytes` reveals how many subreports existed but not which peers
are missing, and that distinction separates a PHY-level link failure from a
relay-level frame loss. That is recoverable from `k` plus a documented formula,
consistent with how frame offsets, slot times, and `M` are already derived.

### 20. Gateway exemption

**Question.** Does the gateway transmit a full pooled report, or is it exempt?

**Decision.** **Uniform superslots. The gateway transmits a full pooled report
like every other node.**

**Rationale.** Every node behaves identically, so there is a single code path
for schedule, report assembly, and sync, and no special case in the timing
arithmetic — where a bug causes collisions rather than merely degraded data.
The redundancy is actively useful during bring-up: the host can compare the
gateway's USB-reported observations against the same links relayed back by
peers, validating the entire relay path against ground truth.

**What was given up.** A one-slot gateway superslot would have yielded 9 to 12
percent more report rate:

| Config | Uniform | Gateway `M_0 = 1` | Gain |
| --- | --- | --- | --- |
| N=6, M=2 | 12 slots | 11 slots | 9.1 percent |
| N=6, M=3 (128 taps) | 18 slots | 16 slots | 12.5 percent |
| N=8, M=2 | 16 slots | 15 slots | 6.7 percent |

Two factors reduce the real value of that gain. Observations per cycle are
unchanged at 25 relayed plus 5 direct, so a 9 percent faster cycle is also 9
percent more USB throughput — and item 1 made USB the binding constraint that
blocks config export. And the slot arithmetic change lands in the most
safety-critical path in the firmware.

**Correction to an earlier claim in this log.** A shorter gateway superslot was
previously described as breaking the `k mod N` indexing. That was overstated;
because only one node differs, the derivation stays closed-form:

```text
cycle_slots   = M_0 + (N-1) * M
slot_of(k, m) = floor(k/N) * cycle_slots
              + (k mod N == 0 ? 0 : M_0 + (k mod N - 1) * M)
              + m
```

The decision stands on the single-code-path and relay-validation arguments, not
on arithmetic complexity.

### 21. Liveness rule

**Question.** What governs whether a non-master node may transmit?

**Decision.** **`evidence_age` with a configurable threshold.**

```text
age = 0                                if B_0 was heard directly this cycle
    = 1 + min(ages received this cycle) otherwise
    = INVALID                          if no evidence at all

transmit iff age <= threshold
```

`evidence_age` is already in the frame header from item 9, so this costs zero
additional bytes.

**Purpose.** This rule is power and RF hygiene, not correctness. Nodes stay
mutually synchronised without B_0 because they hear each other, so the network
remains coherent and only the data sink is lost.

**Rationale.** Note 4 as written had two defects. Its staleness was unbounded:
evidence in `U_(j,0)` is already up to `N-1` superslots old when `B_j`
transmits it and up to `N-1` more before `B_i` acts, so the network kept
transmitting for roughly two full cycles after B_0 died. And at one-hop depth
it carried cascade risk, because a node that goes silent stops being an
evidence source for its own neighbours.

`evidence_age` generalises to any hop depth, makes staleness an explicit
tunable, and a threshold above 1 removes the cascade risk.

**Convergence.** Ages are per-transmission rather than persisted, and each hop
adds 1, so the network-wide minimum grows by at least 1 per cycle once B_0
stops. Bounded, with no count-to-infinity. Shutdown takes roughly `threshold`
cycles — 60 ms at a 20 ms cycle with threshold 3.

**Retires defect D9.**

**Available refinement, not adopted.** Resume hysteresis, requiring several
consecutive cycles of valid evidence before a silenced node rejoins, would
prevent a marginal link causing start-stop oscillation. Costs a second tunable
and slower recovery. Revisit if oscillation is observed.

### 22. Master identity

**Question.** How is the master identified?

**Decision.** **A single `master_node_id` configuration parameter, defaulting
to 0.** It serves as both bootstrap transmitter and liveness anchor. The
gateway role remains a separate physical fact about which board has USB
attached.

**Rationale.** Three roles were conflated on B_0: bootstrap transmitter (the
node that transmits without having heard anyone, establishing `k`), liveness
anchor (item 21), and gateway. Hardcoding B_0 made bench work with two peer
boards impossible without a gateway, and coupled timing authority to the USB
attachment in exactly the way `docs/architecture.md:18-19` warns against.
Setting `master_node_id` to a peer yields a fully working network with no data
sink, which is what bench testing needs.

`master_node_id` is covered by `config_hash`, so a two-master misconfiguration
is caught by item 10's machinery rather than needing new mechanism.

**Retires defect D10.**

**Rejected.** Separate `master_node_id` and `sink_node_id` would most
faithfully honour the architecture note but costs a second field and failure
mode for a distinction with no current use case. Election by lowest present ID
directly contradicts item 21, whose entire purpose is that the network stops
when the anchor dies.

### 23. Resync policy and conflicting `k`

**Question.** How strictly is the master's `k` authoritative, and what does a
node do when a peer's `k` disagrees?

**Decision.**

- **Master authoritative when heard.** If a master frame was received this
  cycle, its `k` wins. Otherwise adopt from any peer frame.
- **Corrections applied only at cycle boundaries**, never mid-superslot.
- **Listen-before-bootstrap.** On boot the master listens briefly and adopts a
  running network's `k` rather than forcing every node backwards to 0.

**Accuracy is not the issue.** A receiver localises slot phase in its own clock
domain from `rx_timestamp` plus `(k, m)` from the payload, accurate to within
time-of-flight — under 100 ns at 30 m, against slots of 1.7 to 3 ms. That is a
20000:1 margin, so multi-hop reference error is irrelevant. CFO at 1.8 ppm
gives 43 ns of drift per 20 ms cycle, so **no rate correction is needed for
TDMA**. Rate correction is still required host-side for ranging.

**Why cycle-boundary corrections.** A phase jump between computing a programmed
TX time and the hardware executing it could miss the deadline, and the entire
design leans on the measured 0 DTU programmed-versus-actual error.

**Why listen-before-bootstrap.** Item 21 makes master reboot mostly
self-consistent: if the master dies, peers stop within `threshold` cycles, so a
rebooting master normally hears silence and legitimately starts at `k = 0`. But
if it reboots faster than `threshold` cycles the peers are still running at
high `k`, and forcing them backwards would insert a discontinuity into the
host's time index.

**Rejected.** "Highest `k` wins" is elegant and partition-safe but a single
frame with a corrupted high `k` that passed FCS and header validation would
poison every node permanently. "Master only, never from peers" would prevent a
node with a marginal master link from syncing even while hearing four peers
perfectly, discarding the relay topology the protocol is built on.

### 24. Identity validation

**Question.** How are duplicate or invalid node identities detected?

**Decision.** **Bind `node_id` to the board's hardware UID in the roster and
verify at boot**, layered with runtime and host-side checks. Zero wire cost.

1. The roster maps each board's `FICR->DEVICEID` to a `node_id`. Firmware
   reads `hwinfo_get_device_id()` at boot and **refuses to run** if it does not
   match its configured `node_id`. Duplicate assignment becomes impossible to
   flash rather than merely detectable afterwards.
2. Boot check that `node_id < N`.
3. Runtime check for receiving a frame whose `src_addr` equals own `node_id`.
4. Host check for inconsistent `tx_timestamp` on the same `(node_id, k, m)`.

**Reframing.** "Two masters" cannot arise independently: `master_node_id` is a
shared config value covered by `config_hash`, so a mismatch is already caught
by item 10. Two masters requires two boards believing they are the same node,
so **duplicate `node_id` is the real failure**. `config_hash` cannot catch it,
because the hash covers the shared roster while each board's own `node_id`
comes from its build.

It is also the most damaging failure available — two boards transmitting in the
same superslot indefinitely — and insidious, because mutual jamming may leave
neither frame decodable, so check 3 alone can silently fail to fire on exactly
the case it exists for. That is why prevention at flash time is the primary
mechanism and detection is only backup.

**Rejected.** Carrying a per-board EUI-64 in every frame would make duplicates
unambiguously detectable regardless of collisions, and `dwt_seteui` exists, but
it costs 8 B per frame in the scarcest budget and does nothing to prevent the
misflash. Relying on flashing discipline alone was rejected because the project
already flashes boards with different roles at different times, which is
precisely how duplicate assignment occurs.

### 25. Unobserved peer signalling

**Question.** What does a node convey about a peer it did not observe this
cycle?

**Decision.** **A `peer_observed_bitmap` u8 in the frame header.** Bit `j` set
means a subreport for peer `j` is included in this pooled report. `N <= 8` from
item 2 makes one byte cover every peer exactly.

**Rationale.** Gives immediate, unambiguous loss attribution. A cleared bit
means `B_i` did not observe `B_j`; a set bit whose subreport does not arrive
means relay loss. Those are different diagnoses — PHY-level link failure versus
frame collision — and separating them is otherwise only possible by
cross-referencing other nodes' reports. It also lets a receiver validate its
own parse against a declared expectation.

**Supersedes the item 19 discussion of loss attribution.** Deriving which peers
are missing from `pooled_total_bytes` plus the rotation formula would place a
derivation in the host that must stay in lockstep with firmware. One explicit
byte removes that coupling entirely.

Cost is 1 B per frame, 2 B per superslot at M=2, or 0.13 percent.

**Rejected.** Per-peer consecutive-miss counters (N-1 bytes) would let any
receiver assess link health without history, but the host already has full
history and no node currently acts on peer link quality. Explicit
absent-subreport stubs with reason codes would give richer bring-up diagnostics
at ~4 B per missing peer, but the same information is available from the bitmap
plus separate counters exported over USB under item 26.

### 26. USB export model and backpressure

**Question.** What does the gateway export, and what happens under
backpressure?

**Decision, export model.** **Whole received frames, forwarded verbatim with a
thin wrapper** carrying local RX metadata.

**Rationale.** Item 11 placed CRC32 at subreport granularity, computed by the
observing node, so any re-encoding at the gateway would leave the host unable
to verify it and destroy the end-to-end integrity property. Beyond that, the
gateway is the **most CPU-constrained node** — it does everything a peer does
plus USB export, against the tight RX callback budget from item 6 — so
verbatim forwarding reduces its work to a memcpy. It also gives maximum
debuggability: the host sees exactly what was on air, including headers,
`peer_observed_bitmap`, and `evidence_age`.

Cost is about 4 percent more USB than extracting subreports: 31 B of header per
frame, 62 B per superslot against ~1480 B of payload. Padding adds more, but
only when reports are short, which is when there is little data to carry.

**Decision, backpressure.** **Bounded queue, drop-newest, counted at the
producer, gap records in the cycle summary.**

- The radio **never** blocks on USB, per `AGENTS.md`.
- When the queue is full the producer fails to enqueue and increments a precise
  counter at the point of loss.
- Archived captures get a clean contiguous run with a quantified tail gap.
- Accepted cost: the real-time dashboard lags during a transient, because the
  freshest data is what gets discarded.

Since item 1 made the USB budget a hard config constraint, in-spec operation
should not overflow at all; this policy governs transients and out-of-spec
configurations.

**Required counters, resolving defect D13.** The silent `K_NO_WAIT` drops at
`usb_cir_stream.c:33, 66` are replaced by counted drops, reported in the
`CYCLE_SUMMARY` record of `contracts/usb-cdc-v1.md`:

- RX PHY and FCS errors
- Hardware frame-filter rejects
- Frames passing FF but failing Heimdall validation
- Per-peer `m = 0` misses
- USB queue drops
- Subreport CRC32 failures (host side)

### 26a. Amendment — record formats specified

The original decision said the gateway forwards frames "verbatim with a thin
wrapper" and never defined the wrapper. That was not merely an unfinished
section: `budget.usb_wrapper_bytes` in the configuration was a placeholder
feeding the throughput calculation, and that calculation blocks configuration
export. **An invented number was controlling a hard build-time limit.**

`contracts/usb-cdc-v1.md` now specifies the framing and all seven record types.
The overheads are normative constants in the model, replacing the placeholder:

| Constant | Bytes | Contents |
| --- | --- | --- |
| `USB_OUTER_BYTES` | 16 | sync, version, type, flags, reserved, length, sequence, CRC32 |
| `USB_RADIO_FRAME_WRAPPER_BYTES` | 8 | `u40` rx_timestamp, `u8` rx_flags, `u16` frame_len |
| `USB_LOCAL_OBS_WRAPPER_BYTES` | 5 | `u8` reporting_node_id, `u32` k |
| `USB_TX_RECORD_PAYLOAD_BYTES` | 13 | k, m, tx_timestamp, frame_len, flags |
| `USB_CYCLE_SUMMARY_PAYLOAD_BYTES` | 34 | full counter set |

Per cycle the gateway emits `(N-1) * M` `RADIO_FRAME`, `(N-1)` `LOCAL_OBS`, `M`
`TX_RECORD`, and one `CYCLE_SUMMARY`.

**Two records the original decision missed.**

- `TX_RECORD`. The gateway cannot receive its own transmissions, so without it
  the host learns the gateway's programmed transmit timestamps only indirectly,
  from `observed_tx_timestamp` inside peers' subreports, and only when some peer
  heard it. Ranging on links where the gateway is the transmitter depends on it.
- `HELLO` carrying `config_hash`, `N`, `M`, and tap count. Without it the host
  cannot size a subreport and therefore cannot decode anything, and would have to
  be told the configuration out of band.

**`rx_callback_max_us` in `CYCLE_SUMMARY`** was listed under item 30 as an
available-but-not-adopted refinement. It is adopted here, because item 6a made
the processing model load-bearing in two places rather than one. It closes
bring-up gate 1 continuously in the field rather than by a single bench
measurement.

**Rejected.** Whole-cycle atomic dropping would guarantee every delivered cycle
is a coherent link-matrix snapshot, but costs a ~9 KB buffer at N=6 and
discards per-link samples that would otherwise survive, since per-link time
series tolerate masked gaps. A reduced fallback record omitting CIR taps under
pressure would degrade gracefully and preserve ranging, but adds a second record
type, a pressure threshold to tune, and a variable data rate.

### 27. Config format and build binding

**Question.** How does the configuration bind to the build, and what does
`config_hash` cover?

**Decision.** **The configuration tool is authoritative for the values that get
flashed. The build re-derives them independently and fails on any
disagreement.**

*Amended after the initial decision.* The tool remains authoritative — the
exported configuration is exactly what runs, and the build never substitutes its
own numbers. But it now also proves the tool's arithmetic, so formula drift
between the browser tool and the reference model is a build error rather than a
silent wrong value in flashed firmware.

**Implementation.**

| Component | Path |
| --- | --- |
| Reference model, verifier, header generator | `tools/config/heimdall_config.py` |
| CMake integration | `firmware/radio/app/cmake/heimdall_config.cmake` |
| Kconfig gate | `CONFIG_HEIMDALL_BEACON` |
| Example configuration | `deployment/beacon-config.example.json` |

`heimdall_config.py verify` re-derives `subreport_bytes`,
`pooled_report_max_bytes`, `M`, `frame_payload_bytes`, `frame_bytes`,
`frame_airtime_us`, `rx_processing_us`, `slot_floor_us`, `superslot_us`,
`cycle_us`, `per_link_rate_hz`, `gateway_usb_bytes_per_s`, and `config_hash`,
compares each against the declared value, then checks every invariant. On
success it emits `heimdall_beacon_config.h` into the build tree. A non-zero exit
becomes a CMake `FATAL_ERROR` at configure time.

`CMAKE_CONFIGURE_DEPENDS` is set on both the configuration file and the model,
so a stale generated header cannot survive an edit to either.

**Format.** JSON exported by the tool. JSON is valid YAML, so a YAML parser
reads both, keeping consistency with the existing
`deployment/node-roster.example.yaml`.

**`config_hash` scope.** Computed over the **packed binary parameter struct the
firmware uses**, not over the text file, so whitespace, key ordering, and number
formatting are all irrelevant. Covers: `N`, `M`, `T_slot`, `cir_taps`, CIR
window, `max_frame_bytes`, `frame_payload`, preamble code, channel, PRF, PLEN,
SFD type, data rate, `network_id`, `master_node_id`, `evidence_age` threshold.
Excludes `node_id`, which is per-board and validated by item 24.

**Why the amendment.** The failure modes of an unchecked tool are silent rather
than loud, and all three are severe:

| Wrong value | Consequence |
| --- | --- |
| `frame_payload` | Payload truncation |
| `M` | Superslot overruns into another node's slot |
| `T_slot` | RX accumulator corruption, per defect D11 |

**Invariants enforced.** The verifier checks all of the following, and the
firmware repeats them at boot so a hand-edited configuration cannot reach a
board either:

```text
2 <= N <= 8
master_node_id      <  N
node_id             <  N
1 <= cir_taps       <= 128
0 <= cir_left_taps  <  cir_taps
max_frame_bytes     <= 1023
max_frame_bytes > 127          implies phr_mode == ext
frame_bytes         <= max_frame_bytes
M * frame_payload   >= P_max
slot_duration_us    %  100 == 0
slot_duration_us    >= slot_floor_us
(N-1) * superslot_us in 40-bit timestamp range
gateway_usb_load    <= usb_budget
pdoa_mode           == 0        on single-antenna hardware
```

**Accepted cost.** The sizing formulas are now deliberately maintained twice, in
browser JavaScript and in `heimdall_config.py`. A single shared implementation
remains foreclosed because the tool is standalone browser JavaScript and cannot
call a Python or C module. The cross-check is what makes the duplication safe.

**Rate control note.** `slot_duration_us` is a free parameter bounded below by
`slot_floor_us`, not pinned to it. Raising it above the floor is therefore the
mechanism for reducing report rate and USB load, which resolves the sub-question
left open under item 1: no separate idle or `cycle_period` parameter is needed,
because a longer slot achieves the same effect while keeping every slot occupied
and the schedule arithmetic uniform.

### 28. PHY parameter exposure

**Question.** Which PHY parameters become configuration-exposed?

**Decision.** **Expose everything except STS. STS is permanently excluded and
will not be used.**

**Exposed and covered by `config_hash`:** channel, TX preamble code, RX preamble
code, PLEN, PAC, SFD type, SFD timeout, data rate, PHR mode, PHR rate, PDOA
mode, TX power (`PGdly` and the power word).

**Per-board, in the roster, not hashed:** `node_id`, board UID, **TX antenna
delay, RX antenna delay**.

**Permanently fixed off:** `stsMode = DWT_STS_MODE_OFF`. SP0 packet
configuration is permanent. No STS key material, no STS length parameter.

**Antenna delay calibration — a real gap being closed.** `TX_ANT_DLY =
RX_ANT_DLY = 16385` is currently a hardcoded shared `#define`
(`primitives.c:12`), recorded as the uncalibrated default at
`BRINGUP-NOTES.md:87`. It is a **per-board** constant that offsets every
timestamp, so leaving it shared guarantees a systematic range bias. It moves
into the roster alongside the UID mapping from item 24. It is deliberately not
in `config_hash`, because boards legitimately differ.

**Consequences of exposing data rate.** 850 kb/s multiplies data airtime by 8,
making a 1023 B frame roughly 9.9 ms. It is not infeasible in principle — a 128
B frame at 850 kb/s is about 1.2 ms — but it collapses `frame_payload` and
inflates `M` drastically. The tool must model it and warn rather than assume
6.8 Mb/s.

**Consequences of exposing PDOA.** Requires dual-antenna hardware. On the
current single-antenna boards it must remain `DWT_PDOA_M0`; the tool should flag
any other value as hardware-dependent.

**Consequences of exposing SFD timeout.** It must track preamble length. An
inconsistent pair is a non-functional combination the tool should reject rather
than merely warn about.

**Consequences of excluding STS.** The protocol has **no** cryptographic defence
against a transmitter that deliberately copies the PAN ID and preamble code.
Accepted: the threat model is accidental interference, not a hostile actor. The
`flags` bit previously reserved for STS in item 29 is withdrawn.

**PLEN is the one genuine rate-versus-range lever.** 128 to 64 saves 65 us of
SHR per frame, which is 780 us per cycle at N=6, M=2, or **3.8 percent**, at the
cost of acquisition sensitivity. Default 128;
`BRINGUP-NOTES.md:142` already documents 64 as a lower-airtime fallback.

### 30. RX accumulator race (defect D11)

**Question.** How is the accumulator overwrite race fixed?

**Decision.** **Reorder the callback. No double buffering. Add a post-read
validity check.**

1. Read RX data, then diagnostics, then CIR.
2. Only then call `dwt_rxenable()`.
3. After the CIR read, check whether a new reception began during it and clear
   `obs_flags.cir_valid` if so.

**Decisive finding: the CIR accumulator is not double-buffered.** Comparing the
driver's read paths:

| Path | Buffer selection |
| --- | --- |
| `ull_readrxdata` | switches on `dblbuffon`, selects `RX_BUFFER_0_ID`/`RX_BUFFER_1_ID` (`dw3000_device.c:2376-2378`) |
| `ull_readdiagnostics` | switches on `dblbuffon` (`dw3000_device.c:2630-2637`) |
| `ull_readcir` | reads `ACC_MEM_ID` **unconditionally, no `dblbuffon` switch** (`dw3000_device.c:2436, 2441, 2520`) |

The frame buffer and per-frame diagnostics are double-buffered; the accumulator
is single. **Double buffering therefore cannot protect the CIR under any
configuration.** Enabling it while keeping the current order would preserve
frame `n+1`'s data during the CIR read but still corrupt frame `n`'s CIR,
trading a clean miss for silent corruption — the worse failure.

**The failure mode inverts favourably.** With RX off during the CIR read, a
frame arriving in that window is **cleanly missed** rather than silently
corrupting a CIR that is then reported as valid. Under the schedule frames only
begin at slot boundaries, so if processing completes within
`T_slot - airtime` nothing is missed at all — exactly the item 6 budget, so the
two decisions are consistent. Missed frames are already counted under item 26.

**Retires defect D11.**

**Subsequently adopted.** Permanently instrumenting RX callback duration and
exporting it in the cycle summary was initially left as an available refinement.
Item 26a adopts it as `rx_callback_max_us`, because item 6a made the processing
model load-bearing in two constraints rather than one.

---

## Foreign transmitters and coexistence

Analysis of behaviour when a UWB transmitter that is not part of the network
operates on the same channel.

**Current exposure.** Hardware frame filtering is disabled —
`dwt_configureframefilter()` is never called in `firmware/radio/`, leaving the
reset default. Every frame with a valid FCS reaches the callback. The only
filter is the software tag check at `primitives.c:206-210` (`[0] == 0xC5`,
`[4] == 'P'`, `[5] == '1'`).

**Outcomes.**

| Case | Effect |
| --- | --- |
| Valid FCS, wrong tag | Callback runs, frame read, tag check fails, RX re-arms. One wasted callback, serialised in the workqueue. |
| Bad FCS or PHY error | Error callback, re-arm, counted. Cheap. |
| Preamble collision | Receiver locks to whichever preamble it acquires first. If foreign, the peer's frame in that slot is lost entirely. One missing subreport per collision. |
| Accidental tag match | Unlikely (`0xC5` decodes as a reserved 802.15.4 frame type) but consequences are severe: garbage parsed as a beacon. |

**The CIR accumulator cannot be protected by filtering.** The accumulator is
written during preamble acquisition, before any address or payload field
exists to filter on. Combined with defect D11 — RX re-armed at
`primitives.c:286` *before* the CIR read — a foreign frame arriving in that
window corrupts a CIR that is then reported as a valid observation. Under this
protocol there is a frame in every slot, so the window is always open. **This
raises D11 from a latent issue to a priority fix.**

**Defences, ranked.**

1. **Preamble code.** Currently hardcoded `txCode = rxCode = 9`
   (`main.c:70-71`). Channel 9 at PRF64 supports codes 9-12. A foreign network
   on a different code is largely not acquired at all. This is the only
   defence acting *before* the accumulator is touched. Should become a config
   parameter.
2. **SFD type.** `sfdType = 1` (`main.c:72`) is the DW non-standard 8-symbol
   SFD, so a standards-compliant foreign device never matches the SFD
   detector. Accidental protection already in place.
3. **Hardware frame filtering with PAN ID.** Removes callback cost for foreign
   traffic, but requires real 802.15.4 MAC headers and does not protect the
   accumulator.
4. **STS (SP1/SP3).** Would have been the only cryptographic defence.
   **Permanently excluded by item 28** — STS will not be used. The protocol
   therefore has *no* defence against a transmitter that deliberately copies
   the PAN ID and preamble code. Accepted limitation: the threat model is
   accidental interference, not a hostile actor.
5. **Software `network_id`.** Cheap, and required for the item 10 scoping fix,
   but acts only after the callback cost has been paid.

### 29. Network isolation and frame filtering

**Question.** How is the network isolated from foreign UWB transmitters on the
same channel? Pulled forward from the plumbing group because the coexistence
analysis put it on the critical path.

**Decision.** **Adopt an 802.15.4 MAC header, enable hardware frame filtering,
and make the preamble code a configuration parameter.**

**MAC header, 9 B**, standard data frame with PAN ID compression and short
addresses:

```text
Byte 0-1  Frame Control      0x8841
Byte 2    Sequence Number    free per-frame counter
Byte 3-4  Destination PAN    = network_id, this is what FF matches
Byte 5-6  Destination Addr   = 0xFFFF broadcast
Byte 7-8  Source Address     = node_id (0..N-1)
```

`0x8841` decodes as frame type 001 (data), security 0, frame pending 0,
**ack request 0**, PAN ID compression 1, destination addressing mode 10
(short), frame version 0, source addressing mode 10 (short).

**Driver configuration**, none of which the firmware currently performs:

```c
dwt_setpanid(network_id);
dwt_setaddress16(node_id);
dwt_configureframefilter(DWT_FF_ENABLE_802_15_4, DWT_FF_DATA_EN);
```

`DWT_FF_ENABLE_802_15_4 = 0x2`, `DWT_FF_DATA_EN = 0x002`
(`deca_device_api.h:568-583`). Under 802.15.4 rules a destination of `0xFFFF`
is accepted as broadcast when the PAN ID matches, so all nodes hear each other
while foreign PANs are rejected in hardware before the callback runs.

**Net cost.** The custom header loses `sender_node_id` to the source address
and never needs the `network_id` field the item 10 amendment would otherwise
have required. Total header goes from 24 B to 30 B: **+6 B per frame, 0.6
percent** of a 1023 B frame, in exchange for hardware rejection of foreign
traffic. The MAC sequence byte is a free per-frame loss-detection counter.

**Preamble code.** Hardcoded to 9 at `main.c:70-71`. Channel 9 at PRF64 gives
codes 9-12 as the practical set; codes above 24 switch the driver into SCP
mode (`dw3000_device.c:1841`), which is not wanted. The receiver correlates
against its configured code, so a foreign network on a different code is
largely never acquired. This is the **only** defence acting before the CIR
accumulator is written. Becomes a config parameter covered by `config_hash`.
**Retires defect D12.**

**Not solved by this decision.**

- Accumulator corruption from a colliding preamble on the *same* code. No
  filtering can prevent it; mitigation is fixing D11 and accepting the lost
  observation.
- A transmitter that copies the PAN ID and preamble code. That would require
  STS, which item 28 permanently excludes.

**Bring-up gates added.**

1. **Auto-ACK must stay off.** With FF enabled and `AR = 1` the DW3000
   auto-transmits an ACK inside another node's slot. `dwt_enableautoack()` is
   currently never called; keep it so and keep `AR = 0` in FCTRL.
2. **`DWT_FF_EXTEND_EN` (0x080) may be required** alongside
   `DWT_PHRMODE_EXT`. The flag nominally refers to 802.15.4 extended *frame
   types* rather than extended PHR length, but the interaction is unverified.
3. **FF + broadcast + EXT PHR is an untested combination** on this hardware.
4. **Misconfigured FF fails silently** — zero receptions, no error. An
   `enable_frame_filter` config flag must exist so it can be turned off during
   bring-up.

---

## Open defects carried forward

| Ref | Defect | Proposed resolution | Status |
| --- | --- | --- | --- |
| D1 | `k mod N == i` with 1-based `i` starves `B_N` | 0-based node IDs | Resolved via #2 |
| D2 | u16 `k` wrap skips boards in the schedule | u32 `k` | Pending #8 |
| D3 | Note 2 discards a valid observation when any frame of a superslot is missed | One received frame suffices for `U_(i,j)` | Pending #16 |
| D4 | One `Tx_timestamp` per pooled report, but the report spans M frames | Per-frame self-describing header | Pending #9 |
| D5 | Whole-message CRC discards correctly received frames of a partial report | Per-subreport CRC16 | Pending #11 |
| D6 | Unspecified which of `B_j`'s M frames yields `U_(i,j)` | Carry `m`; one CIR read per superslot | Pending #15 |
| D7 | Nothing binds a board to the active configuration; a stale board jams the network permanently | `N`, `M`, `config_hash` in every header; refuse to TX on mismatch | Pending #10 |
| D8 | Subreport omits RSSI, first-path power, CIR window offset, validity flags; first-path index rounded away | Add fields; carry raw Q10.6 index | Pending #12 |
| D9 | Liveness rule is exactly one hop deep with unbounded staleness | `u8 evidence_age` with threshold | Pending #21 |
| D10 | No bench operation possible without `B_0` | Configurable `master_node_id` | Pending #22 |
| D11 | RX re-armed before CIR read; accumulator overwritten under continuous traffic | Reorder; double buffering cannot help, accumulator is single | Resolved via #30 |
| D12 | Preamble code and SFD type hardcoded, so network isolation is not configurable | Expose as config parameters covered by `config_hash` | Resolved via #29 |
| D13 | No counters distinguish foreign frames, tag-check rejects, and genuine peer loss | Separate counters exported to host | Resolved via #26 |

All identified defects are resolved in design. None is yet resolved in code.

---

## Bring-up gates

These must be completed before or alongside implementation. Several decisions
above are calibrated against numbers that do not yet exist.

**Blocking measurements**

1. **RX callback duration.** GPIO toggle or cycle counter around the callback at
   32 MHz SPI, at both 64 and 128 taps, with a full-size frame. Gates item 6's
   `T_slot` floor and item 27's boot invariants. All processing figures in this
   log are derived from SPI byte counts, not observed.
2. **USB throughput.** The current byte-at-a-time `uart_poll_out()` path
   (`usb_cir_stream.c:81-83`) is not a valid basis for the item 1 budget. Rewrite
   with buffered or async CDC writes, then measure. Both worked examples above
   currently exceed plausible throughput.
3. **Frame error rate versus frame length.** Informs whether item 15's deferred
   short-`m=0` lever is justified, and validates the item 18 balanced-packing
   robustness argument.

**Radio configuration verification**

4. `DWT_PHRMODE_EXT` verified board-to-board (item 5). Never exercised on this
   target.
5. Frame filtering plus broadcast plus EXT PHR as a combination (item 29).
6. Whether `DWT_FF_EXTEND_EN` (0x080) is required alongside `DWT_PHRMODE_EXT`
   (item 29).
7. Auto-ACK confirmed disabled and `AR = 0` in FCTRL (item 29). With FF enabled
   this would transmit inside another node's slot.
8. `enable_frame_filter` escape flag implemented, since misconfigured FF fails
   silently with zero receptions (item 29).

**Calibration**

9. Per-board TX and RX antenna delays measured and recorded in the roster (item
   28). Currently the uncalibrated shared default 16385, which biases every
   range.
10. Per-board `FICR->DEVICEID` recorded in the roster for the item 24 boot check.

**Values in the model that are estimates, not measurements**

11. `budget.rx_fixed_overhead_us`, `budget.spi_transaction_overhead_us`,
    `budget.diagnostics_bytes`, `budget.crc32_bytes_per_us`,
    `budget.report_assembly_us`, and `budget.processing_margin_factor` in
    `deployment/beacon-config.example.json` are engineering estimates. Item 6a
    made them load-bearing in **two** constraints rather than one, so gate 1 is
    correspondingly more important. `rx_callback_max_us` in `CYCLE_SUMMARY`
    keeps them observable after bring-up.
12. `budget.usb_budget_bytes_per_s` is provisional pending gate 2.
13. `PHR_SYMBOLS = 21` in the airtime model is the standard approximation and has
    not been confirmed for `DWT_PHRMODE_EXT`, whose PHR carries more length bits.
    It contributes ~21 us of a ~550 us frame, so an error here is small but
    systematic.

---

## Refinements available, not adopted

| Ref | Refinement | Cost |
| --- | --- | --- |
| #11 | CRC32 over a virtual header (`reporting_node_id`, `k`) to detect relay mis-attribution | Zero wire cost |
| #15 | Short `m = 0` frame to protect the observation instant | One increment of `M` |
| #21 | Resume hysteresis to prevent liveness start-stop oscillation | One tunable |
| #26 | Reduced fallback record without CIR taps under USB pressure | Second record type |
| #6a | Defer the `round_delta = 1` observation by one cycle instead of lengthening `M = 1` slots | One cycle of latency on one link |
