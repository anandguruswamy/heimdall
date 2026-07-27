"""Rotating byte-exact raw USB archive."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import time


@dataclass(frozen=True)
class ClosedSegment:
    index: int
    path: Path
    byte_count: int
    sha256: str


class RawArchive:
    def __init__(
        self,
        directory: Path,
        rotate_bytes: int = 64 * 1024 * 1024,
        sync_interval_seconds: float = 1.0,
    ) -> None:
        if rotate_bytes <= 0:
            raise ValueError("rotate_bytes must be positive")
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.rotate_bytes = rotate_bytes
        self.sync_interval_seconds = sync_interval_seconds
        self.index = 0
        self.file = None
        self.path: Path | None = None
        self.byte_count = 0
        self.digest = hashlib.sha256()
        self.last_sync = time.monotonic()
        self.closed_segments: list[ClosedSegment] = []

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self.file is None:
                self._open()
            available = self.rotate_bytes - self.byte_count
            part = view[:available]
            written = self.file.write(part)
            if written != len(part):
                raise OSError("short raw archive write")
            self.digest.update(part)
            self.byte_count += written
            view = view[written:]
            if self.byte_count == self.rotate_bytes:
                self._close_segment()
        if self.file is not None and (
            time.monotonic() - self.last_sync >= self.sync_interval_seconds
        ):
            self.sync()

    def sync(self) -> None:
        if self.file is None:
            return
        self.file.flush()
        sync = getattr(os, "fdatasync", os.fsync)
        sync(self.file.fileno())
        self.last_sync = time.monotonic()

    def close(self) -> list[ClosedSegment]:
        if self.file is not None:
            self._close_segment()
        return list(self.closed_segments)

    def _open(self) -> None:
        self.path = self.directory / f"segment-{self.index:06d}.husb"
        self.file = self.path.open("xb", buffering=0)
        self.byte_count = 0
        self.digest = hashlib.sha256()

    def _close_segment(self) -> None:
        assert self.file is not None and self.path is not None
        self.sync()
        self.file.close()
        self.closed_segments.append(
            ClosedSegment(self.index, self.path, self.byte_count, self.digest.hexdigest())
        )
        self.index += 1
        self.file = None
        self.path = None
        self.byte_count = 0


def iter_archive_bytes(directory: Path, chunk_size: int = 65536):
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    for path in sorted(directory.glob("segment-*.husb")):
        with path.open("rb") as stream:
            while chunk := stream.read(chunk_size):
                yield chunk
