# Live Processing Split

The UNO Q now runs the thin live agent. It owns USB CDC acquisition, validated
record framing, the local LED-matrix/health view, and best-effort UDP delivery.
It does not write raw captures or run DSP.

The Windows machine runs the processing server. It owns the existing pipeline,
dashboard, REST API, and WebSocket stream. Its runtime data directory currently
contains only operational settings/calibration state; live plots remain bounded
in memory.

## Windows server

Build the service, then register its direct executable as a persistent logon
task. Task Scheduler detaches it from development shells and restarts it after
a laptop reboot:

```powershell
.\tools\build-windows-server.ps1
.\tools\install-windows-server-task.ps1
```

From an Administrator PowerShell, allow inbound UDP port `7878` and TCP port
`8080` for any Mobile Hotspot profile:

```powershell
.\tools\install-windows-firewall.ps1
```

## UNO Q agent

Set the server's LAN address in `/etc/default/heimdall-agent`:

```sh
HEIMDALL_SERVER=192.168.8.10:7878
```

When using the current rootless `@reboot` crontab deployment instead of the
systemd unit, put the same line in `/home/arduino/.config/heimdall-agent.env`.
Then install/restart `heimdall.service`, or restart the rootless launcher. The agent health dashboard remains at
`http://<uno-q-address>:8080/`; its JSON snapshot is at `/api/health`.

### Current portable-demo network

As of 2026-07-30, the Windows Mobile Hotspot gateway is `192.168.137.1` and
the UNO Q received `192.168.137.98` by DHCP on the `Brahmand` hotspot. Use:

```sh
HEIMDALL_SERVER=192.168.137.1:7878
```

The UNO Q's existing home-Wi-Fi profile remains configured as a fallback.
The hotspot lease can change, so verify the UNO Q address before each demo.

### Current infrastructure network

As of 2026-07-31, the low-latency deployment uses the `Ullas` access point:

```text
UNO Q agent:       192.168.8.215:8080
Windows UDP:       192.168.8.101:7878
Windows dashboard: http://192.168.8.101:8080
```

Set `/home/arduino/.config/heimdall-agent.env` to:

```sh
HEIMDALL_SERVER=192.168.8.101:7878
```

An 8-second direct-network probe measured 22.1 ms p95 request latency with one
startup outlier. Equivalent Windows Mobile Hotspot probes measured 113-122 ms
p95 with recurring 190-210 ms pauses. Browser, WebSocket, server queue, USB,
and power-management checks isolated those pauses to the hotspot network path.
Use infrastructure Wi-Fi, a travel router, or a separate access-point adapter
when smooth live plots are required.

## UDP contract

The agent sends versioned `HML1` datagrams. A complete validated USB record is
split into at most 1,152-byte payload fragments. The Windows server discards a
record if any fragment is absent after 100 ms. There is intentionally no
acknowledgement, retransmission, durable spool, or archive path: current live
data always wins over delayed data.

## Windows latency tuning

The live server uses a 2 ms / 64 KiB UDP microbatch, strict topic demand, and a
16 ms latest-only WebSocket coalescer keyed by topic, directed link, and ranging
kind. Each publish interval is carried as one `HMB1` binary batch of complete
`HMT1` envelopes, reducing browser message callbacks without losing the newest
value for any visible link. Distance and health demand do not run CIR alignment, waterfall, or FFT
work. The dashboard coalesces state publication to one browser animation frame,
caches distance display variants, and keeps a fixed 160-sample live plot window.

`GET /api/health` exposes `live` metrics for UDP reassembly, queue depth and
wait, processing duration, WebSocket coalescing, and WebSocket send duration.
After the 2026-07-30 tuning deployment, a live five-node sample reported zero
queue drops, 0.032 ms average queue wait, 0.392 ms average processing time,
0.037 ms average WebSocket send time, and zero expired records.

The Windows build stages under `target/windows-build/`; the task installer stops
the old task, copies the staged binary to `target/windows-server/`, waits for
ports 7878/8080 to be released, and starts the replacement. This avoids trying
to overwrite a running Windows executable.
