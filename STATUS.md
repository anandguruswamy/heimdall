# Heimdall Status

## Project state

The N=5/M=2 Heimdall radio runtime, thin gateway USB export, and replayable UNO Q
ingest are hardware-validated with all five nodes active at 28.571 Hz and 64 CIR
taps. Full-roster USB export is lossless after startup; the current physical
placement still has low-rate radio loss concentrated on node 2. Antenna
calibration remains before timestamps can be treated as accurate ranges.

## Starting point

- Open DW3000/Zephyr Phase 1 bring-up is proven on two DWM3001CDKs.
- SPIM3 at 32 MHz passed DEV_ID, bidirectional traffic, CIR reads, and
  scheduled-TX tests on both boards.
- Native nRF52833 USB CDC is proven and suitable for the gateway path.
- Existing captures and live-CIR tools are available under `firmware/tools`.

## Current host-tooling state

- Native ARM64 Windows setup is working with the Windows x86-64 Zephyr SDK
  0.17.4 under emulation.
- Zephyr, the pinned DW3000 driver, CMSIS, Nordic HAL, and SEGGER modules are
  fetched in the `firmware/` west workspace.
- CMake 3.31.10, Ninja, DTC, west 1.5.0, and the `arm-zephyr-eabi` compiler
  are installed and detected.
- The 8 MHz rollback and 32 MHz SPIM3 profiles both build successfully with
  USB CDC enabled. The build outputs are under `firmware/build-radio/` and
  `firmware/build-radio-spim3/`.
- The baseline fixes included fetching the missing DW3000 west module,
  including the USB DeviceTree overlay, and deleting the inactive SPI1 radio
  node before defining the SPIM3 radio node.
- Builds still emit non-fatal warnings for an empty console library, unused
  role functions, and a deprecated SPI driver macro.
- The current board target is `nrf52833dk/nrf52833`; underscore-form names are
  retained only for overlay/devicetree filenames.

## Beacon implementation progress

- The west workspace was restored from `firmware/radio/west.yml`; the live
  driver is pinned at `6208d99f933872bf024a653b0c9e8bef92349162`.
- Driver-source checks used the DW3000 implementation, not the DW3720 variant:
  EXT PHR is enum value `DWT_PHRMODE_EXT = 0x1`
  (`firmware/modules/lib/zephyr-dw3000-decadriver/dwt_uwb_driver/deca_device_api.h:169-174`);
  TX writes above offset 127 use the indirect path and the TX buffer limit is
  1024 bytes (`.../dw3000/dw3000_device.c:2218-2241`); delayed TX writes the
  high 32 system-time bits with bit 0 ignored (`.../dw3000/dw3000_device.c:4942-4958`);
  the CIR path reads `ACC_MEM_ID` in 16-sample chunks
  (`.../dw3000/dw3000_device.c:2483-2526`); and the GPIO IRQ submits work
  before calling `dwt_isr()` (`.../platform/dw3000_hw.c:66-76`).
- Added a GPIO/cycle-counter RX callback measurement mode with configurable
  64/128 taps and a 1023-byte receive buffer. Corrected full-frame EXT-PHR
  measurements at 32 MHz SPIM3 reported 1983 us maximum at 64 taps, 2868 us
  maximum at 128 taps, and 366 us maximum for `dwt_writetxdata`; the complete
  run is recorded in `firmware/radio/BRINGUP-NOTES.md`.
- The first full-frame EXT-PHR run exposed that the application remained at the
  driver's 2 MHz initialization SPI rate. The live compatibility path requires
  SPI below 7 MHz during `dwt_initialise()` and exposes the fast-rate transition
  separately (`firmware/modules/lib/zephyr-dw3000-decadriver/platform/deca_compat.c:410-431`,
  `.../platform/dw3000_spi.c:44-49,80-87`). The application now calls
  `dw3000_spi_speed_fast()` immediately after initialization; the timing result
  will be re-measured after this correction.
- Added initial frame-header/subreport serialization, CRC32, and schedule
  arithmetic modules under `CONFIG_HEIMDALL_BEACON`. The beacon-enabled SPIM3
  build passes and the existing 51 Python model tests pass.

## Hardware validation

- Board 1 (`760223921`) was flashed successfully through the UNO Q using its
  native Linux ARM64 J-Link tools and the J9 connection.
- Image: 8 MHz rollback profile, USB CDC-enabled `zephyr.hex`.
- J-Link reported successful flash download and verification (`O.K.`) for the
  nRF52833 target. D9 is blinking green, confirming the firmware reaches its
  heartbeat loop after radio initialization.
- The 32 MHz SPIM3 image was then flashed and verified on the same board; D9
  continued blinking green after the switch.
- Board 2 (`760197419`) was flashed with the 32 MHz SPIM3 image through the
  UNO Q and also shows the green D9 heartbeat.
- Board 1 (`760223921`) was flashed with the scheduled-TX role image from
  `firmware/build-radio-tx/`; J-Link reported successful flash download and
  verification (`O.K.`).
- Board 2 (`760197419`) was flashed with the sensing-RX role image from
  `firmware/build-radio-rx/`; J-Link reported successful flash download and
  verification (`O.K.`).
- Both role images use the nRF52833 target, 32 MHz SPIM3 radio overlay, and
  USB CDC configuration.
- The previously proven continuous `ROLE_TX`/`ROLE_RX` pair was rebuilt with
  the 8 MHz rollback overlay and J-Link VCOM console enabled. Board 1
  (`760223921`) reached TX IRQ 500/500 with zero start failures or timeouts;
  board 2 (`760197419`) received more than 400 frames with three RX errors
  during the capture. Both reported DEV_ID `0xDECA0302`, channel 9, 6.8 Mbps,
  and successful radio initialization. This confirms the board wiring and
  PHY path; the regression is in the newer scheduled-TX/sensing-RX role pair
  or its USB-only runtime path.
- The same continuous `ROLE_TX`/`ROLE_RX` test was repeated with the 32 MHz
  SPIM3 overlay. Board 1 (`760223921`) reached TX IRQ 500/500 with zero start
  failures or timeouts; board 2 (`760197419`) received more than 400 frames
  with zero RX errors during the capture. Both reported successful 32 MHz
  radio initialization.
- The current scheduled/sensing pair was then tested with J-Link VCOM console
  enabled at 32 MHz SPIM3. Board 1 (`760223921`) completed at least 800
  scheduled TX IRQs with zero reported timing error; board 2 (`760197419`)
  completed at least 700 sensing RX frames and CIR reads with zero RX errors.
  Scheduled radio operation is therefore verified; native USB CDC export is
  the remaining runtime-path issue to resolve before beacon framing.
- Native USB CDC export was then verified at 32 MHz SPIM3. The missing
  configuration was `CONFIG_PHASE1_CIR_DUMP=y`; without it, the sensing RX
  callback never queued CDC records. With it enabled in `radio/app/usb.conf`,
  the UNO Q captured 16,384 bytes containing 59 `CIR2` records from board 2.

- The beacon protocol was then designed and specified. No firmware behaviour
  changed: `CONFIG_HEIMDALL_BEACON` is added but defaults to `n`, and the new
  CMake step only runs when it is selected. No board was flashed.
  - `contracts/beacon-v1.md` is the normative wire format and behaviour,
    superseding `beacon-v0.md`.
  - `docs/protocol-decisions.md` records all 30 design decisions, the
    alternatives rejected, and the defects found in the original sketch.
  - `docs/beacon-protocol-explained.md` explains the scheme from background.
  - `tools/config/heimdall_config.py` is the reference sizing model. It derives
    `M`, frame sizes, airtime, slot floor, cycle, rate, and `config_hash`, and
    verifies a configuration against its declared values.
  - `firmware/radio/app/cmake/heimdall_config.cmake` fails the configure stage
    if a configuration's declared values disagree with the model, and generates
    `heimdall_beacon_config.h`.
  - `contracts/usb-cdc-v1.md` defines the gateway-to-host stream: 16 B outer
    framing plus seven record types. Its overheads feed the throughput budget
    that gates configuration export, so they are normative rather than
    descriptive. Supersedes `usb-cdc-v0.md`.
  - `tests/test_beacon_config.py` covers the model with 51 tests, all passing.
- An external review of the plan prompted two model corrections, both now
  covered by tests:
  - **Report-assembly constraint.** A node's report must contain its
    measurement of the peer that transmitted immediately before it, so the TX
    buffer cannot be assembled in advance. The full read-out, CRC, assembly, and
    TX-write chain must fit `M * T_slot - airtime`. The slot floor is now the
    larger of a reception constraint and an assembly constraint. Only `M = 1`
    configurations are affected: N=2 floor 1.1 to 1.3 ms, N=3 1.6 to 1.9 ms,
    N=4 2.0 to 2.4 ms. The six-board target is unchanged.
  - **Subreport CRC32 was missing from the RX budget.** It runs in the observing
    node's callback and costs roughly 37 us for a 296 B subreport, which the
    fixed-overhead allowance did not cover.
- An earlier self-caught error is also recorded: the nominal 6.8 Mb/s is already
  net of Reed-Solomon coding, so airtime estimates go wrong by omitting the
  ~160 us of SHR and PHR, not by double-counting parity.
- Gate 3 passed board-to-board with `DWT_PHRMODE_EXT` and
  `DWT_FF_EXTEND_EN`: board `760223921` transmitted at least 5200 scheduled
  frames and board `760197419` received at least 5200 valid frames with 5200
  CIR reads. TX `error_dtu=0`; RX accumulated 38 errors; callback and TX-write
  maxima were 1953 us and 366 us respectively. Details are in
  `firmware/radio/BRINGUP-NOTES.md`.
- The assembly measurement follow-up now reports independent 64-tap maxima of
  213 us for `dwt_readdiagnostics_acc()` and 915 us for `dwt_readcir_48b()`;
  full callback maximum remained 1953 us over at least 2600 frames with no RX
  errors.
- Real production-byte report assembly is now instrumented in the sensing RX
  callback. The report packer no longer clears an unused 3864-byte maximum
  buffer, and the subreport encoder now uses Zephyr's nibble-table IEEE CRC32.
  Over 8898 valid 1023-byte Gate 3 receptions, maxima were 152 us for CRC alone,
  183 us for complete subreport encoding, 122 us for rotated pool plus frame
  assembly, and 2227 us for the full callback; assembly failures were zero and
  RX accumulated 6 errors. The example configuration now uses the measured
  `crc32_bytes_per_us=1.92` and `report_assembly_us=122`, giving a 1600 us N=2
  assembly floor.
- Native USB CDC bulk transmission is verified on board `760223921` through
  J20 and the UNO Q. The interrupt-driven FIFO path disables TX IRQ when its
  application queue drains, preventing the CDC callback work item from
  starving the transfer work. A radio-free profile sent all 20,000 synthetic
  285-byte `CIR2` records at 600 us spacing: the raw host capture was exactly
  5,700,000 bytes with ordered sequences 0 through 19999. This is a 475 kB/s
  offered load, above the model's current 433 kB/s worst case. A 500 us / 570 kB/s
  profile saturated the queue, so 475 kB/s is the current verified rate rather
  than a measured absolute ceiling. Linux captures must put the ACM TTY in raw
  mode; earlier 8,547-byte `cat` results were canonical-line-discipline
  artifacts and are invalid throughput measurements.
- Added the first radio-only Heimdall runtime gate for the verified N=2, M=1,
  10 ms-slot profile. It listens before master bootstrap, validates schedule and
  configuration in layers, adopts phase independently of CIR/relay quality,
  captures bounded 64-tap observations, assembles the next report, and uses
  delayed TX for alternating ownership. It also implements three-frame
  configuration mismatch inhibition/recovery, stale-epoch recovery, liveness,
  identity-collision shutdown, and TX-completion timeout recovery.
- Runtime images are bound at build time to a node ID, the board's two FICR
  `DEVICEID` words, and per-board TX/RX antenna delays. Zero or mismatched values
  prevent startup. The global `heimdall_runtime_counters` symbol is retained for
  J-Link RAM inspection because J-Link VCOM is currently unreliable.
- The physical N=2 roster is recorded in `deployment/node-roster.lab.yaml`.
  Node 0/gateway is J-Link `760223921`, FICR `75561606:12A31510`; node 1 is
  J-Link `760197419`, FICR `71414197:EAD43288`. Both currently use the explicit
  uncalibrated bring-up value 16385 DTU for TX and RX antenna delay, so this run
  does not validate range accuracy.
- N=2 radio runtime: PASS. Both board-bound images were flashed and verified
  through the UNO Q. Over the final interval, node 0 reached 3283 validated RX,
  3285/3285 completed TX, and `last_tx_k=6568`; node 1 reached 3286 validated
  RX, 3285 completed TX with one normal pending TX, and `last_tx_k=6571` in a
  slightly later snapshot. All frame, configuration, schedule, stale-frame,
  subreport, identity, delayed-start, timestamp-error, assembly-failure, and TX
  timeout counters remained zero. Node 0 recorded one RX error and one watchdog
  transmission without losing schedule; node 1 recorded zero RX errors.
  Callback maxima were 2441 us and 2471 us. The runtime occupies 41,644 B flash
  / 15,552 B RAM for node 0 and 41,420 B flash / 15,552 B RAM for node 1. All
  52 host tests pass.
- The first bootstrap attempt exposed a driver API trap: this DW3000 port's
  `dwt_readsystime()` returns four bytes containing timestamp bits 8-39, not a
  normal five-byte timestamp. Treating it as five bytes scheduled bootstrap
  about 1.96 seconds ahead. Runtime and scheduled-TX system-time conversion now
  restore the omitted low byte explicitly. Corrected first-TX preparation left
  9.546 ms on node 0 and 7.251 ms on node 1 before their programmed TX times.
- Gateway USB CDC v1 is implemented and hardware-validated on node 0. The
  gateway profile combines the 32 MHz SPIM3 radio and native J20 USB overlays,
  uses a 32-block non-blocking slab/FIFO with whole-record drop-newest behavior,
  allocates sequence numbers per attempted record, and exports `HELLO`,
  `HEARTBEAT`, `RADIO_FRAME`, `LOCAL_OBS`, `CYCLE_SUMMARY`, `ERROR`, and
  `TX_RECORD`. USB work occurs only after delayed TX is armed or RX is rearmed.
- The USB contract now explicitly binds outer framing to IEEE CRC-32 and makes
  producer drops sequence-visible. Its throughput model excludes the hardware
  FCS that `RADIO_FRAME` does not carry; the N=2 modeled stream is 747 B/cycle,
  37,350 B/s.
- Combined radio/USB runtime: PASS. Gateway image SHA-256 is
  `A6A21FACBC14B5E9B09D5E747EECEE004CA4CBA5F681C00CEE61C889E2C92357`,
  occupying 78,488 B flash and 39,480 B RAM. Native USB enumerated as
  `usb-Open_UWB_Heimdall_Gateway_7556160612A31510-if00`. A 10 s raw capture was
  381,165 B and contained 2,065 outer-CRC-valid records: 10 HELLO, 20 HEARTBEAT,
  508 RADIO_FRAME, 509 LOCAL_OBS, 509 CYCLE_SUMMARY, and 509 confirmed TX_RECORD.
  There were zero CRC/framing failures, unknown types, validation rejects,
  subreport CRC failures, FCS errors, or filter rejects. All peer frame `k`
  values were odd and all gateway TX `k` values were even. Replay produced the
  same 2,065 records under whole-buffer and fragmented input. All summaries
  satisfy `k_cycle_start = N * cycle_index`.
- The gateway intentionally ran without a host before capture, filling its
  queue. The stream sequence gap was 3,598 records and the first post-attach
  summary independently reported exactly 3,598 producer drops. The final 100
  cycles each reported 1/1 received frames, zero peer misses, zero queue drops,
  and a callback maximum of 2868 us. Gateway RAM counters later showed 13,287
  validated RX/CIR reads, 13,291 completed TX, zero delayed-start, timestamp, or
  timeout failures, and `last_rx_k=26579`, `last_tx_k=26580`. All 63 host tests
  pass.
- Gate H3 host implementation now archives the CDC stream before parsing into
  rotating, checksummed segments; tracks runs, reconnects, configuration epochs,
  outer-valid records, type-level rejections, health records, and canonical CIR
  observations transactionally in SQLite; and replays ordered segments through
  the same parser and canonical processor. Local gateway observations and
  arbitrary-`M` relayed pooled reports use one immutable observation shape.
  Rotation, fragmented input, corruption accounting, reconnect recovery,
  out-of-order `M=2` reassembly, and live/replay observation equivalence are
  covered by the host suite.
- Gate H3 UNO Q validation: PASS. The service ran on `chinny` against the stable
  gateway by-id path, rotated 256 KiB segments, survived a physical J20
  disconnect/reconnect as a new connection epoch, recovered nine catalog entries
  after an intentional abrupt termination, and then stopped cleanly via
  `SIGTERM`. The combined recovery run cataloged and verified 81 segments,
  113,563 raw records, and 55,708 canonical observations. Live and replay raw
  digests were both
  `c4ecf659ec66fbb79c04e7052d503ee13470289ebf11ba609a13438eb350a87c`;
  observation digests were both
  `a96154cd91eef4930897779e355a9136bbfa9e3d4f3c60ede43d097e83970ef8`.
- The final isolated acceptance capture verified the corrected clean-stop path:
  four segments, 5,237 records, and 2,532 observations; zero outer CRC,
  framing, duplicate/old, unknown-type, or trailing-byte failures; matching
  live/replay record digest
  `fa296ae4842dc01a7b7819aad52d3c86ac8652f98cce1c5b1222c5b3c99ca58b`;
  and matching observation digest
  `18adaf829233684272bf398cd60aaf77fef5ca8661065a68ba313e17bc433e10`.
  All four segment sizes and hashes matched, and both SQLite databases passed
  `integrity_check`.
- The final capture's one sequence gap was 18,529 records and exactly matched
  one first-post-attach `usb_queue_drops=18529` summary accumulated while no
  reader was present; all subsequent summaries reported zero drops. Across
  1,290 captured cycles there were zero FCS, filter, validation, or subreport
  CRC failures, one boundary peer miss, and a maximum callback of 2,868 us.
  The 46 rejected host records were all data arriving before the first periodic
  `HELLO`, as required by the decoder gate. All 69 host tests pass.
- Gate H4 N=3 runtime: PASS. Physical node 2 is label 3 / J-Link `760197416`,
  FICR `6402F3A7:947F4A25`. The common profile is nRF52833, 32 MHz SPIM3,
  channel 9, PRF 64 MHz, PLEN 128, PAC 8, SFD type 1, 6.8 Mb/s, EXT PHR,
  N=3, M=1, three occupied 10,000 us superslots, 625-byte frames, 64 taps, and
  config hash `0xC8CF`. Node 0 image SHA-256 is
  `B815BA2836FEB50173663731B172B6195A56C0336EDF300A0EC6895389EA9879`
  (79,208 B flash / 51,640 B RAM); node 1 is
  `5CB0339934D299EF060827530A6549D239CFBE0613BCFA4C643C918F58178E87`
  (42,332 B / 18,304 B); node 2 is
  `5D19DE9658433D11A293A0011B7068B75B0C534D005F61C1667F6F75F06953B7`
  (42,316 B / 18,304 B). All J-Link flashes verified `O.K.`. Antenna delays are
  the explicitly uncalibrated 16385 DTU bring-up values.
- H4 steady-state captures passed modulo-3 ownership, 2/2 summaries, six
  directed observations per complete cycle, and zero FCS, filter, validation,
  subreport CRC, delayed-start, timestamp, assembly, or timeout failures on the
  gateway. The final 100 summaries had zero peer misses and drops. Gateway,
  node 1, and node 2 callback maxima were 3,540 us, 2,990 us, and 3,021 us.
  Holding node 2 halted left node 1 active and the gateway transmitting; node 2
  misses were independent. Holding node 1 halted left node 2 active through its
  predecessor-loss fallback with 327 observations in each direction. Both
  releases restored 2/2 summaries. Reader
  detachment produced a 15,447-record sequence gap exactly matched by 15,447
  producer drops without perturbing subsequent radio timing.
- H4 UNO Q ingest/replay: PASS. Eight segments contained 6,032 records and
  5,909 canonical observations; all 991 summaries were 2/2. Segment hashes and
  sizes matched, both SQLite integrity checks were `ok`, raw digests matched at
  `e584d418fac65d683529a5aa5891e6afb3c79752d17a8e14d1e5f7094e5018db`,
  and observation digests matched at
  `08cc36484497d88bd605ef652ccab30777edd07074d0ebc4a47f72e9b874aa20`.
  All six directed pair counts were 983-986, consistent with 33.333 Hz. The
  host baseline is now 80 passing tests.
- N=5/M=2 three-board qualification: PASS. The tracked profile uses five
  superslots, two 625-byte frames per superslot, 64 CIR taps, a 3,500 us slot,
  a 35,000 us cycle, 28.571 Hz per directed link, modeled 187,200 B/s gateway
  USB load, and config hash `0x8885`. Nodes 3 and 4 were deliberately absent;
  their superslots remained silent and were never compressed or reassigned.
- The runtime now accepts same-`k` increasing-`m` frames, derives phase from
  `(k,m)`, transmits both report fragments with per-frame start counts, captures
  CIR only on `m=0`, pre-packs reports before fallback deadlines, and accounts
  for frame completion plus callback time when arming missing-node recovery.
  The receiver is rearmed immediately after accumulator-sensitive reads.
- Gateway pooled-report parsing and USB framing moved out of the radio callback.
  A 16-event preallocated FIFO feeds one export thread for every USB record type,
  eliminating sequence inversions while keeping USB backpressure independent of
  radio timing. The UNO Q owns M=2 reassembly, padding/count checks, subreport
  CRC validation, canonicalization, and replay.
- Final N=5 images for nodes 0-2 were flashed and verified `O.K.`. Node 0 SHA-256 is
  `436CDAC15FD2152EC3AFD7E8BE813D3BDA1D159CB6EA7593B89316CA98B78FA1`
  (79,908 B flash / 69,624 B RAM), node 1 is
  `AE8EFB80E564720C3844DD927497157970C12F736755CDAC3A8E85580CC92FC7`
  (42,444 B / 18,112 B), and node 2 is
  `DDE8D935F7916C59706AD1E8054A5D99C976DB44D344FB5076153CC39B5D6175`
  (42,428 B / 18,112 B).
- The 30-second rate-qualification capture covered 861 summaries and 5,156 canonical
  observations. All transmissions were confirmed; ownership and cycle binding
  were valid; USB sequence inversions, CRC failures, framing errors, validation
  rejects, firmware errors, and tail USB drops were zero. Active directed-pair
  counts were 855-861. Two isolated `m>0` losses occurred, plus one
  capture-boundary cycle; the last 100 summaries contained exactly the 200
  expected node 3/4 `m=0` misses. Gateway callback maximum was 1,892 us.
- Post-review binaries add generation-bound TX timeouts, stale-TXFRS rejection,
  atomic USB-drop claiming, and strict RX-before-summary export ordering. A final
  five-second smoke capture covered 146 summaries with valid ownership, all TX
  confirmed, no CRC/framing/validation errors, and exactly the expected node 3/4
  misses in the last 100 summaries.
- Board 5 is bound to node 4: J-Link `760223924`, FICR
  `454D4801:5B214CD7`. Its image built at 42,444 B flash / 18,112 B RAM with
  SHA-256
  `50B84078564EC88A8FCD9C67647136E3874473AE48CB7F0A7C2EC68FFA29527F`
  and flashed with J-Link verification `O.K.`. A 15-second capture had all node
  4 transmissions confirmed, valid ownership, zero CRC/framing/validation
  errors, and zero node 4 misses in the last 100 summaries. Moved node 2 missed
  19 of those 100 cycles, so four-active-node qualification is not yet a pass.
- Board 4's J-Link `760200606` initially entered USB product `1366:0101` after
  its old onboard firmware failed to finish an automatic update.
- Board 4 recovered after reconnecting J9 and is bound to node 3: J-Link
  `760200606`, FICR `86B7AC3A:20F3AB47`.
- Full-roster USB optimization: PASS at the original 3,500 us slot. The gateway
  uses a dedicated CDC workqueue, an 8 KiB CDC TX FIFO, speed-optimized code,
  and a 256-entry IEEE CRC32 table. RAM-only diagnostics separate the 16-event
  gateway slab from the 32-record stream slab without changing USB records.
  The final gateway image is 97,820 B flash / 79,160 B RAM with SHA-256
  `FC34D01ED2594084D0698D33C6E046442856A03808772F174D4F956A9A06E1B8`.
- D9-D12 now toggle on validated `m=0` reception from each board's four peers,
  ordered by peer node ID with the local ID omitted. D13 remains the
  non-programmable power/USB indicator. Final fixed-node images are 42,620-42,636
  B flash / 19,136 B RAM. SHA-256 values are node 1
  `53E83586EE4FDE2FF181526D5FA2383E65F28D9D0C0CD21FE83983ABD8B63C56`,
  node 2 `9C109FC6DA7018A80AE1703938CEF43A0026C40E7ABC93696449DB8740E2275B`,
  node 3 `C7D7A2ACBB9A19425B4CF8CD78F81203B251913498EA75E9CFF6D5E897F8D31E`,
  and node 4 `AA61156678F80E9239F1F1E4552391B1513ED406B1D6AAE5E4727C89AD2C3A16`.
  All five flashes verified `O.K.` with J-Link V9.62.
- A 60-second LED-enabled capture passed sustained export and protocol
  integrity. It contained 11,221,153 bytes and 1,717 summaries; after the first
  summary acknowledged pre-capture backlog, USB drops were zero. All TX records
  were confirmed; ownership and cycle binding were valid; CRC, framing,
  validation, FCS, and filter errors were zero. Gateway callback maximum was
  2,105 us. Radio delivery was 13,690/13,736 expected peer frames (99.665%):
  1,671 cycles received 8/8 and 46 received 7/8, primarily due to the current
  node 2 placement. Capture SHA-256 is
  `DD913D7F7CB6B773AA5FC65B12B2527ADADF0A1C64E5145CFD30599F809D34C4`.
- After review, the gateway explicitly initializes its CRC table before RX or
  USB export can start. The exact final image set passed a 30-second confirmation
  with 5,625,173 bytes and 859 summaries. The initial backlog acknowledgement
  and two residual startup drops were followed by zero tail drops; all TX was
  confirmed and protocol checks passed. Radio delivery was 6,853/6,872 frames
  (99.724%): 841 cycles were 8/8, 17 were 7/8, and one was 6/8. Capture SHA-256
  is `D97BCC529BFC629680D6690D086D5097634A0CF334857F0DA574B2A6A2C5A652`.
- A 3,000 us candidate reached 33.333 Hz with no delayed-start errors, but was
  rejected: 11/1,003 summaries missed one `m>0` frame and four summaries had an
  active-node `m=0` miss. Early RX rearm reduced this from 147 incomplete cycles,
  but the remaining approximately 173 us timing margin was not reliable.
- Peer activity LEDs now retain their per-reception toggle behavior while
  tracking each peer's last `k`. Once the schedule advances beyond the peer's
  next expected N-slot recurrence, its LED is forced off and its toggle phase is
  reset. This prevents a disconnected peer from leaving an LED on without adding
  timers, work items, allocation, logging, or USB traffic.
- The LED-expiry change was built for the qualified N=5/M=2 profile on 2026-07-27:
  32 MHz SPIM3, channel 9, PRF 64 MHz, PLEN 128, PAC 8, SFD type 1, 6.8 Mb/s,
  EXT PHR, hardware filtering, 64 CIR taps, 3,500 us slots, 35,000 us cycles,
  and config hash `0x8885`. Image SHA-256 values are node 0
  `8C36F8706A6AC53307BC8F50CC74129910283C53657BCA99DA6DCBCE2B48C4B1`,
  node 1 `D03A7C2C0069F6A8094EB2DB8ABCD5DAFF630E000E2D3D865E4C09AB8F82E818`,
  node 2 `8F38E2964459308ED92271FE27DBCEDB92D13AC347A5A2ABFBD8C5270B5BB58F`,
  node 3 `12B4B252BF266CF5F54A4DD68B2FD076FAB8BE7B64C9E3D2A144E39723545DAC`,
  and node 4 `85BC3DB3648CDE9222D2A9AC187B12BFEE0CA84D71FD42D8B10F82CA7180606E`.
  All five identity-bound boards (`760223921`, `760197419`, `760197416`,
  `760200606`, and `760223924`) flashed and verified `O.K.` with J-Link V9.62.
- Automated LED-expiry qualification passed the unchanged radio/export path.
  A controlled eight-second halt of node 2 produced one peer miss in every final
  100 gateway summary while ownership, cycle binding, TX confirmation, CRC,
  framing, filtering, and validation remained valid and tail USB drops were zero.
  After release, a five-second full-roster capture had 145 summaries, two
  isolated peer misses in the final 100, zero tail drops or protocol errors, all
  TX confirmed, and a 2,166 us callback maximum. Direct visual confirmation that
  the affected LED clears was skipped by operator choice and remains pending.
- The production UNO Q Rust service and embedded Svelte/WebGL2 dashboard build
  with Rust 1.93.1 and Zig 0.15.2. The protocol, DSP, and service suites pass all
  48 Rust tests; the Python reference suite
  passes 87 tests; Svelte checking and the production frontend build pass.
- Live N=5/M=2 service qualification covered all 20 directed links at the
  expected 28.571 Hz. A 15-second no-client run archived 2,822,533 bytes
  (188.2 kB/s), matching the modeled 187.2 kB/s. A five-client run archived
  2,813,651 bytes while serving distance, CIR, waterfall, fast-FFT, and CFO
  streams concurrently; the CIR client received approximately 12 MB/s.
- Host DSP optimizations cache the 16 fractional-phase Kaiser FIR coefficients,
  use coarse plus 1/16-sample correlation refinement, update causal distance
  filters incrementally, and bound the private DS matcher history to 120 ms.
  The N=8 synthetic benchmark sustained 53,215 link updates/s.
- Coordinated shutdown finalizes `.husb` segments before exit. A ten-second live
  capture finalized at 1,886,715 bytes and replayed 4,346 records into 5,147
  observations across all 20 links. Built-in verification reported SQLite
  integrity `ok`, zero genuine rejects, zero buffered bytes, and zero CRC or
  framing failures; 437 expected pre-`HELLO` records were tracked separately.
- Production cutover completed on 2026-07-27. The ARM64 binary at
  `/home/arduino/.local/bin/heimdall-service` has SHA-256
  `CB4D5602676C5BA23E87F0BA2F103C8581BB5828E2B077A6387219C6C69BF336`
  and serves the live dashboard/API on port 8080. The obsolete Python dashboard
  process is stopped and its `@reboot` entry is replaced by the tracked Rust
  launcher. Desktop and 390x844 phone renders passed after making node cards a
  readable horizontal rail on narrow screens. The tracked systemd unit passes
  `systemd-analyze verify`, but rootless deployment currently uses cron because
  installing a system unit requires privileged access.
- The final dashboard release sends compact 64-tap CIR products and reserves
  16x CIR interpolation for waterfall products. A 30-second five-client load
  delivered 199,768,296 WebSocket bytes across distance, CIR, waterfall,
  fast-FFT, and CFO clients with zero additional processing-queue drops, parser
  CRC failures, framing errors, or rejected records. The prior concurrent-client
  queue-drop regression is resolved.
- The final live browser audit passed all eight tabs at 1440x1000 and 390x844:
  all 16 views remained `LIVE`, all active links had data, canvases had valid
  dimensions, no synthetic state appeared, and Edge reported zero exceptions.
- Protected 30+30-second capture qualification produced clip 1 with 25,829
  complete records and 11,207,035 raw bytes. Its raw SHA-256 is
  `61420066BB7E595EB1129D487535C6D691F3C5E7F38D84F42A2AD704D140188D`;
  replay yielded 33,956 observations across all 20 links with zero genuine
  rejects, buffered bytes, CRC failures, or framing errors.
- The circular archive enforces its 200 MB quota and checks the 20% filesystem
  free-space floor before every write. Health now reports archive pause state,
  error count, last error, and free percentage; clip finalization reserves its
  expected space against the same floor. Full service-restart warm restoration
  remains open, while WebSocket reconnect now resets stale state and restores a
  compact five-minute distance snapshot on demand.
- The Windows ARM64 Snapdragon host now cross-builds the Debian ARM64 release
  with the pinned Rust 1.93.1 GNU/LLVM host toolchain and checksum-verified Zig
  0.15.2. The first successful release took 36.98 seconds, an incremental service
  rebuild took 21.59 seconds, and a no-change release check took 0.32 seconds.
  The resulting ARM64 PIE was checked with `file`, `ldd`, and live USB/API startup
  before production promotion.
- The latest browser functional audit passed all 16 desktop/phone tab states with
  zero exceptions and explicitly verified column-major N=5 layout: the first
  column is N0→N1 through N0→N4 and the second begins N1→N0. A clean
  simultaneous all-topic five-client load delivered 159,785,120 WebSocket bytes
  across distance, CIR, waterfall, fast-FFT, and CFO with zero additional
  processing-queue drops, parser CRC failures, framing errors, or rejected
  records. The zero-drop result was achieved after compaction-gating the
  distance-history clone path, skipping unused serde fields (evidence, mm fields,
  bridge_duration_s), and increasing the processing channel buffer from 128 to
  1,024 records.
- The CIR waterfall repair was built with the pinned Rust 1.93.1 Debian ARM64
  release profile and deployed on 2026-07-27 without changing radio firmware.
  The active board/profile remains DWM3001CDK N=5/M=2, channel 9, 6.8 Mbps,
  64 taps, 3,500 us slots, 35 ms cycles, and config hash `0x8885`. Waterfall rows
  now use one stable reference-peak-relative Kaiser grid from startup, publish
  `x_min`/`x_max`/`x_step`, zero unavailable edge samples, and emit the processed
  current row rather than the original aligned CIR. Invalid CIR records are no
  longer admitted, dB limits are converted once, and display history is retained
  by event time.
- Live waterfall load qualification passed on all 20 directed links. The raw
  path delivered 52,988,896 bytes in 20 seconds; magnitude clutter delivered
  42,822,280 bytes; and complex nuisance fit plus noise clipping/path-loss
  compensation delivered 14,116,704 bytes. Each final run had zero additional
  processing-queue drops, CRC failures, or framing errors. The final health
  snapshot was `ok` with 20/20 links, zero cumulative queue drops, CRC failures,
  and framing errors after restart. The all-tab desktop/390x844 functional audit
  also passed with zero failures or browser exceptions. Production settings were
  restored to clutter/path loss off, spike rejection on, and taps `-20..50`.
- Four deterministic service regressions cover invalid-CIR admission, aligned
  grid edge behavior, processed-current-row output, and settings validation.
  The ARM64 Linux release compiles successfully, while test-binary execution was
  not repeated on Windows because the existing GNU/LLVM host linker wrapper
  treats Rust's `-o` argument as a PowerShell common-parameter abbreviation.
- Waterfall spike rejection now distinguishes one isolated low-correlation frame
  from a persistent channel change. Live sampling found that 73/80 `1>4` frames
  and 75/80 `4>1` frames fell below the old 0.90 all-or-nothing gate even though
  both links remained healthy. With rejection enabled after the fix, a five-second
  capture delivered 142 `1>4` rows and 143 `4>1` rows with nonzero maxima above
  8.1, and all 20 directed links were represented. A controlled 20-second final
  stream delivered 54,061,944 bytes with zero queue-drop, CRC, or framing deltas.
- The dashboard tab is now named `Live CIR` while retaining the compatible
  `instantaneous-cir` subscription. Waterfall controls are grouped by history,
  tap window, color scale, static removal, and compensation; tap endpoints use
  explicit numeric fields, checkboxes remain next to complete labels, desktop
  controls wrap without widening the five-column plot grid, and mobile controls
  scroll independently. Safe waterfall defaults were restored after validation.
- A timestamp-only 30-second static-clock measurement used protected clip 2
  (`a826bea0854483aebe9f219d83d8bcb7668774554ad0f017c35394ecc64b2b98`)
  and 16,909 post-trigger observations across all 20 directed links. Robust
  one-second sliding skew fits found median/max absolute relative drift of
  0.00006089/0.00013194 ppb/ms. Reverse-link drift sums agreed within
  0.00000086 ppb/ms RMS, and the per-node decomposition relative to N0 left
  0.00000094 ppb/ms RMS residual. The maximum fitted drift changes relative
  frequency by 3.958 ppb over 30 seconds and corresponds to only 0.097 mm of
  light travel over 70 ms before ADS-TWR cancellation. Raw and processed local
  artifacts are under ignored `captures/raw/` and `captures/processed/`; the
  reusable analyzer is `host-tools/clock-drift/analyze_timestamp_drift.py`.
- Live Distance now retains finite negative SS-TWR and DS-TWR estimates instead
  of rejecting them as nonphysical. Such samples carry quality bit
  `NEGATIVE_RANGE`; the dashboard displays them by default and provides a
  `CENSOR NEGATIVE` toggle that masks them as plot gaps without deleting
  history. Canvas2D and WebGL line rendering both preserve those gaps. The
  ARM64 release was deployed on 2026-07-27 with SHA-256
  `A06BB64C09A6E11000A24BEA1C01A50E9C25E4A0D494B9FA3D1D3E6EC1156E69`.
  Post-restart health showed all 20 links, zero rejected records, and zero
  processing-queue drops. The N=5/M=2 radio firmware, PHY, 3,500 us slot,
  35 ms cycle, board identities, and config hash `0x8885` were unchanged.
- CIR Waterfall dB conversion now caches each converted telemetry frame, returns
  stable frame identities to the render loop, skips all bound computation when
  fixed scale is active, and uses a linear-time 256-bin percentile estimate for
  dynamic scale instead of sorting every heatmap on every animation-frame read.
  The optimized embedded dashboard was deployed with ARM64 service SHA-256
  `948DF1597D59E3291DA0E47D8E8CD22B8BF559FCBC1ED771E8AAC38CCF2F91A0`.
- Board Positions now locks its camera center and scene span after framing, so
  live solver updates move boards within a stationary view instead of repeatedly
  recentering and rescaling the scene. Node-count changes and `RESET VIEW`
  deliberately reframe. CIR Waterfall now also exposes editable MIN/MAX dB color
  limits, persists them through the existing settings API, and enforces ordered
  bounds. The combined dashboard was deployed in ARM64 service SHA-256
  `A3E3EDC6F632633F16B3BF4704D54AD88090892DA44F2BC0BC050E520E29E925`.
- The browser-side Board Positions computation now uses classical MDS for a
  complete distance-matrix initialization and reduced-coordinate
  Levenberg-Marquardt refinement with warm starts at a 10 Hz solve cadence.
  Orientation is applied as whole-configuration reflections; Jacobian rank,
  optimizer convergence, and iteration count are reported explicitly. Exact
  geometry is scale-invariant in tests, a noisy complete graph and an observable
  graph with one missing edge converge, and the previously failing live review
  matrix fits below 1 cm RMS instead of approximately 1.06 m RMS. The corrected
  embedded dashboard was deployed in ARM64 service SHA-256
  `60CE121F611D78BF48D0ABE985A20CE6648F07F71C4256D0DA2693765C4BDA7B`.
  Live desktop/phone audits reported full 9/9 rank, convergence in 5/4
  iterations, and 0.28/0.11 cm fit RMSE respectively, with zero browser
  exceptions or audit failures.
- The first downstream Windows radar-map milestone is implemented under
  `host-tools/radar-map/`. It replays canonical observations from `.husb`,
  loads explicit metre-based board geometry, identifies the known CIA
  `false_first_path` condition using absolute-index discontinuities plus
  correlation/energy gates, aligns and clutter-subtracts CIRs, and performs
  multistatic magnitude backprojection into a configurable `(z,y,x)` voxel
  volume. It exports NumPy arrays and JSON metadata, offers optional Zarr
  output, and serves XY/XZ/YZ slices through a minimal local HTTP API.
- Synthetic tests localize a surveyed 3D reflector and cover anomaly rejection,
  storage round trips, and slice axis order. The full Python suite now passes
  93 tests. A replay smoke test on protected clip 2 decoded 25,787 records and
  33,367 unique canonical observations, admitted 32,994 observations across all
  20 directed links, and wrote a `(5,7,7)` test volume with zero CRC/framing
  failures or trailing bytes. The geometry used for that smoke test was the
  explicit non-surveyed example and does not validate physical map accuracy.
- NumPy 2.2.6 is the only required new runtime package. No native Windows ARM64
  wheel exists for that release, so the validated laptop path currently uses
  CPython x86-64 under Windows ARM64 emulation. Zarr remains optional and
  unpinned until its storage contract is stabilized.
- The radar-map server now includes a dependency-free browser viewer with
  linked XY/XZ/YZ heatmaps and an interactive WebGL 3D point cloud. The point
  products are percentile-thresholded and capped server-side, board coordinates
  are overlaid, and canonical `TOP +Z` / 3D cameras render +X right and +Y up.
  Static-environment magnitude with a two-tap direct-path guard and complex
  median-baseline motion residual are exported and selectable independently.
- A fresh protected live capture was triggered through the UNO Q API as clip 6.
  It contains 25,877 records and 11,227,684 raw bytes with SHA-256
  `290C66ED400F3846710C5DFCEDF82A92E44760D553090D006331BEDD7AE93D61`.
  Replay produced 33,664 canonical observations across all 20 links; 33,533
  passed radar-map quality gates and 131 were classified `false_first_path`.
- `deployment/radar-geometry.live-20260728.json` records a contemporaneous
  range-derived frame with N0 at the origin, N1 on +X, N2 on +Y, and N3 on +Z.
  It is now the exact `dashboard-live-5962002` geometry export rather than an
  independently reconstructed range fit. The snapshot records all ten smoothed
  DS-TWR inputs, edge ages, five coordinate triples, rank 9/9, four iterations,
  and 0.27 cm RMS. Programmatic comparison against radar metadata reports zero
  coordinate difference. The dual live-data volumes use 5 cm spacing over a
  `71x67x47` grid.
- The dashboard retains DS edges but displays each edge's age, and its canonical
  reset/top cameras no longer render +Y downward. Projection orientation and
  solver behavior pass all six tests. Board Positions exposes the exact solved
  geometry as a downloadable JSON snapshot, and the live audit stores the same
  snapshot for radar replay. Svelte checking, the production frontend build,
  and 93 Python tests pass. The corrected ARM64 service binary has SHA-256
  `6C826C204EDC2CFBE54DC3A62AFD9DBA435ADD5CD5A43EFFB8EF17B72AC9FEFD`.
  Deployment remains pending because both
   `arduino@192.168.8.215` and `arduino@chinny` reject non-interactive SSH
   authentication.
- The UNO Q was switched to headless boot on 2026-07-31: its default systemd
  target is `multi-user.target`, and `lightdm` is disabled and inactive. After
  reboot, `arduino-router.service` and the Heimdall launcher were verified
  active. This does not affect SSH, Arduino CLI, or the deployed service. To
  restore the local HDMI desktop, run `sudo systemctl set-default
  graphical.target` and `sudo systemctl enable --now lightdm` on the UNO Q.

## Next executable checkpoint

- Classifier pipeline expanded on 2026-08-06. Training now supports a five-class
  seat target including `Empty`, dynamic person labels, independent seat/person
  models or a shared dual-head model, standard/lite backbones, a canonical
  10-link reciprocal feature set (with 20 directed links retained as an option),
  and zero-padded LOS windows controlled by taps left/right. Schema-v2 model
  manifests carry the exact live feature contract. Live inference consumes that
  contract, preserves legacy checkpoints, publishes raw and rolling-smoothed
  seat/person outputs, and records average/p95 snapshot latency. Python contract
  tests and 50 Rust service tests pass; one checkpoint-dependent inference test
  remains ignored. Live hardware accuracy and the `<35 ms` p95 snapshot target
  are not yet verified, and no ONNX/QNN runtime dependency has been introduced.
- Commit `4e3a772` was rebuilt and deployed to the Windows ARM64 processing
  server on 2026-08-06. The running scheduled task owns UDP `7878` and TCP
  `8080`, `/api/health` reports `ok`, the served dashboard bundle contains the
  new Training UI and `Empty` class. A follow-up removed stale `qc_de` training
  paths: the deployed service now discovers this checkout's classifier toolkit
  and validates Torch/NumPy interoperability before selecting Python. It chose
  `C:\Users\anand\miniconda3\python.exe`. The Captured Clips table now uses
  explicit row selection and bulk select/unselect/delete actions, and training
  accepts only the selected `clip_ids`. The deployed bundle and empty-selection
  API guard were verified. The Simulator tab is now Presence Detection; Empty
  maps to no chair, and occupied-chair overlays show the stable predicted person
  name instead of seat abbreviations. Person-only checkpoints now leave all
  chairs unassigned and show the stable identity in the cabin center. Presence
  Detection now exposes one 1-300 snapshot majority window shared by person-name
  voting and seat-position probability smoothing. The served UI and backend
  validation were verified; the replacement binary SHA-256 is
  `526C303DFC4C285C8A077F4FE1B39D0F95476BAE14C78AF620BF7AB225B2290B`.

- Live migration in progress (2026-07-30): `heimdall-service agent` now keeps
  only CDC validation, local health, LED status support, and UDP forwarding;
  `heimdall-service server` receives the live stream before the existing DSP
  pipeline. The agent writes no raw archive or spool. See
  `unoq/deploy/LIVE-MIGRATION.md` for startup and firewall requirements.
- Portable live split deployed on 2026-07-30. UNO Q `192.168.137.98` runs the
  agent and forwards to the Windows hotspot gateway `192.168.137.1:7878` with
  zero agent UDP send drops. The Windows server exposes the full dashboard on
  `http://192.168.137.1:8080`; it received the HELLO/configuration and all 20
  expected directed links. Host tests pass (55 tests), the ARM64 test binaries
  link, and the deployed binary SHA-256 is
  `4A858180A1AA559D860323565D6E234C7A6E4FF123DC7FDEC0CFD535B1D53C8A`.
- Windows live-path tuning was deployed on 2026-07-30: strict topic gating
  prevents distance/health views from running CIR DSP; UDP records are
  microbatched for at most 2 ms; WebSocket output retains only the newest
  topic/link/kind value every 16 ms; and dashboard state/rendering is browser
  frame-coalesced and event-driven. A five-node live sample had zero processing
  drops and expired records, 0.032 ms average queue wait, 0.392 ms average
  processing time, and 0.037 ms average WebSocket send time. The server ran at
  approximately 7% of one CPU core while receiving all 20 directed links.
- WebSocket delivery was subsequently changed to one `HMB1` binary batch per
  16 ms interval. A live distance sample carried an average 21.6 latest link
  updates per browser message, with zero queue drops or expired UDP records;
  this removes the previous hundreds-to-thousands of individual browser message
  callbacks per second.
- The live path returned to the `Ullas` infrastructure network on 2026-07-31:
  UNO Q `192.168.8.215` forwards to Windows `192.168.8.101:7878`, and the
  dashboard is at `http://192.168.8.101:8080`. Direct-network probing measured
  22.1 ms p95 latency with no periodic stalls. The Windows Mobile Hotspot path
  measured 113-122 ms p95 with recurring 190-210 ms pauses; those pauses were
  present before server processing and are not caused by dashboard rendering.
  After retargeting, all 20 directed links advanced with zero processing queue
  drops and invalid UDP datagrams. Rootless boot remains the existing `@reboot`
  crontab launcher, with the target in
  `/home/arduino/.config/heimdall-agent.env`.
- A dedicated routerless Ethernet mode was deployed on 2026-08-02. The laptop
  USB GbE interface uses `192.168.250.1/30`; the UNO Q USB Ethernet interface
  uses the persistent NetworkManager profile `Heimdall Direct Ethernet` at
  `192.168.250.2/30`. Neither side installs a gateway or DNS server. The UNO Q
  launcher prefers Ethernet target `192.168.250.1:7878` while carrier is
  present, then falls back automatically to the configured target for the active
  Wi-Fi profile. Hardware verification measured 1 ms RTT with zero ping loss;
  Windows received 1,324 UDP records in three seconds with all 20 directed
  links, advancing rounds, zero queue drops, and zero expired/invalid UDP
  records. Repeatable setup is in `configure-direct-ethernet.sh` and
  `enable-direct-ethernet.ps1`.
- The `HaQathon` WPA3 network was configured and hardware-verified on 2026-08-03.
  The persistent NetworkManager profile uses SAE, autoconnect priority 10, and
  IPv4/IPv6 route metric 600; `Heimdall Direct Ethernet` retains priority 100.
  With Ethernet unplugged, the UNO Q remained reachable over Wi-Fi at its
  DHCP-assigned address `10.73.51.61`, and the launcher changed the agent target
  to the Windows host at `10.73.51.192:7878` within its two-second polling loop.
  The boot `@reboot` launcher and its Wi-Fi fallback in
  `/home/arduino/.config/heimdall-agent.env` were verified. No UWB gateway board
  was attached for this check, so CIR forwarding was not expected or tested.
  These DHCP addresses are not stable and must be checked before use. Wi-Fi
  credentials are stored only in the UNO Q NetworkManager profile and are
  deliberately not recorded in this repository.
- Remaining UNO Q transport work is deliberately deferred: periodically replay
  HELLO so a Windows restart does not require restarting the agent, and add a
  clocked transport timestamp before claiming measured cross-machine latency.

N=5/M=2 is hardware-qualified with all nodes active at 28.571 Hz, including
lossless steady-state gateway export at the full modeled 187,200 B/s. The next
deployment checkpoint is improving node 2 placement/link reliability, then
performing per-board antenna-delay calibration and ranging validation.

Per-board antenna-delay calibration and ranging validation remain required. All
current boards still use the explicit uncalibrated 16385 DTU bring-up value; do
not present their timestamps as accurate ranges. Beacon-v1 cycle-summary
behavior at the approximately 545-year u32 wrap boundary for non-power-of-two N
also remains a protocol clarification item; ordinary wrapped ownership selection
is already tested and does not stall.
