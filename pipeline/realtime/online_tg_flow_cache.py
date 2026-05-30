from __future__ import annotations

import struct
from dataclasses import dataclass

from pipeline.models import PacketRecord
from pipeline.realtime.online_flow_cache import OnlineFlowEntry, OnlineFlowKey, RealtimeFlowCache


# TrafficGenerator가 TCP payload 맨 앞에 little-endian unsigned int 5개를 붙인다.
# 온라인 캡처 경로는 payload 앞부분(prefix)만 넘기므로, 여기서는 그 prefix에서
# request 경계를 찾는다. 즉, TCP 5-tuple이 아니라 TG metadata가 "논리 flow"의 시작 신호다.
TG_METADATA_SIZE = 20
TG_FLOW_DIR_SRC_TO_DST = 0
TG_FLOW_DIR_DST_TO_SRC = 1

TG_DIRECTION_TO_NAME = {
    TG_FLOW_DIR_SRC_TO_DST: "src_to_dst",
    TG_FLOW_DIR_DST_TO_SRC: "dst_to_src",
}


@dataclass(frozen=True)
class TrafficGeneratorMetadata:
    """TrafficGenerator payload prefix에서 읽은 논리 요청 메타데이터."""

    flow_id: int
    size_bytes: int
    tos: int
    rate_mbps: int
    direction_value: int

    @property
    def direction(self) -> str:
        # payload 안 direction 값은 정수라서, 파이프라인에서 쓰는 문자열 방향으로 바꾼다.
        return TG_DIRECTION_TO_NAME[self.direction_value]

    @property
    def dscp(self) -> int:
        # IPv4 TOS의 상위 6bit가 DSCP이고, 하위 2bit는 ECN이다.
        return self.tos >> 2


@dataclass(frozen=True)
class OnlinePacketEvent:
    """온라인 캡처 경로가 관측한 TCP 패킷 1개를 Python 쪽에서 다루기 위한 구조체."""

    # 시간/캡처 위치 정보.
    ts_us: int  # 패킷 시간(us).
    epoch_ts_us: int | None  # 실제 시각 기준 패킷 시간(us).
    frame_number: int  # 캡처된 패킷 순번.
    ifname: str  # 패킷을 본 인터페이스 이름.

    # IP/port 정보.
    src_ip: str  # 출발지 IP.
    dst_ip: str  # 목적지 IP.
    src_port: int  # 출발지 TCP port.
    dst_port: int  # 목적지 TCP port.

    # IP 계층 정보.
    frame_len: int  # Ethernet frame 전체 길이.
    ip_len: int  # IP packet 길이.
    ip_hdr_len: int  # IP header 길이.
    ip_ttl: int  # IP TTL.
    ip_dscp: int  # IP DSCP 값.
    ip_ecn: int  # IP ECN 값.

    # TCP 계층 정보.
    tcp_len: int  # TCP payload 길이.
    tcp_hdr_len: int  # TCP header 길이.
    tcp_seq: int  # TCP sequence number.
    tcp_ack: int  # TCP ACK number.
    tcp_flags: int  # TCP flags 값.
    tcp_window_size: int  # TCP window size.
    payload_prefix: bytes = b""  # TCP payload 앞 20B.

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
    """현재 TCP connection의 한 방향에서 진행 중인 TrafficGenerator flow 상태."""

    # online_key는 실시간 FlowCache entry를 만들 때 필요한 논리 flow 식별 정보이고,
    # remaining_tcp_payload_bytes는 metadata+payload를 얼마나 더 소비해야 이 flow가 끝나는지 나타낸다.
    online_key: OnlineFlowKey
    direction: str
    remaining_tcp_payload_bytes: int


def parse_tg_metadata(payload_prefix: bytes) -> TrafficGeneratorMetadata | None:
    """TrafficGenerator가 payload 앞에 붙이는 5개 unsigned-int 메타데이터를 읽는다."""
    # payload prefix가 20바이트보다 짧으면 아직 TG metadata를 판단할 수 없다.
    if len(payload_prefix) < TG_METADATA_SIZE:
        return None

    # TrafficGenerator 쪽 C/C++ 구조와 맞추기 위해 little-endian unsigned int 5개로 해석한다.
    flow_id, size_bytes, tos, rate_mbps, direction_value = struct.unpack(
        "<IIIII",
        payload_prefix[:TG_METADATA_SIZE],
    )
    # direction 값이 약속된 0/1이 아니면 TG flow 시작 패킷으로 보지 않는다.
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


class TrafficGeneratorOnlineFlowCache:
    """online packet event를 TrafficGenerator 요청 단위 FlowCache entry로 변환한다.

    TrafficGenerator는 하나의 persistent TCP connection에 여러 요청을 순차적으로
    실을 수 있다. 따라서 5-tuple은 connection만 식별하고, 실제 request 경계는
    각 요청/응답 payload 시작 부분의 TrafficGenerator metadata로 판별한다.

    여기서 만들어진 ready OnlineFlowEntry는 online_request.py에서 모델 입력 payload로 변환되고,
    그 payload는 Redis Stream에 XADD되어 classifier worker가 비동기로 읽어 간다.
    """

    def __init__(
        self,
        feature_packet_count: int,
        server_port: int = 5001,
    ):
        self.flow_cache = RealtimeFlowCache(feature_packet_count=feature_packet_count)
        self.server_port = server_port
        # key는 패킷 방향 그대로의 4-tuple이다. client/server 정규화는 하지 않는다.
        self._states: dict[tuple[str, int, str, int], _DirectionalFlowState] = {}

    def process_event(self, event: OnlinePacketEvent) -> OnlineFlowEntry | None:
        """패킷 이벤트 하나를 처리하고, flow에 매칭되면 갱신된 entry를 반환한다."""
        tg_meta = parse_tg_metadata(event.payload_prefix)

        if tg_meta is not None:
            # TrafficGenerator metadata가 논리 flow의 시작과 방향을 알려준다.
            # 포트 기반 방향 추론은 통계/보조 정보로만 쓰고, flow direction은 TG 값을 신뢰한다.
            direction = tg_meta.direction
            state_key = self._make_state_key(event)
            state = self._start_flow(event, tg_meta, direction)
            self._states[state_key] = state
        else:
            matched = self._find_state_for_event(event)
            if matched is None:
                state_key = None
                state = None
            else:
                state_key, state = matched

        if state is None or state_key is None:
            # metadata를 아직 보지 못한 connection 중간 패킷은 flow_id를 알 수 없어 버린다.
            return None
        if event.tcp_len <= 0:
            # ACK-only 패킷은 모델 feature로 쓰지 않고 payload byte 진행량도 없으므로 건너뛴다.
            return None

        # 실시간 FlowCache는 패킷의 src/dst 방향을 그대로 쓰는 online flow key로 PacketRecord를 모은다.
        # feature_packet_count만큼 payload 패킷이 쌓이면 ready 상태로 간주된다.
        packet = self._event_to_packet_record(event)
        entry = self.flow_cache.add_packet(
            state.online_key,
            state.direction,
            packet,
        )

        state.remaining_tcp_payload_bytes -= event.tcp_len
        if state.remaining_tcp_payload_bytes <= 0:
            # metadata + payload 크기만큼 소비했으면 해당 방향 flow가 끝난 것으로 본다.
            self._states.pop(state_key, None)

        return entry

    def mark_pending(self, entry: OnlineFlowEntry) -> None:
        # Redis Stream에 올렸지만 결과를 아직 기다리는 flow는 pending으로 표시한다.
        self.flow_cache.mark_pending(entry)

    def apply_classification_result(self, result: dict) -> OnlineFlowEntry | None:
        # classifier worker가 돌려준 elephant/mice 결과를 내부 FlowCache entry에 반영한다.
        return self.flow_cache.apply_classification_result(result)

    def _infer_direction(self, event: OnlinePacketEvent) -> str | None:
        # 통계/보조 판단용 포트 기반 방향 추론이다.
        # FlowCache의 실제 direction은 TrafficGenerator metadata 값을 신뢰한다.
        if event.dst_port == self.server_port:
            return "src_to_dst"
        if event.src_port == self.server_port:
            return "dst_to_src"
        return None

    def _find_state_for_event(
        self,
        event: OnlinePacketEvent,
    ) -> tuple[tuple[str, int, str, int], _DirectionalFlowState] | None:
        # metadata가 없는 후속 패킷은 같은 src/dst 4-tuple의 active state에 이어 붙인다.
        state_key = self._make_state_key(event)
        state = self._states.get(state_key)
        if state is None:
            return None
        return state_key, state

    def _make_state_key(self, event: OnlinePacketEvent) -> tuple[str, int, str, int]:
        # client/server로 정규화하지 않고 패킷에 찍힌 src/dst를 그대로 쓴다.
        return (event.src_ip, event.src_port, event.dst_ip, event.dst_port)

    def _start_flow(
        self,
        event: OnlinePacketEvent,
        tg_meta: TrafficGeneratorMetadata,
        direction: str,
    ) -> _DirectionalFlowState:
        return self._start_flow_for_connection(
            src_ip=event.src_ip,
            src_port=event.src_port,
            dst_ip=event.dst_ip,
            dst_port=event.dst_port,
            tg_meta=tg_meta,
            direction=direction,
            start_time_us=event.ts_us,
        )

    def _start_flow_for_connection(
        self,
        *,
        src_ip: str,
        src_port: int,
        dst_ip: str,
        dst_port: int,
        tg_meta: TrafficGeneratorMetadata,
        direction: str,
        start_time_us: int,
    ) -> _DirectionalFlowState:
        # TG metadata도 TCP payload 일부로 전송되므로, flow 종료 판단에는 metadata 20바이트까지 포함한다.
        total_payload_bytes = TG_METADATA_SIZE + tg_meta.size_bytes

        online_key = OnlineFlowKey(
            src_ip=src_ip,
            src_port=src_port,
            dst_ip=dst_ip,
            dst_port=dst_port,
            flow_id=tg_meta.flow_id,
        )
        return _DirectionalFlowState(
            online_key=online_key,
            direction=direction,
            remaining_tcp_payload_bytes=total_payload_bytes,
        )

    def _event_to_packet_record(self, event: OnlinePacketEvent) -> PacketRecord:
        # 기존 dataset builder가 PacketRecord를 입력으로 받으므로,
        # 온라인 패킷 이벤트를 offline pcap 파싱 결과와 같은 형태로 맞춰준다.
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
            # latency 분석용 wall-clock timestamp는 PacketRecord 기본 필드가 아니라 동적으로 붙인다.
            packet.epoch_ts_us = event.epoch_ts_us
        return packet
