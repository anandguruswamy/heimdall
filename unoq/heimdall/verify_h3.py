"""Verify a live H3 database, its raw archive, and a replay database."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


OBSERVATION_COLUMNS = """
    o.route,o.reporting_node_id,o.observed_node_id,o.observed_k,o.report_k,
    o.usb_sequence,o.obs_flags,o.observed_m,o.round_delta,
    o.observed_tx_timestamp,o.rx_timestamp,o.cfo_raw,o.fp_index_q10_6,
    o.f1,o.f2,o.f3,o.ip_power,o.accum_count,o.dgc_decision,
    o.cir_start_offset,o.cir_taps,o.cir_blob,o.subreport_bytes
"""


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def _digest_rows(rows) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        count += 1
        for value in row:
            if value is None:
                encoded = b"N"
            elif isinstance(value, int):
                encoded = b"I" + str(value).encode("ascii")
            elif isinstance(value, str):
                encoded = b"S" + value.encode("utf-8")
            else:
                encoded = b"B" + bytes(value)
            digest.update(len(encoded).to_bytes(8, "little"))
            digest.update(encoded)
    return count, digest.hexdigest()


def _database_summary(db: sqlite3.Connection) -> dict[str, object]:
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    connections = [
        {
            "id": row[0],
            "status": row[1],
            "records": row[2],
            "observations": row[3],
            "stats": json.loads(row[4]) if row[4] else None,
        }
        for row in db.execute(
            """SELECT c.id,c.status,COUNT(DISTINCT r.id),COUNT(o.id),c.stats_json
               FROM connections c
               LEFT JOIN usb_records r ON r.connection_id=c.id
               LEFT JOIN observations o ON o.record_id=r.id
               GROUP BY c.id ORDER BY c.id"""
        )
    ]
    for connection in connections:
        connection_id = connection["id"]
        sequence = db.execute(
            """SELECT MIN(ingest_index),
                      (SELECT sequence FROM usb_records WHERE connection_id=? ORDER BY ingest_index LIMIT 1),
                      (SELECT sequence FROM usb_records WHERE connection_id=? ORDER BY ingest_index DESC LIMIT 1)
               FROM usb_records WHERE connection_id=?""",
            (connection_id, connection_id, connection_id),
        ).fetchone()
        connection["first_sequence"] = sequence[1]
        connection["last_sequence"] = sequence[2]
        summary = db.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(CAST(json_extract(d.payload_json,'$.usb_queue_drops') AS INTEGER)),0),
                      COALESCE(MAX(CAST(json_extract(d.payload_json,'$.usb_queue_drops') AS INTEGER)),0),
                      COALESCE(SUM(CASE WHEN CAST(json_extract(d.payload_json,'$.usb_queue_drops') AS INTEGER)=65535 THEN 1 ELSE 0 END),0)
               FROM decoded_records d JOIN usb_records r ON r.id=d.record_id
               WHERE r.connection_id=? AND d.kind='CycleSummaryRecord'""",
            (connection_id,),
        ).fetchone()
        connection["cycle_summaries"] = summary[0]
        connection["producer_drops_reported"] = summary[1]
        connection["max_reported_drops"] = summary[2]
        connection["saturated_drop_summaries"] = summary[3]
        connection["nonzero_drop_summaries"] = db.execute(
            """SELECT COUNT(*) FROM decoded_records d
               JOIN usb_records r ON r.id=d.record_id
               WHERE r.connection_id=? AND d.kind='CycleSummaryRecord'
                 AND CAST(json_extract(d.payload_json,'$.usb_queue_drops') AS INTEGER)>0""",
            (connection_id,),
        ).fetchone()[0]
        heartbeats = db.execute(
            """SELECT MIN(CAST(json_extract(d.payload_json,'$.uptime_ms') AS INTEGER)),
                      MAX(CAST(json_extract(d.payload_json,'$.uptime_ms') AS INTEGER)),
                      MIN(CAST(json_extract(d.payload_json,'$.cycles_completed') AS INTEGER)),
                      MAX(CAST(json_extract(d.payload_json,'$.cycles_completed') AS INTEGER))
               FROM decoded_records d JOIN usb_records r ON r.id=d.record_id
               WHERE r.connection_id=? AND d.kind='HeartbeatRecord'""",
            (connection_id,),
        ).fetchone()
        connection["heartbeat_uptime_ms"] = [heartbeats[0], heartbeats[1]]
        connection["heartbeat_cycles"] = [heartbeats[2], heartbeats[3]]
        connection["record_types"] = {
            str(record_type): count
            for record_type, count in db.execute(
                "SELECT type,COUNT(*) FROM usb_records WHERE connection_id=? GROUP BY type",
                (connection_id,),
            )
        }
        health = db.execute(
            """SELECT
                 COALESCE(SUM(CAST(json_extract(d.payload_json,'$.frames_received') AS INTEGER)),0),
                 COALESCE(SUM(CAST(json_extract(d.payload_json,'$.frames_expected') AS INTEGER)),0),
                 COALESCE(SUM(CAST(json_extract(d.payload_json,'$.fcs_errors') AS INTEGER)),0),
                 COALESCE(SUM(CAST(json_extract(d.payload_json,'$.filter_rejects') AS INTEGER)),0),
                 COALESCE(SUM(CAST(json_extract(d.payload_json,'$.validation_rejects') AS INTEGER)),0),
                 COALESCE(SUM(CAST(json_extract(d.payload_json,'$.subreport_crc_failures') AS INTEGER)),0),
                 COALESCE(MAX(CAST(json_extract(d.payload_json,'$.rx_callback_max_us') AS INTEGER)),0)
               FROM decoded_records d JOIN usb_records r ON r.id=d.record_id
               WHERE r.connection_id=? AND d.kind='CycleSummaryRecord'""",
            (connection_id,),
        ).fetchone()
        connection["radio_health"] = {
            "frames_received": health[0],
            "frames_expected": health[1],
            "fcs_errors": health[2],
            "filter_rejects": health[3],
            "validation_rejects": health[4],
            "subreport_crc_failures": health[5],
            "rx_callback_max_us": health[6],
        }
    raw_count, raw_digest = _digest_rows(
        db.execute(
            """SELECT r.sequence,r.type,r.flags,r.raw,r.decode_status,r.decode_error
               FROM usb_records r JOIN connections c ON c.id=r.connection_id
               ORDER BY c.id,r.ingest_index"""
        )
    )
    observation_count, observation_digest = _digest_rows(
        db.execute(
            f"""SELECT {OBSERVATION_COLUMNS}
                 FROM observations o
                 JOIN usb_records r ON r.id=o.record_id
                 JOIN connections c ON c.id=r.connection_id
                 ORDER BY c.id,r.ingest_index,o.id"""
        )
    )
    rejection_reasons = {
        reason: count
        for reason, count in db.execute(
            """SELECT decode_error,COUNT(*) FROM usb_records
               WHERE decode_status='rejected' GROUP BY decode_error"""
        )
    }
    producer_drops = db.execute(
        """SELECT COALESCE(SUM(CAST(json_extract(payload_json,'$.usb_queue_drops') AS INTEGER)),0)
           FROM decoded_records WHERE kind='CycleSummaryRecord'"""
    ).fetchone()[0]
    return {
        "integrity": integrity,
        "runs": db.execute("SELECT id,status FROM runs ORDER BY id").fetchall(),
        "connections": connections,
        "raw_records": {"count": raw_count, "sha256": raw_digest},
        "observations": {"count": observation_count, "sha256": observation_digest},
        "rejection_reasons": rejection_reasons,
        "producer_drops_reported": producer_drops,
    }


def _verify_segments(db: sqlite3.Connection, archive_root: Path) -> dict[str, object]:
    issues: list[str] = []
    checked = 0
    catalog = {
        (connection_id, segment_index): (byte_count, sha256)
        for connection_id, segment_index, byte_count, sha256 in db.execute(
            "SELECT connection_id,segment_index,byte_count,sha256 FROM raw_segments"
        )
    }
    actual: set[tuple[int, int]] = set()
    for directory in sorted(archive_root.glob("connection-*")):
        try:
            connection_id = int(directory.name.removeprefix("connection-"))
        except ValueError:
            continue
        for path in sorted(directory.glob("segment-*.husb")):
            try:
                segment_index = int(path.stem.removeprefix("segment-"))
            except ValueError:
                continue
            key = (connection_id, segment_index)
            actual.add(key)
            expected = catalog.get(key)
            if expected is None:
                issues.append(f"uncataloged segment {connection_id}:{segment_index}")
                continue
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            found = (path.stat().st_size, digest.hexdigest())
            if found != expected:
                issues.append(f"segment mismatch {connection_id}:{segment_index}")
            checked += 1
    for connection_id, segment_index in sorted(catalog.keys() - actual):
        issues.append(f"missing segment {connection_id}:{segment_index}")
    return {"cataloged": len(catalog), "checked": checked, "issues": issues}


def verify(live_path: Path, archive_root: Path, replay_path: Path) -> dict[str, object]:
    live_db = _open_read_only(live_path)
    replay_db = _open_read_only(replay_path)
    try:
        live = _database_summary(live_db)
        replay = _database_summary(replay_db)
        segments = _verify_segments(live_db, archive_root)
    finally:
        live_db.close()
        replay_db.close()
    equivalent = (
        live["integrity"] == "ok"
        and replay["integrity"] == "ok"
        and not segments["issues"]
        and live["raw_records"] == replay["raw_records"]
        and live["observations"] == replay["observations"]
        and live["rejection_reasons"] == replay["rejection_reasons"]
    )
    return {"equivalent": equivalent, "segments": segments, "live": live, "replay": replay}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("live_database", type=Path)
    parser.add_argument("archive_root", type=Path)
    parser.add_argument("replay_database", type=Path)
    args = parser.parse_args()
    result = verify(args.live_database, args.archive_root, args.replay_database)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["equivalent"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
