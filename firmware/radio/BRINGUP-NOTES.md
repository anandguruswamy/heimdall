# Phase 1 bring-up notes

All measurements below must come from the two bench boards. Placeholder rows are
left explicitly pending rather than inferred from build success.

## Step 1.1: toolchain and workspace

- Rollback image verified before any erase: 922157 bytes, SHA-256
  `E7CE0877277176F33F23FEC34120D3A87EDD59393E33EAA56E5AB987599600B6`.
- Connected probes: `760200606` (COM5), `760223921` (COM8).
- Pinned compatibility basis and host-tool versions: see `VERSIONS.md`.
- `hello_world` build: PASS for `nrf52833dk/nrf52833` using Zephyr
  `fd9204a02d52`; 19,908 bytes flash and 4,544 bytes RAM.

## Step 1.2: first flash and console

- Board `760200606`: PASS. First Zephyr transition used a full-chip erase;
  COM5 at 115200 printed the Zephyr banner and 500 ms heartbeats.
- Board `760223921`: PASS. First Zephyr transition used a full-chip erase;
  COM8 at 115200 printed the Zephyr banner and 500 ms heartbeats.
- Both applications toggle active-low D9 (P0.04) every 500 ms. Visual bench
  confirmation remains to be recorded; UART and GPIO initialization passed.
- Rollback-to-FiRa-and-back proof: PASS on `760200606`. A full erase followed
  by programming the stored FiRa HEX verified all three HEX segments. A second
  full erase and Zephyr program verified successfully, after which COM5 resumed
  the Zephyr banner/heartbeat. The documented range-only erase was insufficient
  when transitioning from Zephyr; use a full erase for rollback from Zephyr.
- FiRa runtime BLE advertisement validation was unavailable because the UNO Q's
  BlueZ adapter returned `org.bluez.Error.NotReady`; this did not affect the
  J-Link erase/program/readback verification or return-to-Zephyr proof.

## Step 1.3: DW3110 register access at 8 MHz

| Board | DEV_ID |
|---|---|
| `760200606` | `0xDECA0302` (PASS) |
| `760223921` | `0xDECA0302` (PASS) |

- Both reads used the plan's known-good pin map and 8 MHz legacy SPI. On each
  board, `dw3000_hw_init=0` and `dwt_probe=0` preceded the DEV_ID read.

## Step 1.4: IRQ-driven TX/RX

| Direction | Received / sent | Reception rate | IRQ evidence |
|---|---:|---:|---|
| `760200606` to `760223921` | 999 / 1000 | 99.90% | TX IRQ 1000/1000; RX callback 999; 0 TX failures/timeouts, 1 RX error |
| `760223921` to `760200606` | 1000 / 1000 | 100.00% | TX IRQ 1000/1000; RX callback 1000; 0 TX failures/timeouts, 0 RX errors |

- PHY: channel 9, PRF 64 MHz, preamble length 64, 6.8 Mb/s, STS off.
- Frames were sent every 20 ms. RX completion and RX error handling were both
  callback/IRQ driven; the main RX loop only reported accumulated counters.
- M1.3 checkpoint: PASS in both directions (>99% over 1000 transmitted frames).

## Step 1.5: beacon-scheme primitives

- CIR window per received frame: PASS. In the compact scheduled run, board
  `760223921` received 999/1000 frames and performed 999 64-tap Ipatov
  accumulator reads. Every read was centered 16 taps before the CIA first-path
  index. One frame was lost with one RX error.
- CIA diagnostic logging must be set to `DW_CIA_DIAG_LOG_ALL` before calling
  `dwt_readdiagnostics_acc()` with this pinned driver. Without it, the function
  reads fields outside its minimally populated temporary buffer. The legacy
  `dwt_readaccdata()` direct-access branch also treated the low offset in a way
  that produced a noise-only window; the final code uses the driver's current
  `dwt_readcir_48b()` accumulator path.
- CIR shape validation: PASS against the old rig's expected leading noise floor
  -> first-path spike -> decay shape. Board `760200606` had CIA first paths
  737-743, full-accumulator peaks 739-745, and window peaks at relative taps
  17-18; mean peak/leading-noise was 40.7 dB and mean peak/tail was 32.1 dB.
  Board `760223921` had CIA first paths 737-744, full peaks 739-746, relative
  peaks 17-18, 37.0 dB mean peak/leading-noise, and 31.4 dB mean peak/tail.
  Canonical 16-frame captures are `captures/cir-760200606.csv` and
  `captures/cir-760223921.csv`.
- Scheduled TX: PASS over 1000 frames at a 20 ms radio-clock period. TX IRQ
  completions were 1000/1000 with zero late starts and zero IRQ timeouts.
  Actual TX timestamp minus aligned programmed timestamp was 0 DTU for every
  frame (min/max/mean/RMS jitter all 0 DTU, i.e. below one 15.65 ps DTU and far
  below the 10 us gate).
- Fine RX timestamps: PASS; one 40-bit timestamp was logged for every one of the
  999 received scheduled frames, including correct wrap through 2^40.
- CFO: PASS. With `760200606` transmitting and `760223921` receiving, raw clock
  offset was -159 to -93, average -123. Using the driver's ratio `raw / 2^26`,
  this is -2.36 to -1.38 ppm, average -1.83 ppm.
- SS-TWR: PASS at the radio primitive level. At 8 MHz SPI, a 1000-UUS responder
  delay missed 84/94 attempted delayed-TX deadlines, so the validated profile
  uses 3000 UUS. It completed 100/100 exchanges with zero late replies or
  timeouts. Typical uncalibrated TX/RX antenna delays were both 16385 DTU.
  Raw range averaged -444 mm. CFO-corrected range averaged 117 mm, with a
  -38-to-260 mm observed range; CFO raw was 59-107, average 81.
- Tape-measured antenna separation: approximately 250 mm (user reported about
  25 cm).
- CFO-corrected SS-TWR absolute error: approximately 133 mm (13.3 cm), using
  the 117 mm corrected mean; PASS against the <=300 mm Gate G1 limit.

## Phase 2 prerequisite: SPIM3 at 32 MHz

- Result: PASS on both bench boards. The default 8 MHz overlay remains available
  as the rollback profile; the explicitly selected SPIM3 overlay uses the same
  physical pin map with `NRF_DRIVE_H0H1`.
- Errata review: Nordic's nRF52833 errata do not list a SPIM anomaly. The nrfx
  workarounds for SPIM3 anomalies 195 and 198 in the pinned source are scoped to
  nRF52840, not nRF52833. Hardware stress testing below was still required.
- DEV_ID: `760200606` and `760223921` each returned `0xDECA0302` with
  `dwt_probe=0` at 32 MHz.

| Direction | Received / sent | Reception rate | CIR stress |
|---|---:|---:|---|
| `760223921` to `760200606` | 997 / 1000 | 99.70% | 997/997 received frames read a 64-tap CIR; 3 lost, 4 RX errors |
| `760200606` to `760223921` | 999 / 1000 | 99.90% | 999/999 received frames read a 64-tap CIR; 1 lost, 1 RX error |

- A separate bidirectional IRQ-link pass measured 997/1000 (99.70%) from
  `760223921` to `760200606` and 998/1000 (99.80%) in the reverse direction.
  Both transmitters completed 1000/1000 TX IRQs with no start failures or IRQ
  timeouts.
- CIR shape remained consistent with the 8 MHz reference captures. Receiver
  `760200606` ended with first-path index 738 and a window peak at relative tap
  18; receiver `760223921` ended with first-path index 738 and a peak at tap 17.
  Peak powers were 8,071,722 and 7,803,520 versus leading-window powers 524 and
  1,016, respectively.
- Scheduled TX passed in both directions: 1000/1000 TX IRQ completions, no late
  starts or timeouts, and programmed-versus-actual timestamp error of 0 DTU for
  every frame (min/max/mean/absolute mean all 0 DTU).
- SPIM3/32 MHz is therefore validated for Phase 2 use on the two current boards.

## Preamble comparison: 128 symbols on SPIM3

- The application PHY configuration was changed from `DWT_PLEN_64` to
  `DWT_PLEN_128`, with the matching SFD timeout of 129 symbols. SPIM3 remains
  at 32 MHz and STS remains disabled (SP0).
- Controlled runs flashed the receiver first, then the transmitter, to avoid
  counting startup while the peer was being reprogrammed:

| Direction | Received / sent | Reception rate | CIR reads | RX errors |
|---|---:|---:|---:|---:|
| `760223921` to `760200606` | 1000 / 1000 | 100.00% | 1000 / 1000 | 0 |
| `760200606` to `760223921` | 999 / 1000 | 99.90% | 999 / 999 | 1 |

- Both scheduled-TX runs completed 1000/1000 TX IRQs with no start failures,
  IRQ timeouts, or timestamp error (0 DTU min/max/mean/absolute mean).
- CIR shape remained valid: relative peaks were 17-18 taps and every received
  frame produced a 64-tap window. The 128-symbol profile is retained for the
  next Phase 2 experiments; 64 remains a documented lower-airtime fallback.

## Gate 1 timing measurement: full-frame EXT-PHR

- Date/profile: 2026-07-26, `nrf52833dk/nrf52833`, SPIM3 at 32 MHz, channel 9,
  PRF 64 MHz, PLEN 128, 6.8 Mb/s, STS off.
- Transmitter: J-Link `760223921`, scheduled-TX bring-up image with 1023 total
  frame bytes including FCS, EXT PHR, 20 ms period.
- Receiver: J-Link `760197419`, sensing-RX bring-up image with callback GPIO and
  cycle-counter instrumentation.
- The first run exposed that the application remained at the driver’s 2 MHz
  initialization SPI rate. The application now calls the driver platform’s
  fast-rate transition after initialization; the corrected run reported:

| CIR taps | RX callback max | `dwt_writetxdata` max | Result |
|---:|---:|---:|---|
| 64 | 1983 us | 366 us | 1023-byte EXT-PHR frames and CIR reads observed |
| 128 | 2868 us | 366 us | 1023-byte EXT-PHR frames and CIR reads observed |

- The corrected run also verified scheduled TX completion on the transmitter;
  the observed `error_dtu=0` remained unchanged.
- These are callback maxima from the firmware counter, not GPIO pulse-width
  captures. `report_assembly_us` and the independent diagnostic transaction
  accounting remain pending, so the configuration model has not yet been
  changed from its estimates.
- A follow-up 64-tap run added operation breakdown counters. Over at least
  2600 received frames with no RX errors, the maxima were 1953 us for the full
  callback, 213 us for `dwt_readdiagnostics_acc()`, and 915 us for the 64-tap
  `dwt_readcir_48b()` transaction. The transmitter remained at 366 us maximum
  for `dwt_writetxdata` with `error_dtu=0`.

### Production report assembly measurement

- Date/profile: 2026-07-26, `nrf52833dk/nrf52833`, 32 MHz SPIM3, channel 9,
  PRF 64 MHz, PLEN 128, 6.8 Mb/s, EXT PHR and extended frame filtering,
  N=2, M=1, 64 CIR taps, reporting node 1.
- Board `760223921` ran the verified continuous 1023-byte Gate 3 transmitter.
  Board `760197419` ran the beacon-enabled sensing receiver. Both J-Link
  downloads completed with verification `O.K.`.
- For each valid RX callback, firmware converted the actual CIR to normative
  i16 I/Q samples, encoded the complete CRC-bearing 296-byte subreport, packed
  the rotated pooled report, encoded the 31-byte production frame header, and
  copied the balanced payload into the exact 327-byte TX-buffer image. The
  measured assembly interval excludes subreport encoding/CRC and TX-buffer
  writing, matching the model's `report_assembly_us` boundary.
- The Gate 3 source frame is not a beacon frame, so fields unavailable from it
  (`observed_tx_timestamp` and the candidate frame's `tx_timestamp`) were zero
  during this timing run. Actual RX timestamp, CFO, CIA diagnostics, CIR window,
  node identity, sequence-derived round, CRC, pool ordering, and frame bytes
  used the production serializers.
- The first 4162-frame run measured 549 us for assembly and revealed that
  `heimdall_report_pack()` cleared the entire 3864-byte maximum report before
  overwriting the valid N=2 payload. Removing that unnecessary clear preserved
  metadata initialization and the exact emitted bytes.
- The optimized run covered 4217 valid frames. Maxima read directly from the
  exact ELF's RAM symbols were: full callback 2593 us, diagnostics 213 us, CIR
  read 915 us, subreport encode 518 us, and pooled-report/frame assembly 91 us.
  Assembly failures were zero; the radio accumulated 7 RX errors.
- The 518 us encoder maximum showed that the bitwise CRC implementation was much
  slower than the model's provisional 8 B/us assumption. It was replaced by
  Zephyr's reflected IEEE CRC32 implementation, which uses a 16-entry nibble
  table. A startup benchmark measured CRC alone over the exact 292-byte
  protected span without adding duplicate work to the RX callback.
- The CRC-optimized run covered 8898 valid frames. Stable maxima were: CRC alone
  152 us, complete subreport encode 183 us, pooled-report/frame assembly 122 us,
  full callback 2227 us, diagnostics 213 us, and CIR read 915 us. Assembly
  failures were zero; the radio accumulated 6 RX errors.
- Result: `crc32_bytes_per_us=1.92` and `report_assembly_us=122` are now measured
  model inputs. The N=2 assembly floor is 1600 us and remains below the
  configured 10 ms slot.
- J-Link VCOM output was framing garbage during this run, including with the
  previously verified receiver image. Counters were therefore read by halting
  the receiver briefly and reading named RAM symbols through J-Link, then
  resuming it. Board 2 was restored to the verified Gate 3 receiver afterward.

## Gate 3: EXT-PHR with hardware filtering

- Date/profile: 2026-07-26, `nrf52833dk/nrf52833`, SPIM3 at 32 MHz, channel 9,
  PRF 64 MHz, PLEN 128, 6.8 Mb/s, STS off, 1023-byte frames.
- The TX image enabled `DWT_PHRMODE_EXT`, scheduled TX, and hardware data
  filtering with `DWT_FF_EXTEND_EN`. The RX image enabled the same EXT-PHR
  mode, 64-tap CIR measurement, and the extended hardware filter.
- TX board `760223921` and RX board `760197419` were flashed through the UNO Q
  J-Link host. Both downloads completed with J-Link program verification
  `O.K.`.
- Board-to-board run: at least 5200 scheduled TX IRQ completions and at least
  5200 valid RX frames with 5200 CIR reads. TX reported `error_dtu=0`; RX
  callback maximum remained 1953 us and `dwt_writetxdata` maximum remained
  366 us. RX accumulated 38 errors during the run.
- Result: PASS. `DWT_PHRMODE_EXT` and `DWT_FF_EXTEND_EN` work together for the
  full-size frame profile. The RX error count is retained for later link-budget
  characterization and does not invalidate this gate.

## USB CDC bulk throughput

- Date/profile: 2026-07-26, `nrf52833dk/nrf52833`, native J20 USB CDC,
  interrupt-driven `uart_fifo_fill()`, 32-entry non-blocking application queue.
  The radio was not initialized or exercised; the build retained the 32 MHz
  SPIM3 overlay and channel 9 PHY defaults for consistency with the gateway
  profile.
- Gateway board `760223921` was flashed and captured by the UNO Q through its
  stable USB hub. J-Link download and verification completed `O.K.`.
- The TX callback now disables its UART TX interrupt when the application queue
  is empty. Leaving it enabled continuously resubmitted the callback work while
  FIFO space remained and could starve CDC transfer work.
- Control run: 1,000 synthetic 285-byte `CIR2` records at 20,000 us spacing
  produced exactly 285,000 bytes with sequences 0 through 999 ordered.
- Stress run: 10,000 records at 1,000 us spacing produced exactly 2,850,000
  bytes with sequences 0 through 9999 ordered.
- Required-rate run: 20,000 records at 600 us spacing produced exactly
  5,700,000 bytes with sequences 0 through 19999 ordered. The 475 kB/s offered
  load exceeds the model's current 433 kB/s maximum gateway load. A 500 us spacing
  overloaded the queue, so the absolute transport ceiling remains between the
  verified 475 kB/s rate and the failed 570 kB/s offered load.
- Result: PASS for the protocol budget. The UNO Q reader must configure the ACM
  port for raw input; plain `cat` in canonical mode transformed and buffered the
  binary stream, producing repeatable but invalid 8,547-byte captures.

## N=2 Heimdall runtime beacon

- Date/profile: 2026-07-26, `nrf52833dk/nrf52833`, 32 MHz SPIM3, channel 9,
  PRF 64 MHz, PLEN 128, PAC 8, SFD type 1, 6.8 Mb/s, EXT PHR, STS off,
  hardware data/extended filtering, N=2, M=1, 10,000 us slots, 329-byte frames,
  64 CIR taps, and configuration hash `0x3c50`.
- Physical binding: node 0/gateway is J-Link `760223921`, FICR
  `75561606:12A31510`; node 1 is J-Link `760197419`, FICR
  `71414197:EAD43288`. Both images used TX/RX antenna delay 16385 DTU. This is
  an uncalibrated bring-up value and the run does not validate ranging accuracy.
- Final node 0 image: SHA-256
  `40F3094092F75D878A49D1D809FD5733BF37BC972DF6ABE22BC663132795B680`,
  41,644 B flash, 15,552 B RAM. Final node 1 image: SHA-256
  `B6189735B2D5A28A728A5DA00C6EC1A08D52AB09ED480D8017E369D267694E4B`,
  41,420 B flash, 15,552 B RAM. Both J-Link downloads completed with program
  verification `O.K.`.
- The first node 0 bootstrap exposed that this driver's `dwt_readsystime()`
  returns four bytes representing system-time bits 8-39. Treating it like the
  five-byte RX/TX timestamp APIs incorporated an uninitialized high byte and
  scheduled the first TX about 1.96 seconds ahead. The runtime and scheduled-TX
  primitive now shift this four-byte value left by eight explicitly.
- Corrected first programmed-TX lead after frame preparation was 9.546 ms for
  node 0 and 7.251 ms for node 1. Both exceed the measured processing path and
  completed without a late-start or timeout recovery.
- First interval: both nodes reached 931 validated peer frames with zero
  validation rejects, late starts, timestamp errors, report failures, or TX
  timeout recoveries. Their last completed ownership was adjacent: node 1
  transmitted odd `k=1861`, followed by node 0 transmitting even `k=1862`.
- Final snapshots: node 0 had 3283 validated RX, 3283 CIR reads, 3285/3285 TX
  completions, `last_rx_k=6567`, and `last_tx_k=6568`. Node 1 had 3286 validated
  RX, 3286 CIR reads, 3286 TX attempts, 3285 completions with one TX normally
  pending at the snapshot, `last_rx_k=6570`, and `last_tx_k=6571`. All reject,
  identity, delayed-start, timestamp-error, assembly-failure, and TX-timeout
  counters remained zero. Node 0 accumulated one RX error and one watchdog TX;
  node 1 accumulated no RX errors. Callback maxima were 2441 us and 2471 us.
- Result: PASS. Bootstrap, continuous RX, validated CIR/report assembly,
  alternating delayed TX, monotonic round ownership, subreport CRC relay, and
  master watchdog recovery operated continuously on the two physical boards.

## Combined runtime and USB CDC v1

- Date/profile: 2026-07-27, node 0/gateway J-Link `760223921`, FICR
  `75561606:12A31510`, `nrf52833dk/nrf52833`, 32 MHz SPIM3 plus native J20 USB,
  channel 9, PRF 64 MHz, PLEN 128, PAC 8, SFD type 1, 6.8 Mb/s, EXT PHR,
  hardware filtering, N=2, M=1, 10,000 us slots, 329-byte frames, and 64 CIR
  taps. Node 1 remained on the compatible fixed-node runtime.
- Gateway image SHA-256:
  `A6A21FACBC14B5E9B09D5E747EECEE004CA4CBA5F681C00CEE61C889E2C92357`.
  J-Link program and verification completed `O.K.`. Image use was 78,488 B
  flash and 39,480 B RAM. Antenna delays remained the uncalibrated 16385 DTU
  bring-up values.
- Native USB enumerated at the stable link
  `/dev/serial/by-id/usb-Open_UWB_Heimdall_Gateway_7556160612A31510-if00`.
  A ten-second raw capture contained 381,165 bytes and 2,065 complete records:
  10 `HELLO`, 20 `HEARTBEAT`, 508 `RADIO_FRAME`, 509 `LOCAL_OBS`, 509
  `CYCLE_SUMMARY`, and 509 confirmed `TX_RECORD`. Capture termination left 128
  bytes of one trailing record buffered, as expected for arbitrary stream stop.
- Host validation found zero outer CRC failures, framing errors, duplicates,
  unknown types, configuration/validation rejects, subreport CRC failures, FCS
  errors, or filter rejects. All 508 peer frames carried odd `k`; all 509 gateway
  TX records carried even `k`. The decoder correctly skipped 19 data records
  received before the first periodic `HELLO`; all 499 post-`HELLO` local
  observations decoded a valid CIR and independent subreport CRC.
- The gateway ran detached before capture, deliberately exercising backpressure.
  The parser measured a 3,598-record sequence gap and the first summary after
  attachment reported exactly 3,598 producer drops. Every summary satisfied
  `k_cycle_start = N * cycle_index`; the final 100 summaries each reported 1/1
  frames, no peer misses, no USB drops, and an RX callback maximum of 2868 us.
  The connected stream therefore drained faster than
  its 37,350 B/s modeled production rate without affecting the 10 ms slots.
- Gateway RAM counters after capture showed 13,287 validated RX and CIR reads,
  13,291 TX attempts and completions, zero late
  starts, timestamp errors, identity/configuration inhibitions, or TX timeout
  recoveries, and adjacent `last_rx_k=26579`, `last_tx_k=26580`. Three successful
  watchdog transmissions accumulated without loss of schedule.
- Replaying the real capture as 4096-byte chunks and as repeating 1, 7, 64, and
  1023-byte chunks produced the same 2,065 byte-identical records. Result: PASS
  for simultaneous radio timing, USB CDC v1 framing, bounded backpressure,
  capture, decode, and deterministic replay.

## Gate H4: N=3 Heimdall runtime

- Date/profile: 2026-07-26, `nrf52833dk/nrf52833`, 32 MHz SPIM3, channel 9,
  PRF 64 MHz, PLEN 128, PAC 8, SFD type 1, 6.8 Mb/s, EXT PHR, hardware
  filtering, N=3, M=1, exactly three occupied 10,000 us superslots, 625-byte
  frames, 64 CIR taps, and configuration hash `0xC8CF`.
- Physical binding: node 0/gateway is J-Link `760223921`, FICR
  `75561606:12A31510`; node 1 is `760197419`, FICR `71414197:EAD43288`; node 2
  is physical label 3 / `760197416`, FICR `6402F3A7:947F4A25`. All images use
  uncalibrated TX/RX antenna delay 16385 DTU, so this gate does not validate
  range accuracy.
- Final images: node 0 SHA-256
  `B815BA2836FEB50173663731B172B6195A56C0336EDF300A0EC6895389EA9879`,
  79,208 B flash / 51,640 B RAM; node 1
  `5CB0339934D299EF060827530A6549D239CFBE0613BCFA4C643C918F58178E87`,
  42,332 B flash / 18,304 B RAM; node 2
  `5D19DE9658433D11A293A0011B7068B75B0C534D005F61C1667F6F75F06953B7`,
  42,316 B flash / 18,304 B RAM. All three J-Link downloads verified `O.K.`.
- A steady ten-second capture contained 677 `RADIO_FRAME`, 676 `LOCAL_OBS`,
  340 `CYCLE_SUMMARY`, and 338 confirmed `TX_RECORD` records. The final 100
  summaries were all 2/2 with zero peer misses and USB drops. Ownership passed
  `k % 3 == source_node_id` for RX and `k % 3 == 0` for gateway TX. Outer CRC,
  framing, FCS, filter, validation, and relayed-subreport CRC failures were zero.
  The summary callback maximum was 2777 us; the later gateway RAM maximum was
  3540 us, still below the conservative 10 ms slot.
- Canonical processing of the capture produced all six directed pairs. The H4
  ingest interval produced pair counts `(0,1)=986`, `(0,2)=984`, `(1,0)=984`,
  `(1,2)=983`, `(2,0)=986`, `(2,1)=986`, consistent with the modeled 33.333 Hz
  per-link rate. Boundary-truncated CIR reports remain variable-length and
  self-describing as required by beacon v1; host reassembly accepts up to the
  configured 64-tap maximum.
- Reader detachment filled the non-blocking gateway queue without disturbing
  radio timing. On attachment, one 15,447-record sequence gap exactly matched
  the first summary's 15,447 producer drops; all 991 captured summaries were
  2/2 and subsequent drops were zero.
- Node 2 was held halted for a ten-second capture while node 1 remained active.
  The gateway continued with 339 confirmed TX records and 338 summaries; node 2
  was independently marked missing. After release, the next capture returned
  to 2/2 and zero misses for the final 100 summaries, proving recovery from the
  N=3 missing-intermediate stall case.
- Node 1 was then held halted to exercise node 2's immediate-predecessor loss.
  Node 2 remained active with 327 local gateway observations and 327 relayed
  reports during the capture; gateway TX continued and summaries independently
  marked one peer missing. Releasing node 1 again restored 2/2 and zero misses
  for the final 100 summaries.
- Final RAM snapshots: gateway 31,255/31,255 validated RX/CIR reads, 15,982 TX
  attempts and 15,981 completions with one normal pending TX, zero late starts,
  timestamp errors, validation rejects, identity/configuration inhibitions, or
  timeout recoveries, and 680 watchdog TX. Node 1 had 58,287 validated RX/CIR
  reads, 29,515 attempts / 29,514 completions, zero late starts, timestamp
  errors, validation rejects, inhibitions, or timeout recoveries, and 71
  watchdog TX. Node 2 had 54,431 validated RX/CIR reads, 27,241 attempts / 27,239
  completions with one late start and one normal pending TX, zero timestamp
  errors, validation rejects, inhibitions, or timeout recoveries, and 17
  watchdog TX. Node callback maxima were 2990 us and 3021 us.
- H4 ingest/replay acceptance: PASS. Eight 256 KiB rotating segments held 6,032
  records and 5,909 observations. Live and replay SQLite integrity checks were
  `ok`; all segment sizes and hashes matched. Raw digests both equal
  `e584d418fac65d683529a5aa5891e6afb3c79752d17a8e14d1e5f7094e5018db`;
  observation digests both equal
  `08cc36484497d88bd605ef652ccab30777edd07074d0ebc4a47f72e9b874aa20`.
  The 19 rejected records were all expected pre-HELLO data.
- Result: PASS for Gate H4 schedule, two-peer report retention, six directed
  observations, bounded processing, source-specific misses, watchdog recovery,
  USB backpressure, and replay-equivalent ingestion. At the once-per-u32-wrap
  boundary, N=3 necessarily repeats owner 0 because `2^32` is not divisible by
  3; wrap-safe next-owner selection is tested, while cycle-summary semantics at
  that approximately 545-year boundary remain a protocol clarification item.

## N=5 / M=2 rate qualification

- Final PHY and framing remain channel 9, PRF 64 MHz, preamble 128, PAC 8,
  SFD type 1, 6.8 Mb/s, EXT PHR, 625-byte frames, and 64 CIR taps. N=5 derives
  M=2 with 592 payload bytes per frame and 1,184 pooled-report bytes.
- The fastest reliable profile uses 3,500 us slots, 7,000 us superslots,
  35,000 us cycles, 28.571 Hz per directed link, modeled 187,200 B/s gateway
  USB, and config hash `0x8885`. Nodes 0-2 were active; nodes 3-4 were
  deliberately absent without schedule compression.
- Firmware now chains `m=0` and `m=1`, accepts same-`k` increasing fragments,
  derives delayed phase from both fields, captures CIR only on `m=0`, retains
  the pooled report across both TX frames, and emits m-aware TX records. Report
  prepacking and corrected RX-timestamp-to-work-delay accounting preserve
  missing-node fallback deadlines.
- The gateway callback now snapshots into a 16-event slab and rearms RX after
  accumulator-sensitive reads. One export thread serializes all USB record
  types, eliminating the observed multi-producer sequence inversion. Relayed
  fragment reassembly, padding/start-count checks, and subreport CRC validation
  run on the UNO Q host.
- Final identity-bound images flashed with J-Link V9.62 and verified `O.K.`:
  node 0 `436CDAC15FD2152EC3AFD7E8BE813D3BDA1D159CB6EA7593B89316CA98B78FA1`
  (79,908 B flash / 69,624 B RAM), node 1
  `AE8EFB80E564720C3844DD927497157970C12F736755CDAC3A8E85580CC92FC7`
  (42,444 B / 18,112 B), and node 2
  `DDE8D935F7916C59706AD1E8054A5D99C976DB44D344FB5076153CC39B5D6175`
  (42,428 B / 18,112 B).
- The 30-second rate-qualification capture contained 861 summaries, 3,442 radio frames,
  1,721 local observations, 1,726 confirmed TX records, and 5,156 canonical
  observations. USB ordering, CRC, framing, ownership, cycle binding, firmware
  error, FCS, filter, validation, and tail-drop checks passed. Pair counts were
  855-861. Two isolated `m>0` losses and one capture-boundary cycle occurred;
  the last 100 summaries reported exactly the expected 200 node 3/4 misses.
  Gateway callback maximum was 1,892 us.
- Post-review binaries additionally bind timeout work to a TX generation, reject
  stale TXFRS callbacks after timeout recovery, atomically claim USB-drop counts,
  and enqueue each RX record before the summary that counts it. A final
  five-second smoke capture covered 146 summaries with all TX confirmed, valid
  ownership, zero CRC/framing/validation failures, and exactly the expected
  node 3/4 misses in the last 100 summaries.
- Board 5 (`760223924`) was identified as FICR `454D4801:5B214CD7`, built for
  node 4 with the same N=5/M=2 profile, and flashed with verification `O.K.`.
  Its image is 42,444 B flash / 18,112 B RAM, SHA-256
  `50B84078564EC88A8FCD9C67647136E3874473AE48CB7F0A7C2EC68FFA29527F`.
  A 15-second capture had zero node 4 misses in the last 100 summaries and no
  CRC, framing, ownership, or validation errors. Node 2 missed 19/100 cycles
  after it was moved to wall power, so the four-active-node topology is not yet
  qualified.
- Board 4's J-Link (`760200606`) entered USB product `1366:0101` after an
  automatic onboard-firmware update timed out.
- Reconnecting board 4 completed its J-Link update. Its FICR is
  `86B7AC3A:20F3AB47`, and it is now roster-bound and flashed as node 3.
- Full-roster 3,500 us export initially saturated both application slabs near
  150 kB/s. A dedicated CDC workqueue and 8 KiB TX FIFO reduced contention; a
  256-entry CRC32 table and `CONFIG_SPEED_OPTIMIZATIONS=y` raised sustained
  gateway export to the required 187,200 B/s. The final gateway uses 97,820 B
  flash / 79,160 B RAM and has SHA-256
  `FC34D01ED2594084D0698D33C6E046442856A03808772F174D4F956A9A06E1B8`.
- D9-D12 toggle for validated `m=0` reception from the four peer IDs in sorted
  order with the local ID omitted. D13 is not MCU-controlled. Final node 1-4
  image hashes are `53E83586EE4FDE2FF181526D5FA2383E65F28D9D0C0CD21FE83983ABD8B63C56`,
  `9C109FC6DA7018A80AE1703938CEF43A0026C40E7ABC93696449DB8740E2275B`,
  `C7D7A2ACBB9A19425B4CF8CD78F81203B251913498EA75E9CFF6D5E897F8D31E`,
  and `AA61156678F80E9239F1F1E4552391B1513ED406B1D6AAE5E4727C89AD2C3A16`.
- The final 60-second LED-enabled capture contained 11,221,153 bytes and 1,717
  summaries. USB drops were zero after the first summary acknowledged backlog
  accumulated before the reader attached. All TX was confirmed, callback
  maximum was 2,105 us, and CRC/framing/ownership/validation checks passed.
  Radio delivery was 13,690/13,736 frames (99.665%): 1,671 cycles were 8/8 and
  46 were 7/8, with loss concentrated on node 2 in the current placement.
  Capture SHA-256 is
  `DD913D7F7CB6B773AA5FC65B12B2527ADADF0A1C64E5145CFD30599F809D34C4`.
- The reviewed gateway explicitly initializes the generated CRC table before
  enabling RX or queueing USB records. Its exact final image passed a 30-second
  confirmation with 5,625,173 bytes, 859 summaries, zero tail USB drops, all TX
  confirmed, and no CRC/framing/validation errors. Radio delivery was
  6,853/6,872 frames (99.724%). Capture SHA-256 is
  `D97BCC529BFC629680D6690D086D5097634A0CF334857F0DA574B2A6A2C5A652`.
- A 3,000 us / 33.333 Hz candidate was rejected despite zero delayed-start
  errors: 11 of 1,003 summaries missed one `m>0` frame and four summaries had
  an active-node `m=0` miss. The approximately 173 us measured margin is not a
  reliable operating point.
