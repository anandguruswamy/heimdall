"""Long-running Linux CDC ingestion service."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import struct
import termios
import threading
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
    database.parent.mkdir(parents=True, exist_ok=True)
    lock = database.with_name(database.name + ".lock").open("a+b")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError(f"another ingest process owns {database}")
    storage = HeimdallStorage(database)
    storage.recover_interrupted(archive_root)
    run_id = storage.start_run({**metadata, "mode": "live", "device": str(device)})
    stop_requested = threading.Event()
    prior_sigterm = signal.signal(
        signal.SIGTERM, lambda _signum, _frame: stop_requested.set()
    )
    try:
        while not stop_requested.is_set():
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
                while not stop_requested.is_set():
                    readable, _, _ = select.select([descriptor], [], [], 1.0)
                    if not readable:
                        continue
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        raise OSError("CDC device returned end of stream")
                    session.feed(chunk)
                _complete_pending_record(descriptor, session)
            except (OSError, termios.error):
                if session is not None:
                    session.close("disconnected")
                stop_requested.wait(reconnect_seconds)
            finally:
                if descriptor is not None:
                    if previous is not None:
                        try:
                            termios.tcsetattr(descriptor, termios.TCSANOW, previous)
                        except termios.error:
                            pass
                    os.close(descriptor)
        if session is not None and not session.closed:
            session.close("stopped")
        storage.close_run(run_id, "stopped")
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
        signal.signal(signal.SIGTERM, prior_sigterm)
        storage.close()
        lock.close()


def _complete_pending_record(descriptor: int, session: IngestSession) -> None:
    deadline = time.monotonic() + 2.0
    while session.parser.buffer and time.monotonic() < deadline:
        buffered = len(session.parser.buffer)
        if buffered < 12:
            needed = 12 - buffered
        else:
            payload_length = struct.unpack_from("<H", session.parser.buffer, 6)[0]
            needed = 16 + payload_length - buffered
        if needed <= 0:
            return
        readable, _, _ = select.select([descriptor], [], [], deadline - time.monotonic())
        if not readable:
            return
        chunk = os.read(descriptor, needed)
        if not chunk:
            return
        session.feed(chunk)


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
