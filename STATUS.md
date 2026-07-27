# Heimdall Status

## Project state

Scaffold created. No Heimdall-specific multi-node beacon protocol has been
flashed yet.

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

## Next executable checkpoint

Three measurements gate the protocol's numeric claims and must be taken before
implementation is trusted:

1. RX callback duration, instrumented with a GPIO toggle or cycle counter at
   32 MHz SPI, at both 64 and 128 taps. PASS for the Gate 1 read path at 1983 us
   and 2868 us respectively; the 64-tap production assembly profile measured a
   2227 us full callback with the optimized encoder.
2. USB CDC throughput after replacing the byte-at-a-time `uart_poll_out()` path.
   PASS at a 475 kB/s offered load with 20,000 complete ordered records; this
   exceeds the modelled 234-433 kB/s gateway range.
3. `DWT_PHRMODE_EXT` verified board-to-board, together with hardware frame
   filtering, broadcast addressing, and auto-ACK confirmed disabled. PASS for
   the current 1023-byte test profile; the 38 observed RX errors remain a
   separate link-budget characterization item.

The next task is gateway heartbeat plus normative USB runtime records, followed
by capture/replay coverage. Per-board antenna-delay calibration remains required
before treating runtime timestamps as calibrated ranging measurements.
