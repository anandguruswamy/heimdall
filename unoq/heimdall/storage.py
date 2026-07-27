"""SQLite persistence for raw records and canonical Heimdall events."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .archive import ClosedSegment
from .canonical import CanonicalObservation, CanonicalOutput
from .protocol import LocalObservationRecord, RadioFrameRecord, Record

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value):
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _decoded_summary(decoded: object) -> object:
    if isinstance(decoded, RadioFrameRecord):
        return {
            "rx_timestamp": decoded.rx_timestamp,
            "rx_flags": decoded.rx_flags,
            "frame_length": len(decoded.frame),
        }
    if isinstance(decoded, LocalObservationRecord):
        return {
            "reporting_node_id": decoded.reporting_node_id,
            "k": decoded.k,
            "observed_node_id": decoded.subreport.observed_node_id,
        }
    return _jsonable(decoded)


class HeimdallStorage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = FULL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_info (
                version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY,
                started_utc TEXT NOT NULL,
                ended_utc TEXT,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connections (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES runs(id),
                opened_utc TEXT NOT NULL,
                closed_utc TEXT,
                device_path TEXT NOT NULL,
                status TEXT NOT NULL,
                stats_json TEXT
            );
            CREATE TABLE IF NOT EXISTS configurations (
                id INTEGER PRIMARY KEY,
                connection_id INTEGER NOT NULL REFERENCES connections(id),
                first_sequence INTEGER NOT NULL,
                config_hash INTEGER NOT NULL,
                hello_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usb_records (
                id INTEGER PRIMARY KEY,
                connection_id INTEGER NOT NULL REFERENCES connections(id),
                configuration_id INTEGER REFERENCES configurations(id),
                ingest_index INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                type INTEGER NOT NULL,
                flags INTEGER NOT NULL,
                raw BLOB NOT NULL,
                decode_status TEXT NOT NULL,
                decode_error TEXT,
                UNIQUE(connection_id, ingest_index)
            );
            CREATE TABLE IF NOT EXISTS decoded_records (
                record_id INTEGER PRIMARY KEY REFERENCES usb_records(id),
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY,
                record_id INTEGER NOT NULL REFERENCES usb_records(id),
                route TEXT NOT NULL,
                reporting_node_id INTEGER NOT NULL,
                observed_node_id INTEGER NOT NULL,
                observed_k INTEGER NOT NULL,
                report_k INTEGER,
                usb_sequence INTEGER NOT NULL,
                obs_flags INTEGER NOT NULL,
                observed_m INTEGER NOT NULL,
                round_delta INTEGER NOT NULL,
                observed_tx_timestamp INTEGER NOT NULL,
                rx_timestamp INTEGER NOT NULL,
                cfo_raw INTEGER NOT NULL,
                fp_index_q10_6 INTEGER NOT NULL,
                f1 INTEGER NOT NULL,
                f2 INTEGER NOT NULL,
                f3 INTEGER NOT NULL,
                ip_power INTEGER NOT NULL,
                accum_count INTEGER NOT NULL,
                dgc_decision INTEGER NOT NULL,
                cir_start_offset INTEGER NOT NULL,
                cir_taps INTEGER NOT NULL,
                cir_blob BLOB NOT NULL,
                subreport_bytes BLOB NOT NULL,
                UNIQUE(record_id, route, reporting_node_id, observed_node_id, observed_k)
            );
            CREATE TABLE IF NOT EXISTS raw_segments (
                id INTEGER PRIMARY KEY,
                connection_id INTEGER NOT NULL REFERENCES connections(id),
                segment_index INTEGER NOT NULL,
                path TEXT NOT NULL,
                byte_count INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                UNIQUE(connection_id, segment_index)
            );
            """
        )
        row = self.db.execute("SELECT version FROM schema_info").fetchone()
        if row is None:
            self.db.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
        elif row[0] != SCHEMA_VERSION:
            raise RuntimeError(f"unsupported database schema {row[0]}")
        self.db.commit()

    def start_run(self, metadata: dict[str, object]) -> int:
        cursor = self.db.execute(
            "INSERT INTO runs(started_utc,status,metadata_json) VALUES (?,?,?)",
            (utc_now(), "running", json.dumps(metadata, sort_keys=True)),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def close_run(self, run_id: int, status: str = "clean") -> None:
        self.db.execute(
            "UPDATE runs SET ended_utc=?, status=? WHERE id=?", (utc_now(), status, run_id)
        )
        self.db.commit()

    def start_connection(self, run_id: int, device_path: str) -> int:
        cursor = self.db.execute(
            "INSERT INTO connections(run_id,opened_utc,device_path,status) VALUES (?,?,?,?)",
            (run_id, utc_now(), device_path, "open"),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def close_connection(self, connection_id: int, status: str, stats: dict) -> None:
        self.db.execute(
            "UPDATE connections SET closed_utc=?,status=?,stats_json=? WHERE id=?",
            (utc_now(), status, json.dumps(stats, sort_keys=True), connection_id),
        )
        self.db.commit()

    def add_configuration(self, connection_id: int, sequence: int, hello: object) -> int:
        payload = _jsonable(hello)
        cursor = self.db.execute(
            "INSERT INTO configurations(connection_id,first_sequence,config_hash,hello_json) "
            "VALUES (?,?,?,?)",
            (connection_id, sequence, payload["config_hash"], json.dumps(payload, sort_keys=True)),
        )
        return int(cursor.lastrowid)

    def add_record(
        self,
        connection_id: int,
        configuration_id: int | None,
        ingest_index: int,
        record: Record,
        output: CanonicalOutput | None,
        error: str | None,
    ) -> int:
        cursor = self.db.execute(
            "INSERT INTO usb_records(connection_id,configuration_id,ingest_index,sequence,type,flags,raw,decode_status,decode_error) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                connection_id,
                configuration_id,
                ingest_index,
                record.sequence,
                record.type,
                record.flags,
                record.raw,
                "valid" if output is not None else "rejected",
                error,
            ),
        )
        record_id = int(cursor.lastrowid)
        if output is not None:
            self.db.execute(
                "INSERT INTO decoded_records(record_id,kind,payload_json) VALUES (?,?,?)",
                (
                    record_id,
                    type(output.decoded).__name__,
                    json.dumps(_decoded_summary(output.decoded), sort_keys=True),
                ),
            )
            for observation in output.observations:
                self._add_observation(record_id, observation)
        return record_id

    def _add_observation(self, record_id: int, item: CanonicalObservation) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO observations(
                record_id,route,reporting_node_id,observed_node_id,observed_k,report_k,
                usb_sequence,obs_flags,observed_m,round_delta,observed_tx_timestamp,
                rx_timestamp,cfo_raw,fp_index_q10_6,f1,f2,f3,ip_power,accum_count,
                dgc_decision,cir_start_offset,cir_taps,cir_blob,subreport_bytes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record_id, item.route, item.reporting_node_id, item.observed_node_id,
                item.observed_k, item.report_k, item.usb_sequence, item.obs_flags,
                item.observed_m, item.round_delta, item.observed_tx_timestamp,
                item.rx_timestamp, item.cfo_raw, item.fp_index_q10_6, item.f1,
                item.f2, item.f3, item.ip_power, item.accum_count, item.dgc_decision,
                item.cir_start_offset, item.cir_taps, item.cir_blob, item.subreport_bytes,
            ),
        )

    def add_segments(self, connection_id: int, segments: list[ClosedSegment]) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO raw_segments(connection_id,segment_index,path,byte_count,sha256) VALUES (?,?,?,?,?)",
            [
                (connection_id, segment.index, str(segment.path), segment.byte_count, segment.sha256)
                for segment in segments
            ],
        )
        self.db.commit()

    def commit(self) -> None:
        self.db.commit()

    def observation_fingerprints(self, run_id: int) -> list[tuple]:
        return self.db.execute(
            """SELECT route,reporting_node_id,observed_node_id,observed_k,report_k,
                obs_flags,observed_m,round_delta,observed_tx_timestamp,rx_timestamp,
                cfo_raw,fp_index_q10_6,f1,f2,f3,ip_power,accum_count,dgc_decision,
                cir_start_offset,cir_taps,hex(cir_blob),hex(subreport_bytes)
                FROM observations o JOIN usb_records r ON r.id=o.record_id
                JOIN connections c ON c.id=r.connection_id WHERE c.run_id=?
                ORDER BY o.id""",
            (run_id,),
        ).fetchall()

    def close(self) -> None:
        self.db.close()
