"""Pure conversion from validated USB records to canonical Heimdall events."""

from __future__ import annotations

from dataclasses import dataclass
import struct

from .beacon import ReportReassembler
from .protocol import (
    DecoderState,
    HelloRecord,
    LocalObservationRecord,
    RadioFrameRecord,
    Record,
    Subreport,
    decode_record,
)


@dataclass(frozen=True)
class CanonicalObservation:
    route: str
    reporting_node_id: int
    observed_node_id: int
    observed_k: int
    report_k: int | None
    usb_sequence: int
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
    cir_taps: int
    cir_blob: bytes
    subreport_bytes: bytes


@dataclass(frozen=True)
class CanonicalOutput:
    decoded: object
    observations: tuple[CanonicalObservation, ...] = ()
    configuration_changed: bool = False


class CanonicalProcessor:
    def __init__(self) -> None:
        self.decoder_state = DecoderState()
        self.reassembler = ReportReassembler()

    def process(self, record: Record) -> CanonicalOutput:
        prior_hello = self.decoder_state.hello
        decoded = decode_record(record, self.decoder_state)
        changed = isinstance(decoded, HelloRecord) and decoded != prior_hello
        if changed:
            self.reassembler.reset()
        if isinstance(decoded, LocalObservationRecord):
            observation = self._observation(
                "local",
                decoded.reporting_node_id,
                decoded.k,
                None,
                record.sequence,
                decoded.subreport,
                decoded.subreport_bytes,
            )
            return CanonicalOutput(decoded, (observation,), changed)
        if isinstance(decoded, RadioFrameRecord):
            hello = self.decoder_state.hello
            assert hello is not None
            relayed = self.reassembler.add(decoded, hello)
            observations = tuple(
                self._observation(
                    "relayed",
                    item.header.source_node_id,
                    (item.header.k - item.subreport.round_delta) & 0xFFFFFFFF,
                    item.header.k,
                    record.sequence,
                    item.subreport,
                    item.subreport_bytes,
                )
                for item in relayed
            )
            return CanonicalOutput(decoded, observations, changed)
        return CanonicalOutput(decoded, (), changed)

    @staticmethod
    def _observation(
        route: str,
        reporting_node_id: int,
        observed_k: int,
        report_k: int | None,
        usb_sequence: int,
        subreport: Subreport,
        raw: bytes,
    ) -> CanonicalObservation:
        cir_values = [value for pair in subreport.cir_iq for value in pair]
        cir_blob = struct.pack(f"<{len(cir_values)}h", *cir_values)
        return CanonicalObservation(
            route=route,
            reporting_node_id=reporting_node_id,
            observed_node_id=subreport.observed_node_id,
            observed_k=observed_k,
            report_k=report_k,
            usb_sequence=usb_sequence,
            obs_flags=subreport.obs_flags,
            observed_m=subreport.observed_m,
            round_delta=subreport.round_delta,
            observed_tx_timestamp=subreport.observed_tx_timestamp,
            rx_timestamp=subreport.rx_timestamp,
            cfo_raw=subreport.cfo_raw,
            fp_index_q10_6=subreport.fp_index_q10_6,
            f1=subreport.f1,
            f2=subreport.f2,
            f3=subreport.f3,
            ip_power=subreport.ip_power,
            accum_count=subreport.accum_count,
            dgc_decision=subreport.dgc_decision,
            cir_start_offset=subreport.cir_start_offset,
            cir_taps=len(subreport.cir_iq),
            cir_blob=cir_blob,
            subreport_bytes=raw,
        )
