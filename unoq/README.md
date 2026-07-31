# UNO Q Runtime

This directory contains the Linux-side Heimdall runtime. Its primary seams are:

```text
CDC adapter -> frame decoder -> canonical observation stream -> fusion/storage
capture replay -----------------------------------------------^
```

The Rust workspace provides strict USB/beacon protocol handling, raw-first
archive and replay, ranging/CIR/FFT processing, REST and FlatBuffers WebSocket
APIs, and the embedded Svelte dashboard. The Python implementation under
`heimdall/` remains the reference adapter.

On the Windows ARM64 development host, install the pinned
`1.93.1-aarch64-pc-windows-gnullvm` Rust toolchain with its
`aarch64-unknown-linux-gnu` target and extract the documented Zig 0.15.2 archive
under `tools/installers/windows-arm64/`, then use the tracked wrappers:

```powershell
# Runs host-side Rust tests on Windows ARM64.
.\tools\test-host.ps1

# Compiles and links test executables for the UNO Q, without trying to run them.
.\tools\test-linux-arm64.ps1

# Produces the deployable Linux ARM64 binary.
.\tools\build-linux-arm64.ps1 -Release
```

The wrappers use the cached Zig executable at
`tools/installers/windows-arm64/zig-aarch64-windows-0.15.2/zig.exe`. If the
archive is extracted elsewhere, set `HEIMDALL_ZIG` to its absolute `zig.exe`
path for that PowerShell session. Do not build the Rust service on the UNO Q.

The deployable binary is written to
`target/aarch64-unknown-linux-gnu/release/heimdall-service`.

Production deployment uses `deploy/run-heimdall.sh`. A system unit is provided
at `deploy/heimdall.service`; installing or enabling it requires root. On the
current rootless UNO Q deployment, the launcher is invoked by the `arduino`
user's `@reboot` crontab entry instead.

The dashboard and API listen on port 8080. Runtime data is stored outside the
repository under `/home/arduino/heimdall-data`.

Protected captures retain complete USB records for 30 seconds before and after
the trigger. Use `POST /api/v1/clips` to arm a capture, `GET /api/v1/clips` to
inspect progress, and `GET` or `DELETE /api/v1/clips/{id}` to download or remove
a completed ZIP.

Run the live desktop and phone acceptance audit from `dashboard/`:

```sh
npm run audit:live -- "http://192.168.8.215:8080" /tmp/heimdall-tab-audit
```

The audit visits all eight tabs at 1440x1000 and 390x844, writes screenshots,
and fails on browser exceptions, invalid canvases, synthetic content, offline
state, or missing active-link data.
