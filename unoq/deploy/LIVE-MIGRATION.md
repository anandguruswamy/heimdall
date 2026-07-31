# Live Processing Split

The UNO Q now runs the thin live agent. It owns USB CDC acquisition, validated
record framing, the local LED-matrix/health view, and best-effort UDP delivery.
It does not write raw captures or run DSP.

The Windows machine runs the processing server. It owns the existing pipeline,
dashboard, REST API, and WebSocket stream. Its runtime data directory currently
contains only operational settings/calibration state; live plots remain bounded
in memory.

## Windows server

Run the service on the Windows machine with a UDP listener and dashboard HTTP
listener:

```powershell
.\tools\run-windows-server.ps1
```

Allow inbound UDP port `7878` and TCP port `8080` on the Windows firewall.

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

## UDP contract

The agent sends versioned `HML1` datagrams. A complete validated USB record is
split into at most 1,152-byte payload fragments. The Windows server discards a
record if any fragment is absent after 100 ms. There is intentionally no
acknowledgement, retransmission, durable spool, or archive path: current live
data always wins over delayed data.
