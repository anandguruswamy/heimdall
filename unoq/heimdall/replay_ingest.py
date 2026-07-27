"""Replay a rotated raw archive into a fresh canonical event database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ingest import replay_archive
from .storage import HeimdallStorage


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive_directory", type=Path)
    parser.add_argument("database", type=Path)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--chunk-size", type=int, default=65536)
    args = parser.parse_args()

    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    storage = HeimdallStorage(args.database)
    run_id = storage.start_run(
        {**metadata, "mode": "replay", "archive": str(args.archive_directory)}
    )
    try:
        stats = replay_archive(
            args.archive_directory, storage, run_id, args.chunk_size
        )
        storage.close_run(run_id)
    except BaseException:
        storage.close_run(run_id, "failed")
        raise
    finally:
        storage.close()
    print(json.dumps(stats, sort_keys=True))


if __name__ == "__main__":
    main()
