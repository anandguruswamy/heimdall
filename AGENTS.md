# Heimdall Workspace Notes

Heimdall is the execution project for a multi-node UWB sensing system:

```text
UWB nodes <-> UWB beacon/ranging/CIR backhaul <-> gateway UWB node
                                                   |
                                                   | USB CDC
                                                   v
                                             Arduino UNO Q
                                             fusion hub
```

This folder is intentionally standalone. Dated projects elsewhere in the
workspace are reference material only and should not be modified as part of
Heimdall work.

## Operating rules

- The UWB radio is the node-to-node data plane.
- One DWM3001 gateway node is physically attached to the UNO Q over its native
  USB CDC connection.
- The UNO Q owns ingestion, validation, archival, fusion, and the dashboard.
- Firmware must keep radio timing independent from USB backpressure.
- Every record has a protocol version, node identity, round identity, sequence
  information, and integrity check.
- Capture/replay is a first-class adapter for development and testing.

## Build and deployment workflow

- Build the UNO Q Rust service on this Windows development machine. Use the
  cached cross-compilation toolchain documented in `tools/README.md` and target
  `aarch64-unknown-linux-gnu`.
- Do not run `cargo build` or `cargo build --release` on the UNO Q. The UNO Q
  is a deployment target, not the Rust build host; transfer the verified
  cross-compiled binary there.
- Arduino sketches are the exception: compile and upload UNO Q MCU sketches
  on the UNO Q with `arduino-cli`, using `arduino:zephyr:unoq`.
- After deploying a Rust service or boot-time helper, verify the running
  process, its input/output device or socket, and its boot-start configuration
  on the UNO Q.

## UNO Q access

- Connect to the deployed UNO Q over SSH with:
  `ssh -i C:\Users\anand\Homelab\Heimdall\.secrets\ssh\unoq_wifi_ed25519 arduino@192.168.8.215`
- For the current portable-demo hotspot (2026-07-30), the UNO Q is
  `192.168.137.98`; the laptop hotspot gateway is `192.168.137.1`.
  This DHCP address is not stable and must be verified before a demo.
- Do not add the private key or other secrets to source control.

## Hardware assumptions

- DWM3001CDK nodes with DW3110 radios and nRF52833 MCUs.
- Gateway connection uses the DWM3001 J20/native USB path, not J9 J-Link.
- The validated deployment has two active nodes. Beacon v1 defines exactly N
  occupied superslots; Gate H4 should use three occupied slots for three nodes.
  The older seven-slot/reserved-empty concept conflicts with the current
  contract and must not be introduced without deliberately revising the
  contract and configuration model.
- The UNO Q Linux side is Debian aarch64 and is reachable as `chinny`.

## Safety

- Do not alter the frozen dated experiments while using their artifacts here.
- Do not put generated Zephyr build trees, west dependencies, captures, or
  secrets under source control.
- Treat node/gateway firmware changes as radio changes: record the build
  profile, PHY settings, board ID, and test result in `STATUS.md`.

## Collaboration workflow

- Before a build, deployment, upload, restart, network reconfiguration, or
  other operation likely to take more than a few seconds, state the exact
  operation and expected duration. Report completion as soon as the requested
  action is verified; do not silently wait for optional diagnostics or cleanup.
- Ask before an operation likely to take more than 30 seconds unless the user
  explicitly requested a build, deployment, or similarly long-running action.
- Prefer the smallest verification that proves the requested result. Defer
  optional analysis and cleanup until after reporting that result.
- Before starting a requested task, briefly assess whether it should continue
  in the current conversation or start in a new session, and tell the user
  which is more appropriate.
- Prefer the current session for sequential work that depends on its hardware,
  repository, or decision context. Recommend a new session for an unrelated
  task or when a clean context would materially reduce risk.
- After making repository changes, consider whether a commit or push would be
  useful. If so, tell the user and ask for approval before committing or
  pushing; never commit or push solely based on this assessment.
- When adding a host tool or firmware dependency, update the tracked tooling
  manifest with its version, architecture, download link, and checksum when
  available. Retain a copy of the installer or archive in the ignored local
  `tools/installers/` cache when redistribution is permitted.
- Before downloading or installing a host tool, inspect
  `tools/installers/` for a matching version and host architecture. Prefer a
  cached file whose checksum matches `tools/README.md`; download from the
  documented official link only when no verified cached file is available.
