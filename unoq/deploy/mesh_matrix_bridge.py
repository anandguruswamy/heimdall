#!/usr/bin/env python3
"""Forward live Heimdall topology status to the UNO Q MCU matrix sketch."""

import json
import socket
import time
import urllib.error
import urllib.request

API_URL = "http://127.0.0.1:8080/api/topology"
ROUTER_SOCKET = "/var/run/arduino-router.sock"
INTERVAL_SECONDS = 0.5
FRESH_SECONDS = 2.5


def topology():
    with urllib.request.urlopen(API_URL, timeout=1.0) as response:
        return json.load(response)


def matrix_line(document):
    config = document.get("config") or {}
    node_count = int(config.get("n_nodes", 0))
    gateway = int(config.get("node_id", 0))
    links = {
        (int(link["from"]), int(link["to"])): link
        for link in document.get("links", [])
    }
    latest = [
        float(link["latest_event_s"])
        for link in links.values()
        if link.get("latest_event_s") is not None
    ]
    newest = max(latest, default=None)
    levels = [0] * 5
    connected = 1 if config else 0

    for peer in range(max(node_count, 0)):
        if peer == gateway:
            continue
        link = links.get((peer, gateway))
        if not link or newest is None or link.get("latest_event_s") is None:
            continue
        if newest - float(link["latest_event_s"]) > FRESH_SECONDS:
            continue
        connected += 1
        if peer < len(levels):
            cir = link.get("latest_cir") or {}
            correlation = max(0.0, min(1.0, float(cir.get("correlation", 0.0))))
            levels[peer] = int(round(correlation * 7.0))

    return ",".join([str(min(9, connected)), *(str(level) for level in levels)]) + "\n"


def send(line, message_id):
    def pack_string(value):
        encoded = value.encode("utf-8")
        if len(encoded) >= 32:
            raise ValueError("router command string is too long")
        return bytes((0xA0 | len(encoded),)) + encoded

    command = (
        bytes((0x94, 0x00, message_id))
        + pack_string("mesh_update")
        + bytes((0x91,))
        + pack_string(line)
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1.0)
        connection.connect(ROUTER_SOCKET)
        connection.sendall(command)
        try:
            connection.recv(128)
        except socket.timeout:
            pass


def main():
    message_id = 0
    while True:
        try:
            send(matrix_line(topology()), message_id)
            message_id = (message_id + 1) % 128
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
            pass
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
