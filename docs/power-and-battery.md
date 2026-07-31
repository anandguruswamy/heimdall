# Node Power and Standalone Operation

Investigation into powering DWM3001CDK nodes without a host USB connection.
Records what has been verified on our own boards, what is inferred, and what
remains unknown.

Status: unresolved. The battery input path on our boards has not been shown to
be usable. USB power remains the working option.

## Board power options

The Qorvo Quick Start Guide power-options table lists five inputs:

| Power option | Connector |
|---|---|
| USB, J-Link | `J9` |
| USB, nRF52833 native | `J20` |
| Battery, JST SR connector | `J12` |
| Battery, loose wires | `J1` |
| Raspberry Pi interface | `J10` |

Two points from the same document:

- The board does **not** include a battery charger. Charge management must be
  external.
- Solder bridges exist to isolate peripherals for current measurement. `J16` is
  Segger power, `J6` is Segger reset, `J4` connects the module to the internal
  DC-DC, and `J2` is a header wired in parallel with `J4`.

The Zephyr board page for `decawave_dwm3001cdk` states the board includes a
"battery connector and charging circuit". This contradicts Qorvo's own
documentation and should not be relied on.

The full DWM3001CDK schematic is available only under NDA. Qorvo staff have
stated on their forum that they will answer specific connector and jumper
questions without one.

## Electrical limits

From the DWM3001C datasheet:

- Supply voltage VDD: 2.5 V to 3.6 V operating, 4.0 V absolute maximum.
- Channel 9 (our configured PHY, `deployment/beacon-config.n5.json`):
  RX 45 mA, TX 45 mA, IDLE 32 mA, SLEEP 850 nA.
- Channel 5 for reference: RX 40 mA, TX 40 mA, IDLE 18 mA.

A standard 1S LiPo spans 3.0 V to 4.2 V. A fully charged cell therefore exceeds
the module's absolute maximum if the battery input reaches VDD without
regulation. This is the central unresolved risk.

Firmware currently keeps the receiver active rather than sleeping between
superslots, so the sleep figure is not reachable today. Budget roughly 45 mA for
the module alone, plus unmeasured board overhead.

## Board configuration as received

Verified on all five boards in `deployment/board-inventory.md`:

- `J2` header populated with a shunt.
- `J4` solder bridge open.

This matches the factory configuration described on the Qorvo forum, where `J4`
and `J2` are parallel paths and only one is fitted. All five boards are
consistent, which rules out mixed power-path configuration as a contributing
factor in the node 2 link problem recorded in
`firmware/radio/BRINGUP-NOTES.md`.

## Measurements taken

1. **DMM on `J1` with the board USB-powered.** Reading was not stable; it
   decayed steadily toward zero. This is the signature of a floating,
   high-impedance node being discharged by the meter's input impedance. It is
   not a rail.

2. **Diode mode between `J1` and `J2`, shunt removed, board unpowered.** No DC
   path found in any of the four pin combinations, in either polarity. Only
   intermittent momentary readings, consistent with the meter's current source
   charging a decoupling capacitor before the reading walks off to open.

## What the measurements do and do not establish

Absence of continuity on an unpowered board does not prove absence of
connection. Three cases are indistinguishable to a multimeter:

- The path is genuinely unpopulated. Footprints designated `D2` and `D4` sit in
  this area of the board; an unfitted part there would explain every
  observation so far.
- `J1` has been misidentified.
- A load switch or power-path FET sits in the path. Unpowered and off it reads
  open in both directions, yet works normally once energised. A typical DMM
  diode mode sources about 1 mA below 2 V and cannot turn one on.

## Remaining verification steps

Not yet performed.

1. **Confirm `J1` identification.** Board unpowered, continuity from each `J1`
   pin to the USB connector shell. One pin should be ground. If neither is,
   the preceding measurements are void.

2. **Confirm `J1` is on the battery net.** Continuity from the `J1` non-ground
   pin to the `J12` positive pin. Both are listed as battery inputs and should
   be the same net. If connected, the break is downstream and points at `D2` or
   `D4`.

3. **Identify the `J2` pin roles.** Shunt removed, USB connected, measure each
   `J2` pin to ground. One should read about 3.3 V, the internal DC-DC output.
   The other should float, being the now-disconnected module side.

4. **Apply a safe supply.** Only powering the input distinguishes an
   unpopulated path from a load switch that is off. Two AA alkaline cells give
   about 3.1 V to 3.2 V, below the module's 3.6 V limit, so the test cannot
   damage the board regardless of what `J1` turns out to be. Shunt refitted,
   USB disconnected, positive to the `J1` non-ground pin. If the board boots,
   `J1` feeds the module rail and the usable voltage window is known.

A current-limited bench supply set to 3.3 V with a 100 mA limit is preferable to
the AA cells if one is available. Never begin at 4.2 V.

## Standalone power options, ranked

1. **USB power bank into `J20`.** No board modification, no voltage risk, and
   all boards stay identical and debuggable. The bank must have a low-current or
   always-on mode; at roughly 50 mA the board sits below the auto-shutoff
   threshold of most power banks and will be cut off after a few seconds.
   Estimated runtime is about four days from a 10000 mAh bank, unmeasured.

2. **LiPo with charger and 5 V boost into `J20`.** A self-built power bank, for
   example a boost-and-charger board with a 1S cell. Avoids the auto-shutoff
   problem. Wastes roughly a third of the energy boosting to 5 V only for the
   board to regulate back down.

3. **Regulated 3.3 V into the module side of `J2`.** Qorvo's documented method:
   pull the shunt, feed 3.3 V to the module-side pin with ground at the `J1`
   upper pin. Most efficient, but it requires per-board modification, and with
   the Segger unpowered the module's SWD, UART, and reset lines back-feed
   through the STM32's ESD diodes. Avoiding that leakage means cutting `J16` and
   `J6`, which gives up the on-board debugger.

4. **Battery direct to `J1` or `J12`.** Blocked pending the verification steps
   above.

Note that powering through the battery input also powers the STM32 running the
J-Link OB firmware, so battery draw will exceed the module's 45 mA unless `J16`
is cut.

Recommendation: option 1 until measured runtime proves insufficient. Bespoke
per-node power hardware makes future intermittent faults harder to attribute,
which is a poor trade on a system with an open link-reliability issue.

## Sources

- Qorvo DWM3001CDK Quick Start Guide, 2022 revision, power options and solder
  bridge tables.
- Qorvo DWM3001C datasheet, tables 4, 5, and 9.
- Qorvo Tech Forum, "Measuring power consumption on DWM3001CDK Board" and
  "How to measure power consumption on the DWM3001CDK", for the `J16`, `J6`,
  `J4`, `J2`, and `J1` roles and the GPIO back-powering warning.
