# Phase 1 version and bench inventory

Recorded 2026-07-20 for reproducible Gate G1 bring-up.

## Pinned firmware dependencies

| Component | Pin | Reason |
|---|---|---|
| Zephyr | `nrfconnect/sdk-zephyr` commit `fd9204a02d52630660ce8d729945a4dd743feabf` (commit referenced by the `ncs-v3.3.0` tag object `10cfdb6247b7659a6fa66b4d064ed9dcbfcb46b4`) | The DW3000 driver's newest DWM3001CDK support commit reports successful module-ID readback with NCS 3.3. |
| Zephyr SDK | `0.17.4` | This is the `SDK_VERSION` declared by the pinned Zephyr tree. |
| DW3000 Zephyr driver | `br101/zephyr-dw3000-decadriver` commit `6208d99f933872bf024a653b0c9e8bef92349162` | Exact upstream `master` tip inspected on 2026-07-20; contains DWM3001CDK support merged by PR 44. |
| DW3000 driver payload | Qorvo `dwt_uwb_driver` 08.02.02 from `DW3_QM33_SDK_1.0.2.zip` | Vendored by the pinned br101 module. |

The br101 repository has no CI configuration and its README does not name a
Zephyr revision. Commit `93a6194be7e6dc10d1d21c6c0c7c8bd298f6cada`
is the repository's explicit compatibility evidence: it says the DWM3001CDK
overlay compiled and returned the module ID with NCS 3.3. The workspace therefore
pins the exact Zephyr commit behind NCS 3.3.0 rather than chasing upstream Zephyr.

## Host tools

| Tool | Version |
|---|---|
| Python | 3.12 |
| pyserial | 3.5 |
| west | 1.5.0 |
| CMake | 3.31.10 |
| Ninja | 1.13.0 |
| nrfutil | 8.2.0 (`c910332`) |

CMake 4.4.0 was initially discovered via pip during setup but is incompatible
with the pinned Zephyr tree's `FindZephyr-sdk.cmake` expression syntax. It was
replaced with the working 3.31.10 pin before the first build.

## Rollback artifact

- Path: `../../260714_dwm3001_tunable_batched_ranging/dwm3001_firmware/Output/Common/Exe/DWM3001CDK-DW3_QM33_SDK_CLI-FreeRTOS.hex`
- Size: 922157 bytes
- SHA-256: `E7CE0877277176F33F23FEC34120D3A87EDD59393E33EAA56E5AB987599600B6`

## Bench mapping

| Board | J-Link VCOM | Initial firmware | First Zephyr flash full erase |
|---|---|---|---|
| `760200606` | COM5 | FiRa rollback firmware | Yes, 2026-07-20 |
| `760223921` | COM8 | FiRa rollback firmware | Yes, 2026-07-20 |

## Host connection

- UNO Q: `arduino@192.168.8.215`
- SSH key: workspace `.secrets/ssh/unoq_wifi_ed25519`
