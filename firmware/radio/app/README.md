# Phase 1 applications

The hardware bring-up application sources live here. Build artifacts stay in
the sibling `build-*` directories and are ignored.

## Build

From the `firmware` west workspace root, build the nRF52833 application with:

```powershell
west build -p always --no-sysbuild -b nrf52833dk/nrf52833 radio/app -d build-radio -- "-DOVERLAY_CONFIG=radio/app/usb.conf"
```

The older `nrf52833dk_nrf52833` spelling is the board's devicetree and overlay
filename convention, not the current Zephyr board target. Do not rename files
such as `boards/nrf52833dk_nrf52833.overlay`.
