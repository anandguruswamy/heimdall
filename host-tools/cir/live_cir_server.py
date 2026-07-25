#!/usr/bin/env python3
"""Live 50 Hz CIR server: serial CIR records -> SSE -> browser canvases."""

from __future__ import annotations

import argparse
import json
import struct
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import serial

TAPS = 64
WINDOW = 50


def parse_cir(line: str) -> dict | None:
    if not line.startswith("CIR,"):
        return None
    fields = {}
    for field in line[4:].strip().split(","):
        key, value = field.split("=", 1)
        fields[key] = value
    taps = [[int(a), int(b)] for a, b in (x.split(":") for x in fields.pop("taps").split(";"))]
    if len(taps) != TAPS:
        raise ValueError(f"expected {TAPS} taps, got {len(taps)}")
    return {
        "seq": int(fields["seq"]),
        "rx_ts": int(fields["rx_ts"]),
        "cfo_raw": int(fields["cfo_raw"]),
        "fp": int(fields["fp"]),
        "taps": taps,
        "host_time": time.time(),
    }


class LiveState:
    def __init__(self) -> None:
        self.frames: deque[dict] = deque(maxlen=WINDOW)
        self.condition = threading.Condition()
        self.total = 0
        self.errors = 0

    def add(self, frame: dict) -> None:
        with self.condition:
            self.frames.append(frame)
            self.total += 1
            self.condition.notify_all()


def serial_reader(port: str, state: LiveState) -> None:
    while True:
        try:
            with serial.Serial(port, 115200, timeout=0.25) as uart:
                uart.reset_input_buffer()
                print(f"connected to {port}", flush=True)
                while True:
                    magic = uart.read(4)
                    if len(magic) < 4:
                        continue
                    if magic not in (b"CIR0", b"CIR1", b"CIR2"):
                        continue
                    payload_len = 281 if magic == b"CIR2" else (277 if magic == b"CIR1" else 276)
                    payload = uart.read(payload_len)
                    if len(payload) != payload_len:
                        continue
                    seq, rx_ts, cfo_raw, fp = struct.unpack_from("<IQiI", payload)
                    agc_state = payload[20] if magic in (b"CIR1", b"CIR2") else None
                    rssi_q8_8 = struct.unpack_from("<h", payload, 21)[0] if magic == b"CIR2" else None
                    fp_power_q8_8 = struct.unpack_from("<h", payload, 23)[0] if magic == b"CIR2" else None
                    tap_offset = 25 if magic == b"CIR2" else (21 if magic == b"CIR1" else 20)
                    taps = [list(x) for x in struct.iter_unpack("<hh", payload[tap_offset:])]
                    state.add({"seq": seq, "rx_ts": rx_ts, "cfo_raw": cfo_raw,
                               "fp": fp, "agc_state": agc_state, "rssi_q8_8": rssi_q8_8,
                               "fp_power_q8_8": fp_power_q8_8, "taps": taps,
                               "host_time": time.time()})
        except (serial.SerialException, OSError) as exc:
            print(f"{port} unavailable ({exc}); retrying", flush=True)
            time.sleep(1.0)


class Handler(BaseHTTPRequestHandler):
    state: LiveState
    html_path: Path

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path == "/index.html":
            html = self.html_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
            return
        if self.path != "/events":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        cursor = 0
        try:
            while True:
                with self.state.condition:
                    if self.state.total == cursor:
                        self.state.condition.wait(timeout=2.0)
                    frames = list(self.state.frames)
                    total = self.state.total
                if total != cursor:
                    start = max(0, len(frames) - (total - cursor))
                    for frame in frames[start:]:
                        payload = json.dumps(frame, separators=(",", ":"))
                        self.wfile.write(f"data:{payload}\n\n".encode())
                    self.wfile.flush()
                    cursor = total
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, help="receiver serial port, e.g. COM8")
    parser.add_argument("--http-port", type=int, default=8765)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--html", type=Path, default=Path(__file__).with_name("live-cir.html"))
    args = parser.parse_args()

    state = LiveState()
    threading.Thread(target=serial_reader, args=(args.port, state), daemon=True).start()
    Handler.state = state
    Handler.html_path = args.html.resolve()
    server = ThreadingHTTPServer((args.bind, args.http_port), Handler)
    print(f"live CIR view: http://{args.bind}:{args.http_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
