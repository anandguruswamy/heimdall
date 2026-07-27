# Gate H4 N=3 Handoff

## Checkpoint

- Start from commit `b0bcd0a` (`Validate Gate H3 UNO Q ingest`) on `main`.
- Gate H3 is complete. The N=2 radio runtime, gateway USB export, raw archive,
  SQLite persistence, crash recovery, and exact replay are hardware-validated.
- The host baseline is 69 passing tests:
  `python -m unittest discover -s tests`.
- Gate H4 is an N=3 radio change. Record every build profile, configuration,
  image hash, board binding, PHY setting, and hardware result in `STATUS.md` and
  `firmware/radio/BRINGUP-NOTES.md`.
- Do not modify the frozen dated projects outside this repository.

## Immediate Goal

Run three board-bound DWM3001CDKs at N=3, M=1, initially retaining the proven
10,000 us slot. Require continuous modulo-3 ownership, two peer receptions and
six directed observations per complete cycle, bounded callback/assembly time,
and replay-equivalent gateway ingestion.

Do not reduce the slot to the model floor during first bring-up. Existing N=2
hardware callback maxima are 2.4-2.9 ms, substantially above the nominal model
processing estimate.

## Hardware And Access

- UNO Q host: `chinny`, Debian aarch64, user `arduino`, address
  `192.168.8.215`.
- Interactive access from the repository root:

```powershell
ssh -t -i .secrets/ssh/unoq_wifi_ed25519 arduino@192.168.8.215
```

- Non-interactive access: omit `-t` and use `-T -o BatchMode=yes`.
- Node 0/gateway: J-Link `760223921`, FICR `75561606:12A31510`.
- Node 1/fixed: J-Link `760197419`, FICR `71414197:EAD43288`.
- Gate H4 candidate node 2 is physical label 3, J-Link `760197416`, last seen as
  Windows `COM10`. Its FICR identity and radio acceptance are not recorded.
- Other inventoried spares are `760200606` and `760223924`.
- Do not use `deployment/node-roster.example.yaml` as the H4 roster; its old
  IDs, PHY, and schema do not describe the validated lab deployment.
- J9 is the programming/J-Link path. J20 is the gateway native USB CDC path.
- Stable gateway CDC link:
  `/dev/serial/by-id/usb-Open_UWB_Heimdall_Gateway_7556160612A31510-if00`.
- During Gate H3, UNO Q mappings were board 2 J-Link `/dev/ttyACM0`, board 1
  J-Link `/dev/ttyACM1`, and gateway native CDC `/dev/ttyACM2`. These ACM
  numbers may change when node 2 is attached; use serial/by-id links.

Identify candidate node 2 one board at a time through J9. On `chinny`, read its
nRF52833 FICR words with:

```bash
printf 'connect\nmem32 0x10000060,2\nq\n' |
  JLinkExe -device nRF52833_xxAA -if SWD -speed 4000 \
    -SelectEmuBySN 760197416
```

Confirm `DEVICEID[0]` is the low word and `DEVICEID[1]` the high word against an
existing board before updating `deployment/node-roster.lab.yaml`. Using 16385
DTU for both antenna delays is permitted only as an explicitly uncalibrated
bring-up value. Calibration is not a schedule blocker, but uncalibrated
timestamps must not be presented as accurate ranges.

## UNO Q State

- The H3 runtime was copied, not cloned, to
  `/home/arduino/projects/heimdall/`; GitHub authentication is not installed on
  `chinny`, and an HTTPS clone of the private repository failed.
- No ingest service was running at the end of Gate H3.
- H3 evidence remains under `/home/arduino/projects/heimdall/` in `h3-data/`,
  `h3-acceptance/`, `h3-final/`, and `h3-final2/`. Do not delete it unless the
  user explicitly approves cleanup.
- The final H3 acceptance evidence is summarized in `STATUS.md`. The raw and
  replay databases passed integrity checks and exact digest comparison.

## Confirmed Firmware Blockers

The model, schedule helpers, report packer, and host canonical ingest are mostly
N-generic. The active runtime is not. Address these before flashing N=3.

1. `firmware/radio/app/src/beacon_runtime.c` has a
   `BUILD_ASSERT(HEIMDALL_N_NODES == 2U)` near the runtime constants.
2. `make_observation()` and the shared subreport buffer retain only one peer.
   `rx_ok_cb()` clears all subreport pointers after every reception. N=3 must
   retain one current subreport for each of two peers until local transmission.
3. `validate_relayed_subreport()` requires exactly one subreport, at most one
   subreport of pooled bytes, and a bitmap containing only the local node. N=3
   reports contain two 296-byte subreports and a 592-byte pool.
4. `cycle_summary_handler()` writes misses to
   `peer_m0_miss[1U - CONFIG_HEIMDALL_NODE_ID]`. Track reception by source and
   fill independent miss entries for both gateway peers.
5. Every valid RX cancels the master watchdog, while a new watchdog is installed
   only after master TX. If node 0 receives node 1 and node 2 is absent, the
   chain can stall. Scheduling and fallback must compute the local node's next
   owned `k` from any valid reception and recover from a missing intermediate
   owner.
6. The runtime currently implements only `m=0`. This is acceptable for the
   derived N=3 profile because M remains 1, but keep the limitation explicit.
7. `unoq/heimdall/inspect_capture.py` reports N=2 odd/even ownership. Replace it
   with `k % n_nodes == source_node_id` for received frames and
   `k % n_nodes == hello.node_id` for gateway TX.
8. Firmware may emit fewer CIR taps near accumulator boundaries, while
   `unoq/heimdall/beacon.py` requires the configured full subreport size. More
   links increase exposure to this existing contract risk; reconcile or prove
   the full-size invariant before H4 acceptance.

Already N-generic components that should be preserved include
`beacon_schedule.c`, `beacon_report.c`, `beacon_report.h`, the configuration
model, USB framing, pooled-report host reassembly, canonical observations,
storage, and replay verification.

## Slot Model Decision

Beacon v1 defines a cycle as exactly N occupied superslots. The older workspace
description of seven deterministic slots with unused reserved slots conflicts
with `contracts/beacon-v1.md` and the implemented configuration model. For H4,
use three occupied superslots unless the protocol contract and model are first
changed deliberately. Do not silently add four empty superslots.

## Derived N=3 Profile

Changing only `network.n_nodes` from 2 to 3 in the current configuration and
retaining the 10,000 us slot derives:

| Value | N=3 result |
|---|---:|
| Subreport bytes | 296 |
| Pooled report bytes | 592 |
| M | 1 |
| Frame payload bytes | 592 |
| Full frame bytes | 625 |
| Airtime | 935.35 us |
| Model RX processing | 530.33 us |
| Model TX write | 161.25 us |
| RX floor | 1,800 us |
| Assembly floor | 2,200 us |
| Superslot | 10,000 us |
| Cycle | 30,000 us |
| Per-link rate | 33.333 Hz |
| Gateway USB per cycle | 2,007 B |
| Gateway USB rate | 66,900 B/s |
| Config hash | `0xC8CF` / 51,407 |

Reproduce the derivation from the repository root with:

```powershell
python -c "import json,sys; sys.path.insert(0,r'tools/config'); import heimdall_config as h; c=json.load(open(r'deployment/beacon-config.example.json')); c['network']['n_nodes']=3; d=h.derive(c); print(json.dumps(d['derived'],indent=2)); print(h.check_invariants(d))"
```

Create a separate tracked N=3 configuration rather than overwriting historical
N=2 evidence. Pass its absolute path as `HEIMDALL_CONFIG_FILE` for every image.
The mathematical 2,200 us floor is not hardware-qualified and would exceed the
configured USB budget at that cycle rate. The first safe hardware profile is
the retained 10,000 us slot.

## Build And Flash

All three images must use the same N=3 configuration and hash. Each image is
bound at build time to node ID, FICR low/high words, and TX/RX antenna delays.
Build from `firmware/` with `-p always`, unique N=3 build directories, and an
explicit absolute configuration path.

Node 2 follows this pattern:

```powershell
west build -p always --no-sysbuild -b nrf52833dk/nrf52833 radio/app `
  -d build-beacon-runtime-n3-node2 -- `
  "-DHEIMDALL_CONFIG_FILE=C:/Users/anand/Homelab/Heimdall/deployment/beacon-config.n3.json" `
  "-DOVERLAY_CONFIG=C:/Users/anand/Homelab/Heimdall/firmware/radio/app/runtime.conf" `
  "-DEXTRA_DTC_OVERLAY_FILE=C:/Users/anand/Homelab/Heimdall/firmware/radio/app/boards/nrf52833dk_nrf52833_spim3.overlay" `
  "-DCONFIG_HEIMDALL_NODE_ID=2" `
  "-DCONFIG_HEIMDALL_EXPECTED_DEVICE_ID_LOW=<DEVICEID0>" `
  "-DCONFIG_HEIMDALL_EXPECTED_DEVICE_ID_HIGH=<DEVICEID1>" `
  "-DCONFIG_HEIMDALL_TX_ANTENNA_DELAY_DTU=16385" `
  "-DCONFIG_HEIMDALL_RX_ANTENNA_DELAY_DTU=16385"
```

Rebuild nodes 0 and 1 against the same file. Node 0 uses
`runtime-gateway.conf` plus both the SPIM3 and native USB overlays; nodes 1 and
2 use `runtime.conf` plus SPIM3. Flash non-master nodes first and gateway/master
last so mixed N=2/N=3 operation is brief. Use full board-bound image hashes in
the hardware record.

Copy a built HEX to `chinny` and flash through J9 with:

```bash
printf 'connect\nloadfile /tmp/heimdall-n3-node2.hex\nr\ng\nq\n' |
  JLinkExe -device nRF52833_xxAA -if SWD -speed 4000 \
    -SelectEmuBySN 760197416
```

## Required Tests Before Flash

- Keep all existing 69 tests green.
- Add a real N=3 HELLO/config fixture with M=1 and a 625-byte frame.
- Add two-subreport pooled reports with distinct peers, CRCs, bitmap, order,
  and round deltas 1 and 2.
- Verify six directed canonical observations per complete cycle.
- Replace parity assertions with modulo-3 ownership assertions.
- Add cycle summaries with `frames_expected=2` and independent peer misses.
- Exercise all three ownership positions, `k` wrap, missing node 1, missing node
  2 after node 1 was received, predecessor loss, watchdog recovery, duplicate
  identity, and configuration inhibition.

## H4 Hardware Acceptance

- Ownership follows `k % 3 = 0, 1, 2` continuously.
- Complete cycle duration is 30 ms at the initial profile.
- Gateway summaries report 2/2 peer frames except explained boundary/recovery
  events.
- Each complete cycle yields six directed observations: two gateway-local and
  four relayed from node 1 and node 2 reports.
- Every outgoing report has two peer subreports, correct bitmap and deltas, and
  no self bit.
- Record per-node TX attempts/completions, delayed-start failures, timestamp
  errors, timeout recovery, RX/FCS/filter/config/schedule rejects, callback and
  assembly maxima, per-pair observation rate, and collision/miss rate.
- Verify actual 625-byte frames and approximately 66.9 kB/s gateway USB load.
- Detach and reattach the reader to prove USB backpressure does not perturb
  radio timing.
- Run the H3 archive/replay verifier and require matching raw/observation
  digests, segment hashes, SQLite integrity, and explained sequence gaps.
- Deliberately remove node 2 after node 1 remains active and prove watchdog
  recovery; this targets the confirmed N=3 stall defect.

## Recommended Work Order

1. Read the candidate board FICR and update the roster.
2. Add a tracked N=3 configuration and model tests.
3. Add N=3 host fixtures and modulo ownership inspection.
4. Generalize per-peer observation retention and relayed validation.
5. Fix source-specific miss accounting and next-owner/watchdog recovery.
6. Build all three bound images and record hashes/sizes.
7. Flash nodes 1 and 2, then gateway node 0.
8. Validate radio-only schedule and failure recovery before attaching ingestion.
9. Run combined USB ingest and deterministic replay acceptance.
10. Record results and do not proceed to ranging calibration until the schedule
    and observation completeness gates pass.
