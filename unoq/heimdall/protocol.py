"""Incremental decoder for the Heimdall USB CDC v1 byte stream."""

from __future__ import annotations

from dataclasses import dataclass, field
import struct
import zlib

SYNC = b"\xc3\xa5"
VERSION = 1
MAX_PAYLOAD = 4096

HELLO = 0x01
HEARTBEAT = 0x02
RADIO_FRAME = 0x03
LOCAL_OBS = 0x04
CYCLE_SUMMARY = 0x05
ERROR = 0x06
TX_RECORD = 0x07


class ProtocolError(ValueError):
    """A CRC-valid record violates its type-specific contract."""


@dataclass(frozen=True)
class Record:
    type: int
    flags: int
    sequence: int
    payload: bytes
    raw: bytes


@dataclass
class ParserStats:
    crc_failures: int = 0
    framing_errors: int = 0
    sequence_gaps: int = 0
    duplicates_or_old: int = 0
    unknown_types: int = 0


@dataclass(frozen=True)
class HelloRecord:
    heimdall_version: int
    usb_version: int
    n_nodes: int
    m_slots: int
    node_id: int
    master_node_id: int
    cir_taps: int
    cir_left_taps: int
    config_hash: int
    subreport_bytes: int
    frame_payload_bytes: int
    max_frame_bytes: int
    slot_duration_us: int
    cycle_us: int
    device_id: int
    firmware_id: int


@dataclass(frozen=True)
class HeartbeatRecord:
    uptime_ms: int
    cycles_completed: int
    sync_state: int
    evidence_age: int


@dataclass(frozen=True)
class RadioFrameRecord:
    rx_timestamp: int
    rx_flags: int
    frame: bytes


@dataclass(frozen=True)
class Subreport:
    observed_node_id: int
    obs_flags: int
    observed_m: int
    round_delta: int
    observed_tx_timestamp: int
    rx_timestamp: int
    cfo_raw: int
    fp_index_q10_6: int
    f1: int
    f2: int
    f3: int
    ip_power: int
    accum_count: int
    dgc_decision: int
    cir_start_offset: int
    cir_iq: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class LocalObservationRecord:
    reporting_node_id: int
    k: int
    subreport: Subreport
    subreport_bytes: bytes


@dataclass(frozen=True)
class TxRecord:
    k: int
    m: int
    tx_timestamp: int
    frame_length: int
    confirmed: bool


@dataclass(frozen=True)
class CycleSummaryRecord:
    k_cycle_start: int
    cycle_index: int
    frames_received: int
    frames_expected: int
    fcs_errors: int
    filter_rejects: int
    validation_rejects: int
    subreport_crc_failures: int
    usb_queue_drops: int
    rx_callback_max_us: int
    peer_m0_miss: tuple[int, ...]
    evidence_age: int
    flags: int


@dataclass(frozen=True)
class ErrorRecord:
    code: int
    detail: int
    k: int
    text: str


@dataclass
class DecoderState:
    hello: HelloRecord | None = None


class StreamParser:
    """Parse records across arbitrary reads and recover after corrupt bytes."""

    def __init__(self, max_payload: int = MAX_PAYLOAD) -> None:
        self.buffer = bytearray()
        self.max_payload = max_payload
        self.expected_sequence: int | None = None
        self.pending_crc_records = 0
        self.stats = ParserStats()

    def feed(self, data: bytes) -> list[Record]:
        self.buffer.extend(data)
        records: list[Record] = []
        while True:
            sync_at = self.buffer.find(SYNC)
            if sync_at < 0:
                if self.buffer[-1:] == SYNC[:1]:
                    del self.buffer[:-1]
                else:
                    self.buffer.clear()
                break
            if sync_at != 0:
                del self.buffer[:sync_at]
            if len(self.buffer) < 12:
                break
            payload_length = struct.unpack_from("<H", self.buffer, 6)[0]
            if payload_length > self.max_payload:
                self.stats.framing_errors += 1
                del self.buffer[0]
                continue
            total_length = 16 + payload_length
            if len(self.buffer) < total_length:
                break
            candidate = bytes(self.buffer[:total_length])
            expected_crc = struct.unpack_from("<I", candidate, 12 + payload_length)[0]
            actual_crc = zlib.crc32(candidate[2 : 12 + payload_length])
            if candidate[2] != VERSION or expected_crc != actual_crc:
                self.stats.crc_failures += 1
                if candidate[2] == VERSION:
                    self.pending_crc_records += 1
                del self.buffer[0]
                continue
            del self.buffer[:total_length]
            record = Record(
                type=candidate[3],
                flags=candidate[4],
                sequence=struct.unpack_from("<I", candidate, 8)[0],
                payload=candidate[12 : 12 + payload_length],
                raw=candidate,
            )
            self._account_sequence(record.sequence)
            if record.type not in range(HELLO, TX_RECORD + 1):
                self.stats.unknown_types += 1
            else:
                records.append(record)
        return records

    def _account_sequence(self, sequence: int) -> None:
        if self.expected_sequence is None:
            self.expected_sequence = (sequence + 1) & 0xFFFFFFFF
            self.pending_crc_records = 0
            return
        delta = (sequence - self.expected_sequence) & 0xFFFFFFFF
        if delta == 0:
            self.expected_sequence = (sequence + 1) & 0xFFFFFFFF
        elif delta < 0x80000000:
            self.stats.sequence_gaps += max(0, delta - self.pending_crc_records)
            self.expected_sequence = (sequence + 1) & 0xFFFFFFFF
        else:
            self.stats.duplicates_or_old += 1
        self.pending_crc_records = 0


def encode_record(record_type: int, flags: int, sequence: int, payload: bytes) -> bytes:
    header = struct.pack("<HBBBBHI", 0xA5C3, VERSION, record_type, flags, 0,
                         len(payload), sequence)
    crc = zlib.crc32(header[2:] + payload)
    return header + payload + struct.pack("<I", crc)


def _u40(data: bytes) -> int:
    if len(data) != 5:
        raise ProtocolError("u40 requires five bytes")
    return int.from_bytes(data, "little")


def decode_subreport(data: bytes) -> Subreport:
    if len(data) < 40:
        raise ProtocolError("subreport is shorter than fixed metadata")
    taps = data[35]
    expected_length = 40 + 4 * taps
    if taps == 0 or taps > 128 or len(data) != expected_length:
        raise ProtocolError("subreport tap count does not match its length")
    expected_crc = struct.unpack_from("<I", data, expected_length - 4)[0]
    if zlib.crc32(data[:-4]) != expected_crc:
        raise ProtocolError("subreport CRC32 mismatch")
    cir_values = struct.unpack_from(f"<{2 * taps}h", data, 36)
    cir_iq = tuple(zip(cir_values[0::2], cir_values[1::2]))
    return Subreport(
        observed_node_id=data[0],
        obs_flags=data[1],
        observed_m=data[2],
        round_delta=data[3],
        observed_tx_timestamp=_u40(data[4:9]),
        rx_timestamp=_u40(data[9:14]),
        cfo_raw=struct.unpack_from("<h", data, 14)[0],
        fp_index_q10_6=struct.unpack_from("<H", data, 16)[0],
        f1=int.from_bytes(data[18:21], "little"),
        f2=int.from_bytes(data[21:24], "little"),
        f3=int.from_bytes(data[24:27], "little"),
        ip_power=int.from_bytes(data[27:30], "little"),
        accum_count=struct.unpack_from("<H", data, 30)[0],
        dgc_decision=data[32],
        cir_start_offset=struct.unpack_from("<H", data, 33)[0],
        cir_iq=cir_iq,
    )


def decode_record(record: Record, state: DecoderState) -> object:
    payload = record.payload
    if record.type == HELLO:
        if len(payload) != 36:
            raise ProtocolError("HELLO payload must be 36 bytes")
        values = struct.unpack("<8B4H2IQI", payload)
        hello = HelloRecord(*values)
        if hello.usb_version != VERSION:
            raise ProtocolError("unsupported USB contract version")
        state.hello = hello
        return hello
    if record.type == HEARTBEAT:
        if len(payload) != 12 or payload[10:12] != b"\x00\x00":
            raise ProtocolError("invalid HEARTBEAT payload")
        return HeartbeatRecord(*struct.unpack_from("<IIBB", payload))
    if record.type in (RADIO_FRAME, LOCAL_OBS) and state.hello is None:
        raise ProtocolError("HELLO is required before data records")
    if record.type == RADIO_FRAME:
        if len(payload) < 8:
            raise ProtocolError("RADIO_FRAME payload is too short")
        frame_length = struct.unpack_from("<H", payload, 6)[0]
        if len(payload) != 8 + frame_length:
            raise ProtocolError("RADIO_FRAME length mismatch")
        return RadioFrameRecord(_u40(payload[:5]), payload[5], payload[8:])
    if record.type == LOCAL_OBS:
        if len(payload) < 45:
            raise ProtocolError("LOCAL_OBS payload is too short")
        subreport_bytes = payload[5:]
        return LocalObservationRecord(payload[0], struct.unpack_from("<I", payload, 1)[0],
                                      decode_subreport(subreport_bytes), subreport_bytes)
    if record.type == TX_RECORD:
        if len(payload) != 13:
            raise ProtocolError("TX_RECORD payload must be 13 bytes")
        return TxRecord(struct.unpack_from("<I", payload)[0], payload[4],
                        _u40(payload[5:10]), struct.unpack_from("<H", payload, 10)[0],
                        bool(payload[12] & 1))
    if record.type == CYCLE_SUMMARY:
        if len(payload) != 34:
            raise ProtocolError("CYCLE_SUMMARY payload must be 34 bytes")
        return CycleSummaryRecord(
            *struct.unpack_from("<II8H", payload), tuple(payload[24:32]),
            payload[32], payload[33]
        )
    if record.type == ERROR:
        if len(payload) < 8:
            raise ProtocolError("ERROR payload is too short")
        code, detail, k = struct.unpack_from("<HHI", payload)
        return ErrorRecord(code, detail, k, payload[8:].decode("utf-8", "replace"))
    return record
