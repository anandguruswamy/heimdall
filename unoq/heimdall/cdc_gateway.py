"""Capture an UNO Q CDC byte stream without terminal transformations."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import select
import termios
import time
import tty

from .protocol import StreamParser


def capture(device: Path, output: Path, duration_seconds: float | None = None) -> None:
    descriptor = os.open(device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    parser = StreamParser()
    deadline = None if duration_seconds is None else time.monotonic() + duration_seconds
    try:
        previous = termios.tcgetattr(descriptor)
        tty.setraw(descriptor)
        with output.open("ab", buffering=0) as capture_file:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                readable, _, _ = select.select([descriptor], [], [], 1.0)
                if not readable:
                    continue
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    continue
                capture_file.write(chunk)
                parser.feed(chunk)
    finally:
        if "previous" in locals():
            termios.tcsetattr(descriptor, termios.TCSANOW, previous)
        os.close(descriptor)


def main() -> None:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("device", type=Path)
    argument_parser.add_argument("output", type=Path)
    argument_parser.add_argument("--seconds", type=float)
    args = argument_parser.parse_args()
    capture(args.device, args.output, args.seconds)


if __name__ == "__main__":
    main()
