"""Long-running Linux CDC ingestion service."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import termios
import time
import tty

from .ingest import IngestSession
from .storage import HeimdallStorage


def run(
    device: Path,
    database: Path,
    archive_root: Path,
    metadata: dict[str, object],
    reconnect_seconds: float = 2.0,
    rotate_bytes: int = 64 * 1024 * 1024,
) -> None:
    storage = HeimdallStorage(database)
    run_id = storage.start_run({**metadata, "mode": "live", "device": str(device)})
    try:
        while True:
            descriptor = None
            session = None
            previous = None
            try:
                descriptor = os.open(device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
                previous = termios.tcgetattr(descriptor)
                tty.setraw(descriptor)
                session = IngestSession(
                    storage, run_id, str(device), archive_root, rotate_bytes
                )
                while True:
                    readable, _, _ = select.select([descriptor], [], [], 1.0)
                    if not readable:
                        continue
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        raise OSError("CDC device returned end of stream")
                    session.feed(chunk)
            except (OSError, termios.error):
                if session is not None:
                    session.close("disconnected")
                time.sleep(reconnect_seconds)
            finally:
                if descriptor is not None:
                    if previous is not None:
                        try:
                            termios.tcsetattr(descriptor, termios.TCSANOW, previous)
                        except termios.error:
                            pass
                    os.close(descriptor)
    except KeyboardInterrupt:
        if session is not None and not session.closed:
            session.close("stopped")
        storage.close_run(run_id, "stopped")
    except BaseException:
        if session is not None and not session.closed:
            session.close("failed")
        storage.close_run(run_id, "failed")
        raise
    finally:
        storage.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("device", type=Path)
    parser.add_argument("database", type=Path)
    parser.add_argument("archive_root", type=Path)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--reconnect-seconds", type=float, default=2.0)
    parser.add_argument("--rotate-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    run(
        args.device,
        args.database,
        args.archive_root,
        metadata,
        args.reconnect_seconds,
        args.rotate_bytes,
    )


if __name__ == "__main__":
    main()
