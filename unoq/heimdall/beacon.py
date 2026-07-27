"""Beacon v1 frame decoding and pooled-report reassembly."""

from __future__ import annotations

from dataclasses import dataclass, field
import struct

from .protocol import HelloRecord, ProtocolError, RadioFrameRecord, Subreport, decode_subreport

FRAME_HEADER_BYTES = 31
RX_VALID = 1 << 0
RX_CONFIG_MISMATCH = 1 << 1
RX_UNKNOWN_VERSION = 1 << 2
RX_TRUNCATED = 1 << 3


@dataclass(frozen=True)
class BeaconHeader:
    mac_sequence: int
    network_id: int
    source_node_id: int
    protocol_version: int
    frame_type: int
    m: int
    k: int
    n_nodes: int
    m_slots: int
    config_hash: int
    tx_timestamp: int
    subreport_count: int
    pooled_total_bytes: int
    peer_observed_bitmap: int
    evidence_age: int
    flags: int


@dataclass(frozen=True)
class RelayedSubreport:
    header: BeaconHeader
    subreport: Subreport
    subreport_bytes: bytes


@dataclass
class _PendingReport:
    header: BeaconHeader
    fragments: dict[int, bytes] = field(default_factory=dict)


def decode_beacon_header(frame: bytes, hello: HelloRecord) -> BeaconHeader:
    if len(frame) != FRAME_HEADER_BYTES + hello.frame_payload_bytes:
        raise ProtocolError("beacon frame length does not match HELLO")
    if struct.unpack_from("<H", frame, 0)[0] != 0x8841:
        raise ProtocolError("invalid beacon frame control")
    if struct.unpack_from("<H", frame, 5)[0] != 0xFFFF:
        raise ProtocolError("beacon destination is not broadcast")
    source = struct.unpack_from("<H", frame, 7)[0]
    header = BeaconHeader(
        mac_sequence=frame[2],
        network_id=struct.unpack_from("<H", frame, 3)[0],
        source_node_id=source,
        protocol_version=frame[9],
        frame_type=frame[10],
        m=frame[11],
        k=struct.unpack_from("<I", frame, 12)[0],
        n_nodes=frame[16],
        m_slots=frame[17],
        config_hash=struct.unpack_from("<H", frame, 18)[0],
        tx_timestamp=int.from_bytes(frame[20:25], "little"),
        subreport_count=frame[25],
        pooled_total_bytes=struct.unpack_from("<H", frame, 26)[0],
        peer_observed_bitmap=frame[28],
        evidence_age=frame[29],
        flags=frame[30],
    )
    if source >= hello.n_nodes or source != header.k % hello.n_nodes:
        raise ProtocolError("beacon source does not own superslot")
    if header.protocol_version != hello.heimdall_version or header.frame_type != 0:
        raise ProtocolError("unsupported beacon protocol or frame type")
    if header.n_nodes != hello.n_nodes or header.m_slots != hello.m_slots:
        raise ProtocolError("beacon dimensions do not match HELLO")
    if header.config_hash != hello.config_hash:
        raise ProtocolError("beacon config hash does not match HELLO")
    if header.m >= hello.m_slots:
        raise ProtocolError("beacon fragment index is outside M")
    if header.pooled_total_bytes > (hello.n_nodes - 1) * hello.subreport_bytes:
        raise ProtocolError("pooled report exceeds configured maximum")
    if header.subreport_count > hello.n_nodes - 1:
        raise ProtocolError("beacon subreport count exceeds configured maximum")
    if header.pooled_total_bytes > header.subreport_count * hello.subreport_bytes:
        raise ProtocolError("pooled report exceeds subreport count capacity")
    if header.peer_observed_bitmap.bit_count() != header.subreport_count:
        raise ProtocolError("peer observation bitmap does not match subreport count")
    if header.peer_observed_bitmap & (1 << source):
        raise ProtocolError("beacon report contains a self-observation")
    return header


class ReportReassembler:
    """Collect all M fragments and decode each independently checksummed subreport."""

    def __init__(self) -> None:
        self.pending: dict[tuple[int, int], _PendingReport] = {}
        self.duplicate_fragments = 0
        self.inconsistent_fragments = 0

    def reset(self) -> None:
        self.pending.clear()

    def add(self, radio: RadioFrameRecord, hello: HelloRecord) -> list[RelayedSubreport]:
        if radio.rx_flags != RX_VALID:
            return []
        header = decode_beacon_header(radio.frame, hello)
        payload = radio.frame[FRAME_HEADER_BYTES:]
        valid_in_fragment = max(
            0,
            min(
                hello.frame_payload_bytes,
                header.pooled_total_bytes - header.m * hello.frame_payload_bytes,
            ),
        )
        if any(payload[valid_in_fragment:]):
            raise ProtocolError("beacon frame has non-zero report padding")
        key = (header.source_node_id, header.k)
        pending = self.pending.get(key)
        if pending is None:
            stale = [
                item for item in self.pending
                if item[0] == header.source_node_id and item != key
            ]
            for item in stale:
                del self.pending[item]
            pending = _PendingReport(header)
            self.pending[key] = pending
        elif not self._consistent(pending.header, header):
            self.inconsistent_fragments += 1
            del self.pending[key]
            raise ProtocolError("inconsistent fragments for one pooled report")
        prior = pending.fragments.get(header.m)
        if prior is not None:
            if prior != payload:
                self.inconsistent_fragments += 1
                del self.pending[key]
                raise ProtocolError("duplicate fragment body changed")
            self.duplicate_fragments += 1
            return []
        pending.fragments[header.m] = payload
        if len(pending.fragments) != hello.m_slots:
            return []
        pooled = b"".join(pending.fragments[m] for m in range(hello.m_slots))
        pooled = pooled[: header.pooled_total_bytes]
        del self.pending[key]
        return self._decode_report(header, pooled, hello)

    @staticmethod
    def _consistent(first: BeaconHeader, other: BeaconHeader) -> bool:
        return (
            first.source_node_id,
            first.k,
            first.n_nodes,
            first.m_slots,
            first.config_hash,
            first.pooled_total_bytes,
            first.peer_observed_bitmap,
            first.evidence_age,
            first.flags,
        ) == (
            other.source_node_id,
            other.k,
            other.n_nodes,
            other.m_slots,
            other.config_hash,
            other.pooled_total_bytes,
            other.peer_observed_bitmap,
            other.evidence_age,
            other.flags,
        )

    @staticmethod
    def _decode_report(
        header: BeaconHeader, pooled: bytes, hello: HelloRecord
    ) -> list[RelayedSubreport]:
        decoded: list[RelayedSubreport] = []
        offset = 0
        observed_ids: list[int] = []
        while offset < len(pooled):
            if len(pooled) - offset < 40:
                raise ProtocolError("pooled report ends inside subreport metadata")
            taps = pooled[offset + 35]
            length = 40 + 4 * taps
            if taps == 0 or taps > hello.cir_taps or length > hello.subreport_bytes:
                raise ProtocolError("subreport dimensions exceed HELLO")
            if offset + length > len(pooled):
                raise ProtocolError("pooled report ends inside a subreport")
            raw = pooled[offset : offset + length]
            subreport = decode_subreport(raw)
            if subreport.observed_node_id >= hello.n_nodes:
                raise ProtocolError("subreport node is outside N")
            expected_delta = (
                header.source_node_id + hello.n_nodes - subreport.observed_node_id
            ) % hello.n_nodes
            if subreport.observed_m != 0 or subreport.round_delta != expected_delta:
                raise ProtocolError("subreport schedule metadata is inconsistent")
            observed_ids.append(subreport.observed_node_id)
            decoded.append(RelayedSubreport(header, subreport, raw))
            offset += length
        bitmap_ids = [
            node for node in range(hello.n_nodes)
            if header.peer_observed_bitmap & (1 << node)
        ]
        if (
            sorted(observed_ids) != bitmap_ids
            or len(decoded) != len(bitmap_ids)
            or len(decoded) != header.subreport_count
        ):
            raise ProtocolError("subreports do not match peer observation bitmap")
        return decoded
