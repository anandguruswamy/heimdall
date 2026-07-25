# Firmware Glossary

This glossary defines the hardware, Zephyr, and Heimdall terms used by the
radio firmware.

## Hardware

### DWM3001CDK

The development board used by Heimdall. It combines an nRF52833 MCU with a
Qorvo DW3110 UWB radio and exposes both J-Link and native USB connections.

### DW3110 / DW3000

The DW3110 is the UWB radio on the DWM3001CDK. The firmware and driver commonly
use `DW3000` as the family/API name.

### nRF52833

The Nordic MCU running the firmware. It provides the CPU, GPIO, timers, SPI,
USB, and radio-control interfaces used by the application.

### J9 and J20

- **J9**: J-Link USB connection used for programming, debugging, and J-Link
  VCOM serial output.
- **J20**: native USB connection used by the gateway data path and USB CDC.

Use J9 for board identification and flashing. Use J20 when testing the native
USB CDC gateway path.

### J-Link serial number

The stable hardware identifier exposed by the onboard debugger. It is more
reliable than a Windows COM port and is the identifier recorded in
`deployment/board-inventory.md`.

### VCOM / COM port

The virtual serial port exposed by J-Link. COM numbers are host-specific and
may change when a board is moved to another USB port, hub, or computer.

## Zephyr

### Zephyr

The embedded RTOS, driver framework, configuration system, and build ecosystem
used by the radio firmware.

### west

Zephyr's workspace and build tool. It fetches the pinned Zephyr and DW3000
driver repositories and invokes CMake/Ninja for builds.

The Heimdall west workspace root is `firmware/`.

### Board target

The identifier passed to `west build -b`. The current target is:

```text
nrf52833dk/nrf52833
```

It means the Zephyr `nrf52833dk` board family with the `nrf52833` SoC
qualifier. The physical DWM3001CDK reuses this board support with custom
overlays.

### DeviceTree

Zephyr's hardware description system. It describes peripherals, GPIOs, buses,
and device connections independently of application code.

### DeviceTree overlay

A project-specific `.overlay` file that modifies the board hardware
description. Heimdall overlays configure the DW3110 SPI bus, IRQ/reset/wakeup
GPIOs, UART pins, USB, and the status LED.

The underscore-form filenames, such as
`nrf52833dk_nrf52833.overlay`, are intentional overlay naming conventions. They
are not the current `west build -b` target.

### Kconfig

Zephyr's build-time feature configuration system. Heimdall uses it to select
roles such as scheduled TX, sensing RX, and TWR initiator/responder, as well as
USB and timing options.

Key files are `app/Kconfig`, `app/prj.conf`, and `app/usb.conf`.

### CMake and Ninja

CMake generates the firmware build system; Ninja executes the generated build.
The pinned project setup uses CMake 3.31.10.

### Zephyr SDK

The compiler, linker, debugger tools, and related utilities used to produce
firmware binaries. For this project the required compiler is
`arm-zephyr-eabi`.

## SPI and radio firmware

### SPI / SPIM3

SPI is the synchronous bus between the nRF52833 and DW3110.

`SPIM3` is Nordic's third, EasyDMA-capable SPI master peripheral. Heimdall has
two build profiles:

- SPI1 at 8 MHz: known-good rollback profile
- SPIM3 at 32 MHz: tested high-speed profile

The profiles use the same physical radio wiring but different nRF peripherals.

### IRQ-driven RX

The radio asserts an interrupt when a receive or transmit event occurs. The
firmware handles the event rather than repeatedly polling the radio.

### CIR

Channel impulse response data captured from the DW3110 accumulator. Heimdall's
initial live profile reads 64 complex taps and preserves timestamps, CFO, and
quality metadata.

### TWR

Two-way ranging. The current Phase 1 firmware includes single-sided TWR
initiator and responder roles.

### Scheduled TX

Delayed radio transmission based on the DW3110's device clock. Heimdall uses it
as the foundation for deterministic multi-node slots.

## Heimdall protocol terms

### Beacon

A scheduled UWB frame announcing a node's identity, round, slot, sequence, and
available observation data.

### Round and slot

A round is one complete scheduled sensing cycle. A slot is a node's assigned
transmit position within that round. The initial topology defines seven slots
at 50 Hz.

### USB CDC

USB Communications Device Class. It provides a serial-like transport between
the gateway DWM3001 node and the UNO Q.

### Capture/replay

The ability to feed recorded frames into the same decoding and fusion path as
live USB input. This allows development without connected radios.
