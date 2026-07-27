"""Byte-exact replay through the production USB stream parser."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from .protocol import Record, StreamParser


def replay_bytes(data: bytes, chunk_sizes: Iterable[int] = (4096,)) -> list[Record]:
    parser = StreamParser()
    records: list[Record] = []
    offset = 0
    sizes = tuple(chunk_sizes)
    if not sizes or any(size <= 0 for size in sizes):
        raise ValueError("chunk sizes must be positive")
    index = 0
    while offset < len(data):
        size = sizes[index % len(sizes)]
        records.extend(parser.feed(data[offset : offset + size]))
        offset += size
        index += 1
    return records


def main() -> None:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("capture", type=Path)
    args = argument_parser.parse_args()
    records = replay_bytes(args.capture.read_bytes())
    print(f"records={len(records)}")


if __name__ == "__main__":
    main()
