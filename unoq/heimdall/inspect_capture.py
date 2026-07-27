"""Validate and summarize a raw Heimdall USB capture."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import struct

from .protocol import (
    DecoderState,
    LOCAL_OBS,
    RADIO_FRAME,
    CycleSummaryRecord,
    LocalObservationRecord,
    ProtocolError,
    RadioFrameRecord,
    StreamParser,
    TxRecord,
    decode_record,
)


def inspect(data: bytes) -> dict[str, object]:
    parser = StreamParser()
    records = parser.feed(data)
    state = DecoderState()
    decoded: list[object] = []
    prehello_skipped = 0
    for record in records:
        try:
            decoded.append(decode_record(record, state))
        except ProtocolError:
            if state.hello is None and record.type in (RADIO_FRAME, LOCAL_OBS):
                prehello_skipped += 1
                continue
            raise
    summaries = [item for item in decoded if isinstance(item, CycleSummaryRecord)]
    radio_frames = [item for item in decoded if isinstance(item, RadioFrameRecord)]
    local_observations = [
        item for item in decoded if isinstance(item, LocalObservationRecord)
    ]
    tx_records = [item for item in decoded if isinstance(item, TxRecord)]
    radio_owners = [
        (struct.unpack_from("<I", item.frame, 12)[0],
         struct.unpack_from("<H", item.frame, 7)[0])
        for item in radio_frames
    ]
    tail_summaries = summaries[-100:]
    return {
        "bytes": len(data),
        "records": len(records),
        "record_counts": dict(sorted(Counter(record.type for record in records).items())),
        "crc_failures": parser.stats.crc_failures,
        "framing_errors": parser.stats.framing_errors,
        "sequence_gaps": parser.stats.sequence_gaps,
        "duplicates_or_old": parser.stats.duplicates_or_old,
        "unknown_types": parser.stats.unknown_types,
        "trailing_bytes": len(parser.buffer),
        "prehello_data_skipped": prehello_skipped,
        "hello": asdict(state.hello) if is_dataclass(state.hello) else None,
        "cycle_summaries": len(summaries),
        "summary_usb_drops": sum(item.usb_queue_drops for item in summaries),
        "nonzero_drop_summaries": [
            [item.cycle_index, item.usb_queue_drops]
            for item in summaries
            if item.usb_queue_drops
        ],
        "summary_validation_rejects": sum(item.validation_rejects for item in summaries),
        "summary_subreport_crc_failures": sum(
            item.subreport_crc_failures for item in summaries
        ),
        "summary_fcs_errors": sum(item.fcs_errors for item in summaries),
        "summary_filter_rejects": sum(item.filter_rejects for item in summaries),
        "summary_callback_max_us": max(
            (item.rx_callback_max_us for item in summaries), default=0
        ),
        "summary_cycle_binding_valid": all(
            state.hello is not None
            and item.k_cycle_start == state.hello.n_nodes * item.cycle_index
            for item in summaries
        ),
        "tail_100_usb_drops": sum(item.usb_queue_drops for item in tail_summaries),
        "tail_100_peer_misses": sum(
            sum(item.peer_m0_miss) for item in tail_summaries
        ),
        "tail_100_all_frames_complete": all(
            item.frames_received == item.frames_expected for item in tail_summaries
        ),
        "radio_frames_decoded": len(radio_frames),
        "radio_ownership_valid": state.hello is not None and all(
            k % state.hello.n_nodes == source for k, source in radio_owners
        ),
        "local_observations_decoded": len(local_observations),
        "tx_records_decoded": len(tx_records),
        "tx_all_confirmed": all(item.confirmed for item in tx_records),
        "tx_ownership_valid": state.hello is not None and all(
            item.k % state.hello.n_nodes == state.hello.node_id
            for item in tx_records
        ),
    }


def main() -> None:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("capture", type=Path)
    args = argument_parser.parse_args()
    print(json.dumps(inspect(args.capture.read_bytes()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
