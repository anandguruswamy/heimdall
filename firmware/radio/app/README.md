# Phase 1 applications

The hardware bring-up application sources live here. Build artifacts stay in
the sibling `build-*` directories and are ignored.

## Build

From the `firmware` west workspace root, build the nRF52833 application with:

```powershell
$usbOverlay = (Resolve-Path radio/app/boards/nrf52833dk_nrf52833_usb.overlay).Path.Replace('\', '/')
west build -p always --no-sysbuild -b nrf52833dk/nrf52833 radio/app -d build-radio -- "-DOVERLAY_CONFIG=radio/app/usb.conf" "-DEXTRA_DTC_OVERLAY_FILE=$usbOverlay"
```

The older `nrf52833dk_nrf52833` spelling is the board's devicetree and overlay
filename convention, not the current Zephyr board target. Do not rename files
such as `boards/nrf52833dk_nrf52833.overlay`.

## N=2 runtime gate

Build each runtime image with `runtime.conf`, the SPIM3 overlay, and all four
per-board bindings below:

```powershell
west build -p always --no-sysbuild -b nrf52833dk/nrf52833 radio/app -d build-beacon-runtime-node0 -- `
  "-DOVERLAY_CONFIG=C:/path/to/Heimdall/firmware/radio/app/runtime.conf" `
  "-DEXTRA_DTC_OVERLAY_FILE=C:/path/to/Heimdall/firmware/radio/app/boards/nrf52833dk_nrf52833_spim3.overlay" `
  "-DCONFIG_HEIMDALL_NODE_ID=0" `
  "-DCONFIG_HEIMDALL_EXPECTED_DEVICE_ID_LOW=0x..." `
  "-DCONFIG_HEIMDALL_EXPECTED_DEVICE_ID_HIGH=0x..." `
  "-DCONFIG_HEIMDALL_TX_ANTENNA_DELAY_DTU=..." `
  "-DCONFIG_HEIMDALL_RX_ANTENNA_DELAY_DTU=..."
```

Use node ID 1 and that board's identity and calibration for the second image.
The runtime refuses to start if the identity does not match or either antenna
delay is zero. Runtime state is available to a debugger in the global
`heimdall_runtime_counters` structure.

For the gateway image, use `runtime-gateway.conf` and include both the SPIM3
and `nrf52833dk_nrf52833_usb.overlay` devicetree overlays. The gateway emits
binary USB CDC v1 records and therefore MUST NOT use the USB-console overlay or
share that CDC endpoint with logs.

## Runtime activity LEDs

Beacon runtime uses D9-D12 for peer reception activity. Each LED toggles after
a validated `m=0` frame; peers are mapped in ascending node-ID order with the
local node omitted. D9 is green, D10-D11 are red, and D12 is blue. D13 is the
board power/USB indicator and cannot be controlled by the nRF52833 firmware.
