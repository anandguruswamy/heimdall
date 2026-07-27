# UWB Beacon Contract v1

Status: **normative and implemented.** Supersedes `beacon-v0.md`.

`protocol_version = 1`.

This contract defines the on-air format and behaviour of the Heimdall superslot
beacon protocol. Rationale for every decision is recorded in
`docs/protocol-decisions.md`; an accessible explanation of the scheme is in
`docs/beacon-protocol-explained.md`. Where this contract and the decision log
disagree, this contract governs.

Requirement keywords MUST, MUST NOT, SHOULD, and MAY are used in the RFC 2119
sense.

Several parameters in this contract depend on measurements that have not been
taken. Implementations MUST NOT be flashed to hardware before the bring-up gates
listed in `docs/protocol-decisions.md` are cleared.

---

## 1. Scope and terminology

| Term | Definition |
| --- | --- |
| Node | One DWM3001 board participating in the network, identified by `node_id`. |
| `N` | Number of nodes actually deployed. The cycle is exactly `N` superslots. |
| `N_MAX` | Encoding ceiling, fixed at 8. Never appears in the schedule. |
| Slot | The smallest scheduling unit. Exactly one frame is transmitted per slot. |
| `M` | Slots per superslot. Derived, identical for every node. |
| Superslot | `M` consecutive slots, indexed `m = 0 .. M-1`. One node transmits in each. |
| `k` | Free-running superslot counter, u32. |
| Cycle | `N` consecutive superslots, so `N * M` slots. |
| Pooled report | The set of subreports one node transmits in its superslot. |
| Subreport `U(i,j)` | Node `i`'s physical-layer observation of node `j`'s transmission. |
| Master | The node that bootstraps the schedule and anchors liveness. |
| Gateway | The node physically attached to the UNO Q over USB CDC. |

Node IDs MUST be contiguous integers `0 .. N-1`. `node_id` is a **schedule
position**, not a board identity; the mapping from board hardware UID to
`node_id` lives in the deployment roster (section 11).

Master and gateway are independent roles. `master_node_id` is a configuration
parameter; the gateway is a physical fact about which board has USB attached.

---

## 2. Schedule

Node `i` transmits if and only if:

```
k mod N == i
```

Because `N` is the deployed node count, the cycle contains no empty or reserved
superslots for absent boards. Adding or removing a board changes `N` and
therefore changes the schedule for every node.

A superslot always occupies `M` slots. The absolute slot index of slot `m` in
superslot `k`, counted from the start of time, is:

```
slot_index(k, m) = k * M + m
```

Slot duration is uniform across all slots and equal to `slot_duration_us`.

`k` is u32 and free-running. At wrap, all nodes compute `k mod N` from the same
value and therefore remain in agreement; for `N` in {2, 4, 8} the wrap is exactly
periodic. `k` counts **superslots**, not cycles, so a u32 counter spans
approximately 545 years at a 4 ms superslot.

### 2.1 Observation freshness

Node `i`'s subreport for node `j` describes `j`'s transmission in superslot:

```
k_observed = k - ((i - j) mod N)
```

For `j != i` this is between 1 and `N-1` superslots before `i`'s own
transmission. Observation latency therefore varies per link and is deterministic.

---

## 3. Physical layer

The following are configuration parameters covered by `config_hash`. Values in
brackets are the verified defaults.

| Parameter | Default | Notes |
| --- | --- | --- |
| `channel` | 9 | |
| `tx_preamble_code`, `rx_preamble_code` | 9 | Codes 9-12 usable at PRF 64. Codes above 24 select SCP mode and MUST NOT be used. |
| `prf_mhz` | 64 | |
| `preamble_length` | 128 | 64 is a documented lower-airtime fallback. |
| `pac` | 8 | |
| `sfd_type` | 1 | DW non-standard 8-symbol. |
| `sfd_timeout` | 129 | MUST be consistent with `preamble_length`. |
| `data_rate_kbps` | 6800 | 850 is permitted but collapses `frame_payload`. |
| `phr_mode` | `ext` | `DWT_PHRMODE_EXT`. Required for frames above 127 B. |
| `phr_rate` | `std` | |
| `pdoa_mode` | 0 | Non-zero requires dual-antenna hardware. |
| `tx_pg_delay` | 0x34 | |
| `tx_power` | 0xfefefefe | MUST be identical across nodes; non-uniform power makes links asymmetric and corrupts RSSI comparisons. |

STS MUST be disabled. `stsMode` is permanently `DWT_STS_MODE_OFF` and the packet
configuration is permanently SP0. There is no STS key material and no STS length
parameter in this protocol.

Consequently the protocol provides **no cryptographic authentication**. A
transmitter that deliberately replicates the PAN ID and preamble code cannot be
distinguished from a legitimate node. The threat model is accidental
interference, not a hostile actor.

TX and RX antenna delays are **per-board calibration values** held in the
deployment roster. They MUST NOT be shared constants and are deliberately
excluded from `config_hash`, because boards legitimately differ.

---

## 4. Frame format

All multi-byte fields are little-endian. A 40-bit field (`u40`) is five bytes,
least significant first.

Every frame is fully self-describing: a single received frame is sufficient to
synchronise a cold node, validate configuration compatibility, and locate its
payload within the pooled report.

### 4.1 Header, 31 bytes

```
offset  size  field                  value / notes
------  ----  ---------------------  ------------------------------------------
   0    u16   fctrl                  0x8841
   2    u8    mac_seq                free-running per-frame counter
   3    u16   dest_pan               = network_id
   5    u16   dest_addr              0xFFFF (broadcast)
   7    u16   src_addr               = node_id
------------- 802.15.4 MAC header ends, Heimdall header begins ---------------
   9    u8    protocol_version       1. Offset MUST NOT change across versions.
  10    u8    frame_type             see 4.2
  11    u8    m                      slot index within the superslot
  12    u32   k                      superslot counter
  16    u8    N
  17    u8    M
  18    u16   config_hash            see section 10
  20    u40   tx_timestamp           programmed TX time, see 4.3
  25    u8    subreport_count        subreports *beginning* in this frame
  26    u16   pooled_total_bytes     valid pooled-report bytes in this report
  28    u8    peer_observed_bitmap   bit j set = subreport for j present
  29    u8    evidence_age           see section 8
  30    u8    flags                  see 4.4
------------- payload -------------------------------------------------------
  31    ...   pooled report bytes [m*frame_payload, (m+1)*frame_payload)
------------- FCS -----------------------------------------------------------
        u16   FCS                    appended by hardware
```

`fctrl = 0x8841` decodes as frame type 001 (data), security disabled, frame
pending 0, **ack request 0**, PAN ID compression 1, destination addressing mode
10 (short), frame version 0, source addressing mode 10 (short).

The ack request bit MUST be 0 and automatic acknowledgement MUST remain disabled.
With frame filtering enabled, an auto-ACK would transmit inside another node's
slot.

`N` and `M` are redundant with `config_hash` and are carried solely so a node
with a mismatched configuration can log a useful diagnostic rather than an opaque
hash comparison.

### 4.2 `frame_type`

| Value | Meaning |
| --- | --- |
| 0 | Pooled report frame. The only type defined in v1. |
| 1-255 | Reserved. A receiver MUST ignore frames with an unrecognised type. |

### 4.3 `tx_timestamp`

`tx_timestamp` is the **programmed** delayed-transmit time, computed before
`dwt_writetxdata` and including the transmitting board's TX antenna delay. It is
not a measured value; a frame cannot contain its own actual transmit time.

This is sound only because programmed-versus-actual error measures 0 DTU on this
hardware across 1000 frames at both 8 and 32 MHz SPI. Implementations MUST
re-verify this before relying on it.

### 4.4 `flags`

| Bit | Meaning |
| --- | --- |
| 0 | Report is truncated because assembly was interrupted. |
| 1 | Sender has not achieved synchronisation confidence (bootstrap). |
| 2-7 | Reserved, MUST be transmitted as 0 and ignored on receipt. |

### 4.5 `peer_observed_bitmap`

Bit `j` set means a subreport for node `j` is present in this pooled report. Bit
`i` (the sender's own index) MUST be 0.

This distinguishes two failure modes that are otherwise indistinguishable: a
cleared bit means the sender did not observe that peer, whereas a set bit whose
subreport does not arrive means the frame carrying it was lost.

---

## 5. Subreport format

Each subreport is `40 + 4 * cir_taps` bytes.

```
size  field                  notes
----  ---------------------  ---------------------------------------------------
u8    observed_node_id       j
u8    obs_flags              see 5.1
u8    observed_m             MUST be 0 in v1, see section 6
u8    round_delta            = (i - j) mod N
u40   observed_tx_timestamp  copied verbatim from the observed frame's header
u40   rx_timestamp           local RX timestamp, DWT_IP_M
i16   cfo_raw                dwt_readclockoffset, ppm = raw / 2^26
u16   fp_index_q10_6         raw CIA first-path index, Q10.6
u24   F1                     first-path amplitude point 1
u24   F2                     first-path amplitude point 2
u24   F3                     first-path amplitude point 3
u24   ip_power               Ipatov channel power
u16   accum_count            accumulated preamble symbols
u8    dgc_decision           dwt_get_dgcdecision
u16   cir_start_offset       absolute accumulator index of the first tap
u8    cir_taps               number of complex taps that follow
...   cir                    cir_taps * (i16 I, i16 Q)
u32   crc32                  see section 9
```

`fp_index_q10_6` MUST be the raw Q10.6 value. Rounding it to an integer sample
discards the sub-sample first-path resolution that environmental sensing depends
on.

`cir_start_offset` is an absolute accumulator index, not an offset relative to
the first path. This is unambiguous when the requested window clamps at index 0.

`cir_taps` is carried per subreport rather than taken from configuration so that
a window truncated near an accumulator boundary remains self-describing.

Derived quantities are **not** transmitted. The receiver carries the raw CIA
inputs (`F1`, `F2`, `F3`, `ip_power`, `accum_count`, `dgc_decision`) and the host
computes RSSI and first-path power from them, using `rx_preamble_code` from
configuration. This permits recomputation under corrected calibration, which
transmitting only the Q8.8 results would foreclose.

### 5.1 `obs_flags`

| Bit | Meaning |
| --- | --- |
| 0 | `cir_valid`. Cleared if a reception began during the accumulator read. |
| 1 | `cir_truncated`. The requested window was clipped at an accumulator boundary. |
| 2 | `fp_valid`. CIA first-path diagnostics were populated. |
| 3 | Partial observed superslot: not all `M` frames from `j` were received. |
| 4-7 | Reserved, MUST be 0. |

A host MUST discard the CIR of any subreport with `cir_valid` cleared. Timing
fields remain usable.

### 5.2 CIR sample format

Each tap is a complex pair of `i16` values, I then Q. Accumulator samples are
18-bit signed; firmware sign-extends from bit 17 and shifts right by 2 to fit
`i16`. This discards 2 bits and is a deliberate, documented deviation from
lossless preservation, inherited from validated firmware.

`cir_taps` MUST NOT exceed 128. The CIR window begins `cir_left_taps` samples
before the CIA first-path index, clamped at accumulator index 0.

---

## 6. Observation rules

A node MUST read the CIR for peer `j` **only from the `m = 0` frame** of `j`'s
superslot. If that frame is not received with a valid FCS, `U(i,j)` MUST be
omitted for that cycle and bit `j` of `peer_observed_bitmap` MUST be cleared.

This guarantees a uniformly sampled time series per link. Falling back to a later
frame would jitter the sampling instant by one slot, forcing the host onto
non-uniform spectral methods. A clean gap is preferred to a jittered sample.

Loss of a frame with `m > 0` MUST NOT invalidate `U(i,j)`. Such a frame carries
`j`'s relayed subreports, which is a distinct concern from `i`'s own observation
of `j`. Implementations MAY set `obs_flags` bit 3 to record that the observed
superslot was partial.

Only one node transmits per superslot, so every frame in superslot `k` originates
from node `k mod N` and attribution is unambiguous.

### 6.1 Callback ordering

The RX callback MUST perform its reads in this order:

1. `dwt_readrxdata`
2. `dwt_readdiagnostics_acc`
3. `dwt_readcir_48b`
4. **only then** `dwt_rxenable`

The CIR accumulator is **not** double-buffered. `RX_BUFFER_0`/`RX_BUFFER_1`
double buffering covers the frame buffer and per-frame diagnostics only; the
accumulator at `ACC_MEM_ID` is a single instance. Re-arming the receiver before
reading the accumulator allows a subsequent reception to overwrite it mid-read,
producing a corrupted CIR that would be reported as valid.

With the receiver disabled during the accumulator read, a frame arriving in that
window is cleanly missed rather than silently corrupting data. Under this
schedule frames begin only at slot boundaries, so if processing completes within
`slot_duration_us - frame_airtime_us` nothing is missed.

After the accumulator read, an implementation SHOULD check whether a reception
began during it and clear `obs_flags` bit 0 if so.

Double buffering MUST NOT be enabled as a substitute for this ordering.

---

## 7. Pooled report assembly

### 7.1 Sizing

All values are derived by `tools/config/heimdall_config.py` and are configuration
constants:

```
subreport_bytes     = 40 + 4 * cir_taps
pooled_report_max   = (N - 1) * subreport_bytes
frame_capacity_max  = max_frame_bytes - 31 - 2
M                   = ceil(pooled_report_max / frame_capacity_max)
frame_payload       = ceil(pooled_report_max / M)
frame_bytes         = frame_payload + 31 + 2
```

Slot duration is bounded below by **two independent constraints**; the floor is
whichever is larger. See section 13.2.

`frame_payload` derives from the **maximum** pooled report, not the actual one,
so it and `slot_duration_us` are constants independent of how many observations a
node happens to hold.

### 7.2 Packing

The pooled report is a byte stream split into `M` equal runs. Frame `m` carries
bytes `[m * frame_payload, (m+1) * frame_payload)`. A subreport MAY cross a frame
boundary.

Balanced packing is required rather than greedy fill. It minimises the maximum
frame size, which simultaneously lowers packet error rate for every frame and
shortens `slot_duration_us`, because the slot is sized to maximum frame airtime.

Byte-stream fragmentation is safe only because integrity is per-subreport
(section 9). A whole-report checksum would make a split subreport unverifiable.

### 7.3 Frame count and padding

A node MUST transmit exactly `M` frames in its superslot, padding beyond
`pooled_total_bytes` when it holds fewer subreports than the maximum. Padding
bytes MUST be transmitted as zero and MUST be ignored on receipt.

Every slot therefore always carries a frame, so a receiver never has to
distinguish a lost frame from one that was never sent.

Because `M` and `frame_payload` are constants, the byte offset of frame `m` is
derivable and is not transmitted. `pooled_total_bytes` locates the padding
boundary.

### 7.4 Subreport ordering

Subreports MUST be ordered by ascending `observed_node_id`, rotated by cycle
index:

```
start = (floor(k / N) + 1) mod N
order = [ (start + t) mod N  for t in 0 .. N-1 ]  with j == i removed
```

Exactly one subreport per cycle straddles a frame boundary and therefore requires
both frames to survive, carrying roughly double the loss probability. Rotation
moves each link through that position in turn, so all links converge to the same
loss rate. Fixed ordering would impose a permanent penalty on one link, and
systematic per-link bias is more damaging to a sensing dataset than uniform
random loss.

Subreports are self-describing via `observed_node_id`, so a host MUST NOT depend
on ordering to decode.

### 7.5 Gateway

The gateway transmits a full pooled report exactly like any other node. Its
subreports are redundant over the radio, since its own observations reach the
host directly over USB, but uniformity yields a single code path for schedule,
assembly, and synchronisation, and the redundancy allows the host to validate the
relay path against ground truth.

---

## 8. Synchronisation and liveness

### 8.1 Deriving schedule phase

A receiver derives slot phase in its own clock domain from `rx_timestamp`
together with `k` and `m` from the payload. It never needs the transmitter's
clock. Accuracy is bounded by time of flight, under 100 ns at 30 m, against slots
of milliseconds.

Clock rate correction is **not** required for scheduling. At the measured ~1.8
ppm offset, drift is approximately 43 ns per 20 ms cycle. Rate correction is
still required host-side for ranging.

### 8.2 Resynchronisation

- If a frame from `master_node_id` was received during the current cycle, its `k`
  MUST be authoritative.
- Otherwise a node MUST adopt `k` from any validated peer frame.
- Phase corrections MUST be applied only at cycle boundaries, never mid-superslot.
  A phase adjustment between computing a programmed transmit time and the
  hardware executing it could miss the deadline.

### 8.3 Bootstrap

On reset the master MUST listen before transmitting. If it observes a running
network it MUST adopt that network's `k` rather than restarting from 0, so that
peers are not forced backwards and the host's time index stays monotonic. If it
observes nothing it MAY begin at any `k`, conventionally 0.

Non-master nodes MUST NOT transmit until they have synchronised from a received
frame.

Receivers operate in continuous receive mode. This is the steady-state design,
not a bring-up expedient: every node must hear every other node's transmissions
to populate the link matrix.

### 8.4 Liveness

`evidence_age` conveys distance in cycles from direct observation of the master:

```
evidence_age = 0                               if master heard this cycle
             = 1 + min(evidence_age received)   otherwise
             = 0xFF                             if no evidence at all
```

A non-master node MUST NOT transmit unless
`evidence_age <= evidence_age_threshold`. The master always transmits.

Because ages are per-transmission rather than persisted, and each hop increments,
the network-wide minimum grows by at least 1 per cycle once the master stops.
Shutdown therefore converges in approximately `evidence_age_threshold` cycles
with no count-to-infinity.

This rule exists for power and spectrum hygiene, not correctness. Nodes remain
mutually synchronised without the master; only the data sink is lost.

---

## 9. Integrity

Each subreport carries a CRC32 over all preceding bytes of that subreport,
computed by the **observing** node at observation time and carried unchanged
through the relay.

The 802.15.4 FCS already covers each radio hop and `RXFCG` fires only on a valid
FCS. What the FCS does not cover is the relay: a subreport resides in the
observing node's RAM, is copied into a transmit buffer, crosses the air, and
crosses USB at the gateway. The subreport CRC32 covers that entire path, which is
the one hop unique to this protocol.

There MUST NOT be a whole-report checksum. It would fail whenever any frame of a
multi-frame report was lost, forcing correctly received frames to be discarded,
and it would render byte-stream fragmentation unverifiable.

A host MUST verify each subreport CRC32 before use and MUST count failures
separately from frames lost in transit.

---

## 10. Configuration binding

### 10.1 `config_hash`

`config_hash` is CRC-16/CCITT-FALSE (polynomial 0x1021, initial value 0xFFFF, no
input or output reflection, no final XOR) computed over the following packed
little-endian structure. Field order is normative.

```
u8   protocol_version        u16  sfd_timeout
u8   N                       u32  data_rate_kbps
u8   M                       u8   phr_mode        (0 std, 1 ext)
u8   master_node_id          u8   phr_rate        (0 std, 1 dta)
u16  network_id              u8   pdoa_mode
u16  max_frame_bytes         u8   tx_pg_delay
u16  frame_payload           u32  tx_power
u32  slot_duration_us        u8   evidence_age_threshold
u8   cir_taps                u8   enable_frame_filter
u8   cir_left_taps
u8   channel
u8   tx_preamble_code
u8   rx_preamble_code
u16  preamble_length
u8   pac
u8   sfd_type
```

Total 39 bytes. Hashing this packed form rather than the configuration text makes
the value immune to whitespace, key ordering, and number formatting.

`node_id` and antenna delays are excluded because they are legitimately
per-board.

### 10.2 Mismatch policy

Validation MUST be layered, and the configuration check MUST be the last stage.
This ordering is required: an earlier stage would let a foreign transmitter
silence the network.

1. PHY acquisition (preamble code, SFD type)
2. Hardware frame filter (PAN ID)
3. `protocol_version`, `src_addr < N`, plausible `k`
4. `config_hash` comparison

On a small number of **consecutive** mismatches at stage 4, a node MUST cease all
transmission while continuing to receive, log, and, if it is the gateway, export.
It MUST resume automatically after observing only matching values for a defined
interval.

Acting on a single mismatched frame is forbidden: one FCS-passing frame with
corrupted header bytes would silence a healthy node, and a silenced node also
stops contributing liveness evidence to its peers.

A `protocol_version` mismatch is handled identically.

### 10.3 Build binding

The configuration tool is authoritative for the values that are flashed. The
build MUST independently re-derive every value in the `derived` block and MUST
fail if any disagrees. See `tools/config/heimdall_config.py` and
`firmware/radio/app/cmake/heimdall_config.cmake`.

Firmware MUST additionally assert the invariants listed in section 12 at boot, so
that a hand-edited configuration cannot reach a board.

### 10.4 Rate control

`slot_duration_us` MUST be a multiple of 100 us and MUST NOT be below
`slot_floor_us`. It is otherwise free.

Raising it above the floor is the mechanism for reducing report rate and USB load.
There is no separate idle or cycle-period parameter: a longer slot achieves the
same effect while keeping every slot occupied and the schedule arithmetic uniform.

`slot_duration_us` in multiples of 100 us is exactly representable in device time
units. 1 ms is 63 897 600 DTU, so 0.1 ms is 6 389 760 DTU and is also a whole
number of the 512-DTU delayed-transmit register granularity. One microsecond is
**not** an integer number of DTU.

---

## 11. Deployment roster

The roster maps physical boards to schedule positions and holds per-board
calibration. It is a separate file from the beacon configuration.

Each entry MUST contain:

| Field | Purpose |
| --- | --- |
| `node_id` | Schedule position, `0 .. N-1` |
| `device_id` | `FICR->DEVICEID`, read via `hwinfo_get_device_id()` |
| `tx_antenna_delay_dtu` | Per-board calibration |
| `rx_antenna_delay_dtu` | Per-board calibration |

Firmware MUST read its own device ID at boot and MUST refuse to run if it does
not match the roster entry for its configured `node_id`. This makes duplicate
`node_id` assignment impossible to flash.

Duplicate assignment is the most damaging misconfiguration available: two boards
transmit in the same superslot indefinitely. It is also insidious, because mutual
jamming may leave neither frame decodable, so a receiver's "I heard my own
`src_addr`" check can fail to fire on exactly the case it exists for. Prevention
at flash time is therefore the primary mechanism and runtime detection is backup.

Implementations SHOULD additionally:

- Refuse to transmit on receiving a validated frame whose `src_addr` equals their
  own `node_id`.
- Report inconsistent `tx_timestamp` for the same `(src_addr, k, m)` to the host.

Antenna delays MUST be measured per board. The current shared value of 16385 DTU
is an uncalibrated default and biases every range.

---

## 12. Boot invariants

Firmware MUST verify all of the following at boot and MUST refuse to transmit if
any fails:

```
2 <= N <= 8
master_node_id       <  N
node_id              <  N
device_id            == roster[node_id].device_id
1 <= cir_taps        <= 128
0 <= cir_left_taps   <  cir_taps
max_frame_bytes      <= 1023
max_frame_bytes > 127  implies  phr_mode == ext
frame_bytes          <= max_frame_bytes
M * frame_payload    >= pooled_report_max
slot_duration_us mod 100 == 0
slot_duration_us     >= slot_floor_us
```

---

## 13. Timing model

### 13.1 Airtime

Frame airtime, used to derive `slot_floor_us`:

```
t_psym   = 1017.63 ns   (PRF 64)   |  993.59 ns  (PRF 16)
t_dsym   = 128.21 ns    (6800 kb/s) | 1025.64 ns (850 kb/s)
t_phrsym = t_dsym if phr_rate == dta else 1025.64 ns

SHR  = (preamble_length + sfd_symbols) * t_psym
PHR  = 21 * t_phrsym
DATA = ceil(frame_bytes * 8 / 330) * 378 * t_dsym

airtime = SHR + PHR + DATA
```

`sfd_symbols` is 8 for `sfd_type` 0, 1, and 3, and 16 for `sfd_type` 2.

The `330 -> 378` expansion is Reed-Solomon RS(63,55) over GF(2^6): 55 six-bit
data symbols become 63 coded symbols.

Note that the nominal `data_rate_kbps` is already the **net** rate after RS
coding. The coded symbol rate at 6.8 Mb/s is `1 / 128.21 ns = 7.8 Mb/s`, and
`7.8 * 330/378 = 6.81 Mb/s`. Dividing byte count by the nominal rate therefore
approximates the data field to within final-block padding — 0.67 percent at
1023 B — and does not double-count parity.

The significant error in a naive airtime estimate is **omitting SHR and PHR**,
which together total about 160 us independent of frame size: 12 percent of a
1023 B frame and 29 percent of a 329 B one. Implementations MUST include them.

`PHR = 21` symbols is the standard approximation and has not been confirmed for
`DWT_PHRMODE_EXT`, whose PHR encodes more length bits. It contributes about 21 us,
so an error here is small but systematic.

### 13.2 Slot floor

Two independent constraints bound `slot_duration_us`. The floor is the larger,
rounded up to the next 100 us.

**Constraint 1, reception.** Every slot carries a frame, so every slot must
accommodate that frame's airtime plus the RX callback that follows it.

```
floor_rx = airtime + margin * rx_processing
```

**Constraint 2, report assembly.** Node `i`'s pooled report must contain
`U(i, i-1)`, observed from the peer whose superslot immediately precedes its own.
The entire chain must complete between the end of that peer's `m = 0` frame and
the start of node `i`'s own `m = 0` frame:

```
read RX data -> read diagnostics -> read CIR -> compute subreport CRC32
             -> assemble pooled report -> write TX buffer -> program delayed TX
```

The available window is `M * slot_duration_us - airtime`, so:

```
floor_assembly = (airtime + margin * (rx_processing + assembly + tx_write)) / M
```

**TX preparation MUST NOT be assumed pipelined into the preceding frame's
airtime.** The bytes being written include the measurement of that very frame, so
they cannot exist before it has been received and processed.

Only `M = 1` configurations are bound by constraint 2. For `M >= 2` the extra
slot provides ample slack and constraint 1 dominates:

| N | M | `floor_rx` | `floor_assembly` | binding |
| --- | --- | --- | --- | --- |
| 2 | 1 | 1300 us | 1600 us | assembly |
| 3 | 1 | 1800 us | 2200 us | assembly |
| 4 | 1 | 2200 us | 2800 us | assembly |
| 5 | 2 | 1800 us | 1100 us | reception |
| 6 | 2 | 2000 us | 1300 us | reception |
| 7 | 2 | 2200 us | 1400 us | reception |
| 8 | 3 | 1900 us | 800 us | reception |

Figures are at 64 taps, `max_frame_bytes` 1023, 32 MHz SPI, margin 1.5. The
122 us report-assembly input and 1.92 B/us CRC throughput are measured; RX SPI
and TX-write budget inputs remain modelled as described by the bring-up notes.

An implementation that cannot meet constraint 2 MAY instead report the
`round_delta = 1` peer's observation one full cycle later, using the
`round_delta` field to express the additional latency. Doing so MUST be uniform
across all cycles, never conditional, since a conditional deferral would
reintroduce the sampling jitter that section 6 exists to prevent.

### 13.3 RX processing

```
rx_processing = frame_bytes * spi_byte_time
              + ceil(cir_taps / 16) * (105 * spi_byte_time + transaction_overhead)
              + diagnostics_bytes * spi_byte_time + transaction_overhead
              + subreport_bytes / crc32_throughput
              + fixed_overhead
```

The CIR term reflects the driver's 16-sample chunking: each chunk is one dummy
byte plus six bytes per complex sample, preceded by two 32-bit indirect-pointer
register writes.

The CRC32 term is not negligible. Section 9 places the subreport CRC32 in the
observing node's callback, and at roughly 8 bytes per microsecond a 296 B
subreport costs about 37 us.

`tx_write` is `frame_bytes * spi_byte_time + transaction_overhead`.

---

## 14. Relationship to the USB contract

`contracts/usb-cdc-v1.md` is normative for the gateway-to-host stream.

The gateway forwards each received frame verbatim as a `RADIO_FRAME` record,
prefixed by an 8-byte wrapper carrying its local RX timestamp and validation
flags, inside 16 bytes of outer framing. It MUST NOT re-encode subreports: doing
so would leave the host unable to verify the CRC32 computed by the observing node
and would destroy end-to-end integrity.

The gateway firmware performs only the fixed-header, ownership, phase, and
configuration checks needed for radio safety. It MUST NOT parse or reassemble
relayed pooled reports in the radio callback. Fragment reassembly, padding
validation, per-frame start-count validation, subreport decoding, and subreport
CRC32 validation belong to the UNO Q ingest path. This keeps semantic processing
out of the timing-critical radio path while preserving verbatim bytes for
end-to-end validation and replay.

The gateway's own observations are exported as `LOCAL_OBS` records whose
subreport bytes are **byte-identical in layout** to those carried over the radio,
so the host has one decoder and one CRC check for both paths.

Because the gateway cannot receive its own transmissions, it MUST emit a
`TX_RECORD` per transmitted frame. Ranging on links where the gateway is the
transmitter depends on its programmed transmit timestamps reaching the host.

The radio MUST NOT block on USB backpressure. Queue overruns MUST be counted at
the producer and reported in `CYCLE_SUMMARY`; they MUST NOT be silent.

**The USB contract constrains this one.** Its record overheads feed the gateway
throughput calculation, which is compared against `budget.usb_budget_bytes_per_s`
and determines whether a beacon configuration may be built. Per cycle the gateway
emits `(N-1) * M` `RADIO_FRAME`, `(N-1)` `LOCAL_OBS`, `M` `TX_RECORD`, and one
`CYCLE_SUMMARY`. Across N=2..8 at 64 taps this is 234-433 kB/s, within the
interrupt-driven transport's verified 475 kB/s offered load.

---

## 15. Changes from v0

| v0 | v1 |
| --- | --- |
| `sender_node_id` u16 | u8 `src_addr` in the MAC header; `N_MAX = 8` |
| `slot_id` in the beacon | `m` plus `k`, with the schedule derived from `k mod N` |
| `fragment_index`, `fragment_count` | Removed. Fixed `M` with balanced byte-stream packing. |
| `payload_crc` over the beacon | Per-subreport CRC32, end-to-end across the relay |
| `report_count` | `subreport_count` plus `peer_observed_bitmap` |
| No MAC header, no frame filtering | 802.15.4 MAC header with hardware PAN filtering |
| Exact widths undefined | Fully specified, sections 4 and 5 |
| No configuration binding | `config_hash`, roster device-ID binding, build verification |
