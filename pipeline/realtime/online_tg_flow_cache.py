from __future__ import annotations

import struct
from dataclasses import dataclass

from pipeline.flow_cache import FlowCache
from pipeline.models import FlowEntry, PacketRecord, RequestMeta


TG_METADATA_SIZE = 20
TG_FLOW_DIR_SRC_TO_DST = 0
TG_FLOW_DIR_DST_TO_SRC = 1

TG_DIRECTION_TO_NAME = {
    TG_FLOW_DIR_SRC_TO_DST: "src_to_dst",
    TG_FLOW_DIR_DST_TO_SRC: "dst_to_src",
}


@dataclass(frozen=True)
class TrafficGeneratorMetadata:
    flow_id: int
    size_bytes: int
    tos: int
    rate_mbps: int
    direction_value: int

    @property
    def direction(self) -> str:
        return TG_DIRECTION_TO_NAME[self.direction_value]

    @property
    def dscp(self) -> int:
        return self.tos >> 2


@dataclass(frozen=True)
class XdpPacketEvent:
    ts_us: int
    epoch_ts_us: int | None
    frame_number: int
    ifname: str

    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int

    frame_len: int
    ip_len: int
    ip_hdr_len: int
    ip_ttl: int
    ip_dscp: int
    ip_ecn: int

    tcp_len: int
    tcp_hdr_len: int
    tcp_seq: int
    tcp_ack: int
    tcp_flags: int
    tcp_window_size: int
    payload_prefix: bytes = b""

    @property
    def tcp_syn(self) -> int:
        return 1 if self.tcp_flags & 0x02 else 0

    @property
    def tcp_ack_flag(self) -> int:
        return 1 if self.tcp_flags & 0x10 else 0

    @property
    def tcp_psh(self) -> int:
        return 1 if self.tcp_flags & 0x08 else 0

    @property
    def tcp_fin(self) -> int:
        return 1 if self.tcp_flags & 0x01 else 0

    @property
    def tcp_rst(self) -> int:
        return 1 if self.tcp_flags & 0x04 else 0


@dataclass
class _DirectionalFlowState:
    meta: RequestMeta
    remaining_tcp_payload_bytes: int


def parse_tg_metadata(payload_prefix: bytes) -> TrafficGeneratorMetadata | None:
    """TrafficGenerator가 payload 앞에 붙이는 5개 unsigned-int 메타데이터를 읽는다."""
    if len(payload_prefix) < TG_METADATA_SIZE:
        return None

    flow_id, size_bytes, tos, rate_mbps, direction_value = struct.unpack(
        "<IIIII",
        payload_prefix[:TG_METADATA_SIZE],
    )
    if direction_value not in TG_DIRECTION_TO_NAME:
        return None

    # flow_id 0은 TrafficGenerator가 persistent connection 종료를 알릴 때 쓰는 값이다.
    if flow_id == 0:
        return None

    return TrafficGeneratorMetadata(
        flow_id=flow_id,
        size_bytes=size_bytes,
        tos=tos,
        rate_mbps=rate_mbps,
        direction_value=direction_value,
    )


def build_default_src_index_by_ip(
    k: int = 4,
    src_pods: tuple[int, ...] = (0, 1),
) -> dict[str, int]:
    """기본 fat-tree에서 flowGenerator가 srcHosts를 나열하는 순서를 재현한다."""
    index_by_ip: dict[str, int] = {}
    index = 0
    for pod in range(k):
        for edge in range(k // 2):
            for host in range(1, k // 2 + 1):
                ip = f"10.{pod}.{edge}.{host}"
                if pod in src_pods:
                    index_by_ip[ip] = index
                    index += 1
    return index_by_ip


class TrafficGeneratorOnlineFlowCache:
    """XDP packet event를 TrafficGenerator 요청 단위 FlowCache entry로 변환한다.

    TrafficGenerator는 하나의 persistent TCP connection에 여러 요청을 순차적으로
    실을 수 있다. 따라서 5-tuple은 connection만 식별하고, 실제 request 경계는
    각 요청/응답 payload 시작 부분의 TrafficGenerator metadata로 판별한다.
    """

    def __init__(
        self,
        feature_packet_count: int,
        src_index_by_ip: dict[str, int] | None = None,
        server_port: int = 5001,
    ):
        self.flow_cache = FlowCache(feature_packet_count=feature_packet_count)
        self.src_index_by_ip = src_index_by_ip or build_default_src_index_by_ip()
        self.server_port = server_port
        self._states: dict[tuple[str, int, str, int, str], _DirectionalFlowState] = {}

    def process_event(self, event: XdpPacketEvent) -> FlowEntry | None:
        """패킷 이벤트 하나를 처리하고, flow에 매칭되면 갱신된 entry를 반환한다."""
        direction = self._infer_direction(event)
        if direction is None:
            return None

        state_key = self._make_state_key(event, direction)
        state = self._states.get(state_key)
        tg_meta = parse_tg_metadata(event.payload_prefix)

        if tg_meta is not None and tg_meta.direction == direction:
            # 같은 5-tuple이라도 새 metadata가 보이면 새 TrafficGenerator flow가 시작된 것이다.
            state = self._start_flow(event, tg_meta, direction)
            self._states[state_key] = state

        if state is None:
            # metadata를 아직 보지 못한 connection 중간 패킷은 flow_id를 알 수 없어 버린다.
            return None
        if event.tcp_len <= 0:
            return None

        packet = self._event_to_packet_record(event)
        entry = self.flow_cache.add_packet(state.meta, direction, packet)

        state.remaining_tcp_payload_bytes -= event.tcp_len
        if state.remaining_tcp_payload_bytes <= 0:
            # metadata + payload 크기만큼 소비했으면 해당 방향 flow가 끝난 것으로 본다.
            self._states.pop(state_key, None)

        return entry

    def ready_entries(self) -> list[FlowEntry]:
        return [
            entry
            for entry in self.flow_cache.entries.values()
            if self.flow_cache.is_ready(entry)
        ]

    def mark_feature_sent(self, entry: FlowEntry) -> None:
        self.flow_cache.mark_feature_sent(entry)

    def mark_pending(self, entry: FlowEntry) -> None:
        self.flow_cache.mark_pending(entry)

    def _infer_direction(self, event: XdpPacketEvent) -> str | None:
        if event.dst_port == self.server_port:
            return "src_to_dst"
        if event.src_port == self.server_port:
            return "dst_to_src"
        return None

    def _make_state_key(
        self,
        event: XdpPacketEvent,
        direction: str,
    ) -> tuple[str, int, str, int, str]:
        if direction == "src_to_dst":
            client_ip, client_port = event.src_ip, event.src_port
            server_ip, server_port = event.dst_ip, event.dst_port
        else:
            client_ip, client_port = event.dst_ip, event.dst_port
            server_ip, server_port = event.src_ip, event.src_port
        return (client_ip, client_port, server_ip, server_port, direction)

    def _start_flow(
        self,
        event: XdpPacketEvent,
        tg_meta: TrafficGeneratorMetadata,
        direction: str,
    ) -> _DirectionalFlowState:
        if direction == "src_to_dst":
            client_ip, client_port = event.src_ip, event.src_port
            server_ip, server_port = event.dst_ip, event.dst_port
        else:
            client_ip, client_port = event.dst_ip, event.dst_port
            server_ip, server_port = event.src_ip, event.src_port

        return self._start_flow_for_connection(
            client_ip=client_ip,
            client_port=client_port,
            server_ip=server_ip,
            server_port=server_port,
            tg_meta=tg_meta,
            direction=direction,
            start_time_us=event.ts_us,
        )

    def _start_flow_for_connection(
        self,
        *,
        client_ip: str,
        client_port: int,
        server_ip: str,
        server_port: int,
        tg_meta: TrafficGeneratorMetadata,
        direction: str,
        start_time_us: int,
    ) -> _DirectionalFlowState:
        total_payload_bytes = TG_METADATA_SIZE + tg_meta.size_bytes

        src_index = self.src_index_by_ip.get(client_ip)
        if src_index is None:
            raise KeyError(f"missing src_index mapping for client IP {client_ip}")

        meta = RequestMeta(
            src_index=src_index,
            flow_id=tg_meta.flow_id,
            server_id=0,
            connection_id=0,
            src_ip=client_ip,
            src_port=client_port,
            dst_ip=server_ip,
            dst_port=server_port,
            size_bytes=tg_meta.size_bytes,
            dscp=tg_meta.dscp,
            rate_mbps=tg_meta.rate_mbps,
            start_time_us=start_time_us,
            stop_time_us=0,
            fct_us=0,
            src_to_dst_flow_id=f"{src_index}:{tg_meta.flow_id}:src_to_dst",
            dst_to_src_flow_id=f"{src_index}:{tg_meta.flow_id}:dst_to_src",
            src_to_dst_tos=(tg_meta.dscp << 2) | TG_FLOW_DIR_SRC_TO_DST,
            dst_to_src_tos=(tg_meta.dscp << 2) | TG_FLOW_DIR_DST_TO_SRC,
        )
        return _DirectionalFlowState(
            meta=meta,
            remaining_tcp_payload_bytes=total_payload_bytes,
        )

    def _event_to_packet_record(self, event: XdpPacketEvent) -> PacketRecord:
        packet = PacketRecord(
            source_file=event.ifname,
            frame_number=event.frame_number,
            ts_us=event.ts_us,
            src_ip=event.src_ip,
            dst_ip=event.dst_ip,
            src_port=event.src_port,
            dst_port=event.dst_port,
            frame_len=event.frame_len,
            ip_len=event.ip_len,
            ip_hdr_len=event.ip_hdr_len,
            ip_ttl=event.ip_ttl,
            ip_dscp=event.ip_dscp,
            ip_ecn=event.ip_ecn,
            tcp_stream=0,
            tcp_len=event.tcp_len,
            tcp_hdr_len=event.tcp_hdr_len,
            tcp_seq=event.tcp_seq,
            tcp_ack=event.tcp_ack,
            tcp_flags=f"0x{event.tcp_flags:03x}",
            tcp_syn=event.tcp_syn,
            tcp_ack_flag=event.tcp_ack_flag,
            tcp_psh=event.tcp_psh,
            tcp_fin=event.tcp_fin,
            tcp_rst=event.tcp_rst,
            tcp_window_size=event.tcp_window_size,
            tcp_time_relative=0.0,
            tcp_time_delta=0.0,
            retransmission=False,
            out_of_order=False,
            duplicate_ack=False,
            fast_retransmission=False,
        )
        if event.epoch_ts_us is not None:
            packet.epoch_ts_us = event.epoch_ts_us
        return packet
