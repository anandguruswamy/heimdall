# The Heimdall Beaconing Scheme, Explained

Audience: an engineer comfortable with embedded systems and RF who has not
worked with this codebase. Some UWB familiarity helps but is not assumed.

This document explains *what* the protocol does and *why*. The normative spec is
`contracts/beacon-v1.md`; the decision-by-decision rationale is
`docs/protocol-decisions.md`.

---

## 1. The problem

Heimdall is a multi-node ultra-wideband sensing system. Up to eight boards sit in
a space. One is tethered to a Linux host (an Arduino UNO Q) over USB; the rest
are untethered and have **no** backhaul other than the UWB radio itself.

The goal is not primarily to locate things. It is to capture, for every ordered
pair of nodes, a **channel impulse response** — a time-domain picture of how a
radio pulse from one node arrived at another, including all its reflections. With
`N` nodes there are `N(N-1)` such directed links, so 30 at N=6 and 56 at N=8.
Changes in those impulse responses over time reveal what is moving in the space.

This creates an awkward constraint. The host can only see what the gateway hears
directly, which is `N-1` links. The other `N(N-1) - (N-1)` links exist only
between untethered nodes. **The only way to get them to the host is over the same
radio that is making the measurements.** The radio is simultaneously the
instrument and the data bus.

Everything distinctive about this protocol follows from that.

---

## 2. What a CIR is, and why it is expensive

An impulse-radio UWB receiver correlates the incoming signal against a known
preamble sequence. The DW3110 accumulates the result of that correlation into a
memory buffer. Reading that buffer gives you the **channel impulse response**:
one complex sample per approximately 1.0016 ns of delay.

A direct line-of-sight path shows up as a sharp peak. A reflection off a wall
arrives later and appears as a second, smaller peak, delayed by the extra path
length divided by the speed of light. Since 1 ns of delay corresponds to about
0.3 m of extra path, a 64-tap window spans roughly 19 m of path length — enough
to capture the direct path and first-order room reflections.

A person moving through the space changes those reflections. That is the sensing
signal.

The cost: each complex tap is a 16-bit real and a 16-bit imaginary value, so
**4 bytes per tap**. A 64-tap window is 256 bytes and a 128-tap window is 512
bytes — for *one* observation of *one* link. Multiply by `N-1` peers per node and
by `N` nodes, and the data volume becomes the dominant design constraint.

For context, the classic UWB application — two-way ranging — needs only a handful
of timestamps per exchange. Heimdall needs three orders of magnitude more data
per measurement.

---

## 3. Time structure: slots, superslots, cycles

There is no contention, no carrier sense, and no acknowledgement. Time is divided
deterministically.

```
slot        one frame transmission. Uniform duration.
superslot   M consecutive slots. Exactly one node transmits in a superslot.
cycle       N consecutive superslots, so every node transmits exactly once.
```

A free-running counter `k` numbers the superslots. The entire schedule is one
line:

```
node i transmits during superslot k  if and only if  k mod N == i
```

That is the central idea. A node's whole schedule collapses to an integer
comparison, and — importantly — a node that hears a single frame learns `k` from
the payload and can immediately compute the whole future schedule. There is no
association procedure, no beacon-tracking state machine, and no negotiation.

Here is a cycle for `N = 4`, `M = 2`:

```
superslot k:      0         1         2         3         4
                  |         |         |         |         |
k mod N:          0         1         2         3         0
transmitter:     B0        B1        B2        B3        B0
                  |         |         |         |         |
slots:          [0][1]    [0][1]    [0][1]    [0][1]    [0][1]
                 m=0 m=1   m=0 m=1   m=0 m=1   m=0 m=1
                  \___/
                    |
              one pooled report, split across M frames

<------------------- one cycle, N*M = 8 slots ------------------>
```

`M` is not chosen by hand. It falls out of how much data a node must send, which
is the subject of the next section.

---

## 4. Every frame is both a beacon and a report

This is the second central idea. A transmitted frame does double duty:

1. **As a beacon**, it is a signal that all other nodes measure. Each receiver
   records the arrival timestamp, the carrier frequency offset, the first-path
   index, and the CIR. That measurement *is* the sensing data for that link.
2. **As a report**, its payload carries the measurements that node made of
   *everyone else* during the preceding cycle.

So when node `i` transmits, five other nodes are simultaneously measuring link
`i -> j` while reading `i`'s account of links `i -> everyone`. No airtime is
spent on anything that is not both a measurement and a data transfer.

The payload of node `i`'s transmission is its **pooled report**, a concatenation
of **subreports**:

```
U(i,j) = node i's observation of node j's most recent transmission
```

carrying `{ j, CIR, first-path index, RX timestamp, CFO, gain state,
raw CIA diagnostics, CRC }`.

Because node `j` transmitted in a known earlier superslot, the observation's age
is fully determined:

```
k_observed = k - ((i - j) mod N)
```

Nothing needs to be transmitted to say *when* an observation was made. It is
implied by who is reporting about whom.

### Worked example, N = 4

Node 1 transmits in superslots 1, 5, 9, ... When it transmits in superslot 9, it
reports:

| Subreport | Describes node | Observed in superslot | Age |
| --- | --- | --- | --- |
| `U(1,0)` | node 0 | 8 | 1 superslot |
| `U(1,2)` | node 2 | 7 | 2 superslots |
| `U(1,3)` | node 3 | 6 | 3 superslots |

Meanwhile nodes 0, 2, and 3 are all measuring the link from node 1 as they
receive it. One transmission, three new measurements plus three relayed ones.

---

## 5. Why `M` exists: the payload does not fit

A UWB frame is limited. With the DW3000's extended PHY header mode the ceiling is
1023 bytes; without it, 127.

At 64 taps a subreport is `40 + 4*64 = 296` bytes. At N=6 a node must report on
five peers, so the pooled report is 1480 bytes. That does not fit one frame.

Hence `M`: the pooled report is split across `M` frames, all transmitted
back-to-back by the same node in its superslot.

```
M = ceil(pooled_report_max / frame_capacity)
```

At N=6 and 64 taps that gives `M = 2`. At 128 taps the subreport doubles to 552
bytes, the report becomes 2760 bytes, and `M = 3`.

### Balanced splitting, and why it is free performance

The obvious way to split is greedily: fill frame 0 to capacity, put the remainder
in frame 1. For N=6 that gives 990 + 490 bytes.

The better way is to balance: 740 + 740.

This matters because **the slot duration is sized to the largest frame**. A slot
must accommodate the longest frame's airtime plus the receiver's processing time.
Balancing shrinks the largest frame from 1023 bytes to 773, which shortens the
airtime, which shortens the slot, which shortens the cycle, which raises the
report rate — by roughly 15% at N=6, for nothing. It also lowers the packet error
rate of every frame, since error probability grows with frame length.

The cost is that a subreport can now straddle a frame boundary. That is safe only
because integrity is checked per subreport rather than per report — see section 8.

---

## 6. Timing: how nodes agree without a shared clock

Every board runs its own crystal. There is no wired sync, no PPS, and no common
time base. Yet the schedule requires that nodes not transmit on top of each other.

Three properties make this straightforward.

**Transmit times are exactly predictable.** The DW3000 can be told to transmit at
a specific value of its 40-bit internal clock, which ticks at about 15.65 ps per
unit. On this hardware, measured across 1000 frames, the difference between the
programmed transmit time and the actual transmit time was **zero** clock units.
So a node can put its own upcoming transmit timestamp *into the frame it is about
to send* — a value it knows in advance with certainty.

**A receiver does not need the sender's clock.** When a node receives a frame it
has: its own local arrival timestamp, and `k` and `m` from the payload. That is
sufficient to know when slot `m` of superslot `k` occurred *in its own clock
domain*. The only error is time of flight, under 100 ns at 30 m. Against slots
measured in milliseconds, that is a margin of roughly 20 000 to 1.

**Drift is negligible over a cycle.** The measured frequency offset between
boards is about 1.8 ppm, which is 43 ns of drift per 20 ms cycle. Since every node
re-derives phase from received frames every cycle, drift never accumulates. No
frequency correction is needed for scheduling at all. (It *is* needed for ranging;
see section 10.)

The consequence is that synchronisation requires no protocol machinery. A cold
node listens, receives any single frame, and is synchronised.

---

## 7. Startup, and the liveness rule

One node is designated **master** by configuration. It is the only node permitted
to transmit without having heard anyone, which is what breaks the startup
deadlock.

```
Master boots  ->  listens briefly  ->  hears nothing  ->  starts transmitting at k=0
Peer boots    ->  listens          ->  hears any frame ->  synchronised -> transmits
```

The master listens first even though it is the reference. If it has merely
rebooted while the network is still running, adopting the existing `k` avoids
dragging every node's time index backwards.

### Why nodes stop when the master disappears

If the master dies, the peers remain perfectly synchronised to each other and
could keep running forever. But there is no longer anywhere for the data to go, so
transmitting is a waste of power and spectrum.

Each frame carries a one-byte `evidence_age`:

```
evidence_age = 0                              if the master was heard this cycle
             = 1 + min(ages heard this cycle) otherwise
             = 0xFF                           if no evidence at all
```

A node transmits only while `evidence_age <= threshold`. This is a
distance-in-hops measure, so a node that cannot hear the master directly may still
transmit on the strength of a neighbour that can.

The mechanism terminates cleanly. Ages are recomputed per transmission rather than
remembered, and each relay hop adds one, so once the master stops the minimum age
in the network rises by at least one per cycle. Everyone falls silent after about
`threshold` cycles.

Note this is a hygiene mechanism, not a correctness mechanism. The network does
not *need* the master to stay coherent.

---

## 8. Loss, and why integrity is per-subreport

Frames get lost. Under this design a loss has two quite different meanings, and
the protocol keeps them separate.

**Loss of a measurement.** Node `i` reads the CIR for peer `j` only from the
`m = 0` frame of `j`'s superslot. If that specific frame is lost, `U(i,j)` is
simply absent for that cycle.

Why not fall back to `m = 1`? Because it would jitter the sampling instant of the
time series by one slot. Sensing analysis usually rests on uniformly sampled data
so that an FFT is valid; an occasional sample displaced by 2 ms is worse than an
occasional missing sample, because the gap can be masked whereas the jitter
contaminates the spectrum. A clean hole beats a distorted sample.

**Loss of a relayed report.** If a frame with `m > 0` is lost, node `i`'s own
observation of `j` is unaffected — that came from `m = 0`. What is lost is `j`'s
account of *its* peers.

Keeping these separate is why a one-byte `peer_observed_bitmap` rides in every
header. A cleared bit means "I did not observe that peer" (a link problem). A set
bit whose subreport never arrives means "I did observe it, but the frame carrying
it was lost" (a collision or interference problem). Different diagnoses, different
fixes.

### Where the checksum goes

The 802.15.4 frame check sequence already protects each radio hop; the receiver's
interrupt only fires on a good FCS. So a frame-level checksum adds little.

What the FCS does *not* protect is the relay. A subreport is measured by node `i`,
held in `i`'s RAM, copied into a transmit buffer, sent over the air, received by
the gateway, and pushed across USB. Only some of that path is covered by any FCS.

So the CRC32 is computed **per subreport, by the observing node, at observation
time**, and carried untouched all the way to the host. This has three
consequences:

- A partially received pooled report is still usable — every intact subreport
  verifies on its own.
- A subreport may straddle a frame boundary, which is what makes balanced packing
  (section 5) possible.
- The gateway must forward subreport bytes **verbatim**. If it re-encoded them,
  the host could no longer check the CRC that the measuring node computed, and the
  end-to-end guarantee would evaporate.

---

## 9. The hardware constraints that shape the design

Three properties of the DW3000 and the firmware architecture leave visible marks
on the protocol.

**The receive callback is serialised in a thread, not an interrupt.** The driver's
GPIO interrupt handler defers to a Zephyr work queue, so all frame processing runs
in a single thread. Frame `n` must be fully processed before frame `n+1`'s
interrupt is even serviced. This makes the per-frame processing time a hard
scheduling constraint, not a soft one, and it is why the slot duration model
includes an explicit processing term.

**A node cannot prepare its transmission in advance.** This is subtler than it
looks and it caught the first version of the timing model.

When node `i` transmits, its report must include its measurement of node `i-1` —
the node that transmitted immediately before it. So the bytes going into the
transmit buffer depend on the frame that was still arriving moments earlier. They
cannot be assembled ahead of time.

That puts a whole chain on the critical path between two consecutive
transmissions:

```
end of peer's frame
  -> read data out of chip -> read CIR out of chip -> compute the CRC
  -> assemble the report -> write ~900 bytes back into the chip
deadline: start of our own frame
```

The window is `M x slot - airtime`. When `M >= 2` a node has a whole spare slot
and this is comfortable. When `M = 1` it is the binding constraint, tighter than
the receive path it was originally modelled on. So the slot floor is the larger
of two independent limits, one for receiving and one for assembling, and which
one wins depends on `M`.

**The CIR accumulator is not double-buffered.** The DW3000 has two frame buffers
and will happily receive frame `n+1` while you read frame `n`. But there is only
*one* accumulator. Reading it takes several hundred microseconds of SPI traffic,
and any reception during that window overwrites it.

This has a sharp consequence: the receiver must **not** be re-armed until the
accumulator has been read. The original firmware re-armed first, which is harmless
at a 20 ms frame period but corrupts data under a protocol with a frame in every
slot. And no amount of buffering can fix it, because the accumulator is written
during preamble acquisition, before any address field exists to filter on.

The chosen ordering — read data, read diagnostics, read CIR, *then* re-arm —
converts the failure from "silently corrupted CIR reported as valid" into "frame
cleanly missed and counted". A missing sample is recoverable; a plausible-looking
wrong one is not.

**Airtime is dominated by the preamble, not the payload rate.** A UWB frame
begins with a long preamble: 128 preamble symbols at about 1017.63 ns each, plus
an 8-symbol start-of-frame delimiter, is 138 µs before a single payload bit is
transmitted. The PHY header adds another 21 µs. That ~160 µs is fixed regardless
of frame size — 12% of a 1023-byte frame and 29% of a 329-byte one.

The payload itself carries Reed-Solomon parity, every 330 data bits becoming 378
coded bits, so the model is

```
airtime = SHR + PHR + ceil(bits/330) * 378 * symbol_time
```

One trap worth naming, because it caught this project during design review: the
nominal "6.8 Mb/s" is already the **net** rate after RS coding. The coded symbol
rate is 1/128.21 ns = 7.8 Mb/s, and 7.8 × 330/378 = 6.81 Mb/s. So dividing bytes
by 6.8 Mb/s does *not* double-count parity — it approximates the data field to
better than 1%. It was briefly asserted here that it overestimated by 14%; that
was wrong, and it is now a unit test. What a naive estimate actually gets wrong is
forgetting the preamble.

Getting this wrong propagates into every slot and rate figure in the system, so
the model lives in exactly one place: `tools/config/heimdall_config.py`.

---

## 10. What the host can compute

The gateway forwards received frames verbatim over USB CDC. The host reassembles
pooled reports and ends up with, for each cycle, a set of observations covering
the full `N(N-1)` link matrix.

The stream itself is defined by `contracts/usb-cdc-v1.md`. Received frames are
forwarded byte-for-byte inside a `RADIO_FRAME` record; the gateway's own
measurements go out as `LOCAL_OBS` records whose subreport layout is identical, so
the host has one decoder for both paths. A `TX_RECORD` per transmitted frame
carries the gateway's own programmed transmit timestamps, which the host cannot
obtain any other way since a radio cannot hear itself. A `CYCLE_SUMMARY` per cycle
carries the loss counters.

**Sensing.** For each link, a time series of complex CIRs sampled once per cycle.
Changes across that series are the sensing signal. Because sampling is uniform by
construction (section 8), ordinary spectral methods apply directly.

**Ranging.** Every frame's programmed transmit timestamp is broadcast, and every
receiver's arrival timestamp is reported, so the host has both directions of every
pair within one cycle:

```
round = R(i,j) - T(i)     both in node i's clock
reply = T(j)   - R(j,i)   both in node j's clock
```

Clock offsets cancel in the standard two-way formulation. One caveat specific to
this schedule: the two directions of a pair are separated by up to `N-1`
superslots rather than the microseconds of a conventional two-way exchange. At
1.8 ppm that is tens of nanoseconds of drift, which is metres of range error if
ignored. This is why the measured carrier frequency offset is carried in every
subreport and why applying it is mandatory rather than optional.

**Diagnostics.** Rather than transmitting computed signal-strength values, each
subreport carries the raw accumulator diagnostics that the vendor formulas consume
(`F1`, `F2`, `F3`, channel power, accumulated symbol count, gain decision). The
host computes strength and first-path power from those. This costs about 10 bytes
per observation and buys the ability to recompute archived captures if the
calibration is ever corrected — which transmitting only the final values would
have made impossible.

---

## 11. Configuration, and why it is enforced so hard

Almost nothing in this protocol is a fixed constant. `N`, `M`, tap count, slot
duration, frame size, and every PHY parameter are configuration. A browser tool
lets an operator explore the space and see the resulting rate, airtime, and USB
load, then export a configuration file that the firmware build consumes.

That flexibility creates a serious failure mode. Every node derives its transmit
schedule from `k mod N`. If one board is flashed with a stale `N`, it transmits
inside another node's superslot — permanently, and it corrupts frames rather than
merely being useless. Similar reasoning applies to a wrong frame size (truncation)
or a wrong slot duration (accumulator corruption). All of these are silent.

Three independent mechanisms guard against it.

**On the wire.** Every frame carries a 16-bit `config_hash` over a packed struct
of all interoperability-relevant parameters. A node that sees consecutive
mismatches stops transmitting but keeps receiving and logging, and resumes
automatically once agreement returns. Crucially, the hash check is the *last*
validation stage, after PHY acquisition, PAN filtering, and header sanity — placed
earlier, a foreign transmitter could silence the whole network by accident.

**At build time.** The configuration tool is authoritative for the values that get
flashed, but `tools/config/heimdall_config.py` independently re-derives all of
them and fails the build on any disagreement. Since the sizing formulas
necessarily exist twice — once in browser JavaScript, once in the reference model
— this cross-check is what makes that duplication safe.

**At boot.** Firmware asserts a list of invariants (`frame_bytes` fits,
`M * frame_payload` is sufficient, the slot is above its feasibility floor, taps
within range) so that a hand-edited configuration cannot reach a board.

Separately, each board's `node_id` is bound to its silicon device ID in the
deployment roster and checked at boot. Duplicate `node_id` is the worst available
misconfiguration — two boards transmitting in the same superslot forever — and it
is insidious, because their mutual jamming can leave neither frame decodable, so
a receiver's "I heard my own address" check may never fire. Preventing the misflash
is more reliable than detecting it.

---

## 12. What this design deliberately does not do

Being explicit about the boundaries.

**No security.** STS, the 802.15.4z cryptographic scrambled timestamp sequence, is
permanently disabled. A transmitter that copies the PAN ID and preamble code is
indistinguishable from a legitimate node. The threat model is accidental
interference from other UWB equipment, not an adversary. Isolation comes from
choosing a distinct preamble code (which prevents the receiver from acquiring
foreign frames at all), a non-standard SFD, and hardware PAN filtering.

**No retransmission.** Observations are best-effort. A lost measurement is
counted and reported as a gap, never resent. Retransmission would need buffering
and would break the fixed slot budget the whole schedule depends on.

**No multi-hop routing.** Every node is assumed to hear every other node, which
the full link matrix requires anyway. The `evidence_age` mechanism tolerates a
node that cannot hear the master directly, but there is no general routing.

**No dynamic membership.** `N` is fixed at build time and changing it requires
reflashing every board. Given that `N` appears in every node's schedule
arithmetic, negotiating it at runtime would be a substantial distributed consensus
problem for a system that has six boards on a bench.

---

## 13. Worked numbers

For orientation. All figures from `tools/config/heimdall_config.py`, channel 9,
PRF 64 MHz, 128-symbol preamble, 6.8 Mb/s, 1023-byte frame ceiling, 32 MHz SPI.

| | N=2, 64 taps | N=6, 64 taps | N=6, 128 taps |
| --- | --- | --- | --- |
| Subreport | 296 B | 296 B | 552 B |
| Pooled report | 296 B | 1480 B | 2760 B |
| `M` | 1 | 2 | 3 |
| Frame size | 329 B | 773 B | 953 B |
| Frame airtime | 0.55 ms | 1.08 ms | 1.32 ms |
| RX processing (modelled SPI, measured CRC) | 0.46 ms | 0.57 ms | 0.87 ms |
| TX buffer write (modelled) | 0.09 ms | 0.20 ms | 0.24 ms |
| Slot floor | 1.6 ms | 2.0 ms | 2.7 ms |
| Which limit binds | assembly | reception | reception |
| Cycle at the floor | 3.2 ms | 24.0 ms | 48.6 ms |
| Per-link rate at the floor | 313 Hz | 42 Hz | 21 Hz |
| Links | 2 | 30 | 30 |

Three things are worth drawing out.

**Rate falls roughly as 1/N².** Not 1/N, because `M` itself grows with `N` — more
peers means a larger pooled report means more frames per superslot.

**Which limit binds depends on `M`.** At `M = 1` (N ≤ 4) the slot is set by how
fast a node can package the observation it just made, not by how fast it can
receive. At `M ≥ 2` the reception path dominates and the assembly path has roughly
half the slot to spare.

**Gateway USB load is roughly invariant.** Across every value of `N` from 2 to 8
at 64 taps it stays within 234-433 kB/s, because the radio runs saturated
regardless of the configuration; throughput is essentially
`frame_payload / slot_duration`. It is not even monotonic — it peaks where the
frame size happens to land near the 1023-byte ceiling. This
makes USB, not the radio, the binding constraint on the whole system, and it is
why the configuration tool refuses to export a configuration exceeding a USB
budget.

The lever for backing off is `slot_duration_us`, which may be set anywhere at or
above the feasibility floor. Lengthening the slot lowers the rate and the USB load
without introducing idle slots or a second timing parameter. The N=2 example above
runs at 313 Hz at the floor, which no sensing application needs; the shipped
example configuration sets a 10 ms slot instead, giving a 50 Hz per-link rate and
about 37 kB/s of USB traffic.

---

## 14. Reading order

| Document | Purpose |
| --- | --- |
| This file | Concepts and rationale |
| `contracts/beacon-v1.md` | Normative radio wire format and behaviour |
| `contracts/usb-cdc-v1.md` | Normative gateway-to-host stream |
| `docs/protocol-decisions.md` | Every decision, alternatives, and what was given up |
| `tools/config/heimdall_config.py` | Reference sizing model and verifier |
| `deployment/beacon-config.example.json` | A worked configuration |
| `firmware/radio/BRINGUP-NOTES.md` | Measured hardware results underpinning the timing claims |

**Status.** The protocol is specified but not implemented. Several numbers above
are modelled rather than measured — notably the RX processing time and the USB
throughput ceiling. Both are listed as bring-up gates in the decision log and must
be measured before the design is trusted quantitatively.
