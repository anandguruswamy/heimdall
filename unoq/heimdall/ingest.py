"""Raw-first ingestion shared by live CDC input and archive replay."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .archive import RawArchive, iter_archive_bytes
from .canonical import CanonicalProcessor
from .protocol import HelloRecord, ProtocolError, StreamParser
from .storage import HeimdallStorage


class IngestSession:
    def __init__(
        self,
        storage: HeimdallStorage,
        run_id: int,
        source: str,
        archive_root: Path | None,
        rotate_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.storage = storage
        self.connection_id = storage.start_connection(run_id, source)
        self.parser = StreamParser()
        self.processor = CanonicalProcessor()
        self.configuration_id: int | None = None
        self.ingest_index = 0
        self.records_rejected = 0
        self.closed = False
        self.archive = (
            RawArchive(archive_root / f"connection-{self.connection_id:06d}", rotate_bytes)
            if archive_root is not None
            else None
        )

    def feed(self, data: bytes) -> int:
        if self.closed:
            raise RuntimeError("ingest session is closed")
        if self.archive is not None:
            self.archive.write(data)
        accepted = 0
        for record in self.parser.feed(data):
            output = None
            error = None
            try:
                output = self.processor.process(record)
                if output.configuration_changed:
                    assert isinstance(output.decoded, HelloRecord)
                    self.configuration_id = self.storage.add_configuration(
                        self.connection_id, record.sequence, output.decoded
                    )
            except ProtocolError as exc:
                error = str(exc)
                self.records_rejected += 1
            self.storage.add_record(
                self.connection_id,
                self.configuration_id,
                self.ingest_index,
                record,
                output,
                error,
            )
            self.ingest_index += 1
            accepted += 1
        self.storage.commit()
        return accepted

    def close(self, status: str = "clean") -> dict[str, object]:
        if self.closed:
            raise RuntimeError("ingest session is already closed")
        self.closed = True
        if self.archive is not None:
            self.storage.add_segments(self.connection_id, self.archive.close())
        stats = {
            **asdict(self.parser.stats),
            "records": self.ingest_index,
            "records_rejected": self.records_rejected,
            "trailing_bytes": len(self.parser.buffer),
        }
        self.storage.close_connection(self.connection_id, status, stats)
        return stats


def replay_archive(
    directory: Path,
    storage: HeimdallStorage,
    run_id: int,
    chunk_size: int = 65536,
) -> dict[str, object]:
    session = IngestSession(storage, run_id, str(directory), None)
    try:
        for chunk in iter_archive_bytes(directory, chunk_size):
            session.feed(chunk)
    except BaseException:
        session.close("failed")
        raise
    return session.close()
