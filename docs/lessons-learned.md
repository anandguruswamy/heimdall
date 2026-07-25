# Lessons Carried Forward

This is the useful engineering knowledge from the experiments, without their
historical narrative.

## Radio and firmware

- The closed DWM3001 FiRa MAC exposes SS-TWR/DS-TWR but not the custom
  all-pairs CIR behavior Heimdall needs.
- Full CIR access and custom scheduled beacons require the open DW3000 driver
  path.
- SPIM3 at 32 MHz passed controlled two-board tests; retain the 8 MHz overlay
  as a rollback build profile.
- `DW_CIA_DIAG_LOG_ALL` is required before the driver’s CIR diagnostic helper
  returns populated first-path fields.
- Use `dwt_readcir_48b()` for correctly addressed accumulator windows.
- Scheduled TX must be measured for jitter before choosing TDMA guard bands.
- The initial two-board tests had high reception rates but not perfection;
  loss counters and replayable captures are mandatory.

## Build tooling and host platforms

- Native ARM64 Windows is viable for this project. The Windows x86-64 Zephyr
  SDK runs under Windows ARM emulation and successfully detects the nRF52833
  compiler.
- Zephyr SDK 0.17.4 does not provide a Windows ARM64 bundle. Install its
  Windows x86-64 bundle and the `arm-zephyr-eabi` toolchain.
- Keep CMake at 3.31.10 for the pinned Zephyr tree; CMake 4.4.0 is incompatible
  with its `FindZephyr-sdk.cmake` syntax.
- Windows SDK setup requires `wget` and 7-Zip. The minimal SDK archive does not
  contain the compiler until the target toolchain is installed separately.
- The current Zephyr board target is `nrf52833dk/nrf52833`. The underscore form
  remains in devicetree and overlay filenames and should not be renamed.
- Keep west, CMake, and Zephyr Python packages in one consistent environment.
  CMake invokes the Python interpreter it discovers, which may differ from the
  interpreter used by `pip`.
- The SPIM3 overlay must delete or rename the default SPI1 `dw3000` label before
  defining the SPI3 instance; disabling SPI1 alone does not remove its label.

## USB and transport

- Native nRF52833 USB CDC works as a parallel data path and is preferable to
  using the fragile UNO Q Bluetooth controller for the data plane.
- The gateway must use the DWM3001 native USB/J20 path; J9 is the J-Link path.
- Never let serial or BLE output block the UWB task.
- A compact fixed-size CIR window is useful for live operation; full diagnostic
  windows remain an offline capture mode.
- Binary framing, CRCs, sequence numbers, and explicit producer-drop counters
  are required for trustworthy data.

## UNO Q and operations

- Board-side Arduino builds may need `TMPDIR=/tmp` because `/data/local/tmp`
  is absent on the stock Debian image.
- ADB is a USB access transport, not an Android runtime.
- Keep capture/replay separate from live radio access so fusion development can
  continue when the bench is unavailable.
- BLE dual-stream tests crashed the UNO Q WCN3990 under sustained small-
  notification traffic. Heimdall therefore keeps BLE out of the data plane.
