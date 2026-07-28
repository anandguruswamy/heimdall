from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
UNOQ = ROOT / "unoq"
if str(UNOQ) not in sys.path:
    sys.path.insert(0, str(UNOQ))

from heimdall.canonical import CanonicalProcessor  # noqa: E402
from heimdall.protocol import HELLO, ProtocolError, StreamParser  # noqa: E402


def decode_capture(path: Path, chunk_bytes: int = 1024 * 1024):
    parser = StreamParser()
    processor = CanonicalProcessor()
    observations = {}
    records = 0
    decode_rejections = 0
    post_hello_decode_rejections = 0
    capture_hash = hashlib.sha256()
    hello = None
    last_sequence = None
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_bytes):
            capture_hash.update(chunk)
            for record in parser.feed(chunk):
                records += 1
                try:
                    output = processor.process(record)
                except ProtocolError:
                    decode_rejections += 1
                    if hello is not None:
                        post_hello_decode_rejections += 1
                    continue
                if output.configuration_changed:
                    if hello is not None and output.decoded != hello:
                        raise ValueError(
                            "capture contains multiple HELLO configurations; split it by configuration epoch"
                        )
                    hello = output.decoded
                if record.type == HELLO and last_sequence is not None:
                    sequence_delta = (record.sequence - last_sequence) & 0xFFFFFFFF
                    if sequence_delta >= 0x80000000:
                        raise ValueError(
                            "capture contains a USB sequence restart; split it by device epoch"
                        )
                last_sequence = record.sequence
                for item in output.observations:
                    if item.observed_node_id == item.reporting_node_id:
                        continue
                    key = (
                        item.observed_node_id,
                        item.reporting_node_id,
                        item.observed_k,
                        item.observed_tx_timestamp,
                        item.rx_timestamp,
                    )
                    observations[key] = item
    return list(observations.values()), {
        "records": records,
        "canonical_observations": len(observations),
        "decode_rejections": decode_rejections,
        "post_hello_decode_rejections": post_hello_decode_rejections,
        "capture_sha256": capture_hash.hexdigest(),
        "hello": asdict(hello) if hello is not None else None,
        "parser": vars(parser.stats),
        "trailing_bytes": len(parser.buffer),
        "incomplete_reports": len(processor.reassembler.pending),
        "duplicate_fragments": processor.reassembler.duplicate_fragments,
        "inconsistent_fragments": processor.reassembler.inconsistent_fragments,
    }
