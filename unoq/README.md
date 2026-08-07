# UNO Q Runtime

This directory contains the Heimdall runtime shared by the UNO Q Linux agent
and the Windows ARM64 processing server. Its primary seams are:

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

Protected captures retain complete post-trigger USB records for a requested
1-60 second interval. Use `POST /api/v1/clips` to arm a capture,
`GET /api/v1/clips` to inspect progress, and `GET` or `DELETE
/api/v1/clips/{id}` to download or remove a completed ZIP.

## Camera-assisted collection

The optional Logitech camera path runs only in `heimdall-service server` on the
Windows Snapdragon host. The UNO Q `agent` and monolithic Linux `serve` command
do not open a camera. Enable the detected Brio 101 in the foreground with:

```powershell
.\tools\run-windows-server.ps1 -CameraDevice 'Brio 101' -Ffmpeg 'C:\Users\anand\scoop\apps\ffmpeg\current\bin\ffmpeg.exe'
```

For the logon task, pass the same `-CameraDevice` and `-Ffmpeg` arguments to
`install-windows-server-task.ps1`. Omitting `-CameraDevice` keeps camera support
disabled. The pinned FFmpeg 8.0 x86-64 archive and checksum are documented in
`../tools/README.md`; it runs under Windows ARM64 emulation.

The Training tab requires a participant name and explicit consent before it
starts a continuous session. It records fragmented H.264 video at 1280x720 and
30 fps, publishes a latest-only 2 fps JPEG preview, and guides an operator
through named `Empty`, front, and rear seat calibration prompts. Named prompts,
not image left/right coordinates, define the seat mapping. Stable intervals
create tagged 10-second UWB clips and host-timestamped camera events; transitions
are not captured as training clips. Video, calibration JPEGs, event manifests,
and FFmpeg logs remain under `data/camera-sessions/` until manual deletion.

Camera API lifecycle routes are under `/api/v1/camera/` with `/api/camera/`
compatibility aliases. An active UWB clip records the camera session ID and its
host-clock trigger offset in `metadata.json`. These timestamps align Windows
video processing with Windows UWB receipt; they are not source-frame or
cross-machine transport timestamps.

Run the live desktop and phone acceptance audit from `dashboard/`:

```sh
npm run audit:live -- "http://192.168.8.215:8080" /tmp/heimdall-tab-audit
```

The audit visits all eight tabs at 1440x1000 and 390x844, writes screenshots,
and fails on browser exceptions, invalid canvases, synthetic content, offline
state, or missing active-link data.
