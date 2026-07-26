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
- Result: `report_assembly_us=91` replaces the provisional 20 us input. The N=2
  assembly floor rises from 1300 us to 1400 us and remains below the configured
  10 ms slot.
- The 518 us encoder maximum also shows that the current bitwise CRC plus
  serialization path is much slower than the model's provisional 8 B/us CRC
  throughput. That budget remains explicitly uncalibrated; it must be measured
  separately or the CRC implementation optimized before the slot floor is
  treated as final.
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
  load exceeds the model's current 454 kB/s maximum gateway load. A 500 us spacing
  overloaded the queue, so the absolute transport ceiling remains between the
  verified 475 kB/s rate and the failed 570 kB/s offered load.
- Result: PASS for the protocol budget. The UNO Q reader must configure the ACM
  port for raw input; plain `cat` in canonical mode transformed and buffered the
  binary stream, producing repeatable but invalid 8,547-byte captures.
