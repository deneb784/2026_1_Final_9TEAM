from __future__ import annotations

import struct
from dataclasses import dataclass

from pipeline.models import PacketRecord
from pipeline.realtime.online_flow_cache import OnlineFlowEntry, OnlineFlowKey, RealtimeFlowCache


# TrafficGenerator가 TCP payload 맨 앞에 little-endian unsigned int 5개를 붙인다.
# XDP는 payload 전체를 복사하지 않고 앞부분(prefix)만 넘기므로, 여기서는 그 prefix에서
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
class XdpPacketEvent:
    """XDP 프로그램이 관측한 TCP 패킷 1개를 Python 쪽에서 다루기 위한 구조체."""

    # ts_us는 캡처 기준 상대 시간, epoch_ts_us는 가능할 때 제공되는 wall-clock epoch 시간이다.
    ts_us: int
    epoch_ts_us: int | None
    frame_number: int
    ifname: str

    # 5-tuple 중 IP/port 4개. protocol은 이 경로에서 TCP로 이미 필터링되었다고 본다.
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int

    # IP 계층에서 모델 feature로 쓰거나 검증에 필요한 값들.
    frame_len: int
    ip_len: int
    ip_hdr_len: int
    ip_ttl: int
    ip_dscp: int
    ip_ecn: int

    # TCP 계층 값. tcp_len은 payload 길이이며, 0이면 보통 ACK-only 패킷이다.
    tcp_len: int
    tcp_hdr_len: int
    tcp_seq: int
    tcp_ack: int
    tcp_flags: int
    tcp_window_size: int
    # TrafficGenerator metadata를 읽기 위해 XDP가 복사해 준 payload 앞부분.
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
    """현재 TCP connection의 한 방향에서 진행 중인 TrafficGenerator flow 상태."""

    # online_key는 실시간 FlowCache entry를 만들 때 필요한 논리 flow 식별 정보이고,
    # remaining_tcp_payload_bytes는 metadata+payload를 얼마나 더 소비해야 이 flow가 끝나는지 나타낸다.
    online_key: OnlineFlowKey
    request_key: dict | None
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


def build_default_src_index_by_ip(
    k: int = 4,
    src_pods: tuple[int, ...] = (0, 1),
) -> dict[str, int]:
    """기본 fat-tree에서 flowGenerator가 srcHosts를 나열하는 순서를 재현한다.

    실시간 cache key에는 src_index를 쓰지 않는다. 이 매핑은 online 결과를
    offline meta/dataset과 join해 metric을 계산하기 위한 request_key를 붙일 때만 사용한다.
    """
    # src_index는 dataset/label 쪽에서 쓰는 송신 host 번호다.
    # 온라인 XDP 이벤트에는 IP만 있으므로, metric 호환이 필요할 때 IP로부터 보조 key를 만든다.
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

    여기서 만들어진 ready OnlineFlowEntry는 online_request.py에서 모델 입력 payload로 변환되고,
    그 payload는 Redis Stream에 XADD되어 classifier worker가 비동기로 읽어 간다.
    """

    def __init__(
        self,
        feature_packet_count: int,
        src_index_by_ip: dict[str, int] | None = None,
        server_port: int = 5001,
    ):
        self.flow_cache = RealtimeFlowCache(feature_packet_count=feature_packet_count)
        self.src_index_by_ip = src_index_by_ip
        self.server_port = server_port
        # key는 "client/server 4-tuple + 방향"이다. 같은 TCP connection에서도 양방향 요청/응답이
        # 별도 flow로 처리되므로 direction까지 포함한다.
        self._states: dict[tuple[str, int, str, int, str], _DirectionalFlowState] = {}

    def process_event(self, event: XdpPacketEvent) -> OnlineFlowEntry | None:
        """패킷 이벤트 하나를 처리하고, flow에 매칭되면 갱신된 entry를 반환한다."""
        # server_port를 기준으로 client->server인지 server->client인지 먼저 결정한다.
        # TrafficGenerator metadata의 direction과 실제 포트 방향이 일치할 때만 새 flow로 인정한다.
        direction = self._infer_direction(event)
        if direction is None:
            return None

        state_key = self._make_state_key(event, direction)
        state = self._states.get(state_key)
        tg_meta = parse_tg_metadata(event.payload_prefix)

        if tg_meta is not None and tg_meta.direction == direction:
            # 같은 5-tuple이라도 새 metadata가 보이면 새 TrafficGenerator flow가 시작된 것이다.
            # persistent connection 안에 여러 논리 request가 이어 붙는 구조라서 이 처리가 핵심이다.
            state = self._start_flow(event, tg_meta, direction)
            self._states[state_key] = state

        if state is None:
            # metadata를 아직 보지 못한 connection 중간 패킷은 flow_id를 알 수 없어 버린다.
            return None
        if event.tcp_len <= 0:
            # ACK-only 패킷은 모델 feature로 쓰지 않고 payload byte 진행량도 없으므로 건너뛴다.
            return None

        # 실시간 FlowCache는 5-tuple을 client/server 기준으로 정규화한 online flow key로
        # PacketRecord를 모은다. src_index 기반 request_key는 metric 호환용 보조 값일 뿐이다.
        # feature_packet_count만큼 payload 패킷이 쌓이면 ready 상태로 간주된다.
        packet = self._event_to_packet_record(event)
        entry = self.flow_cache.add_packet(
            state.online_key,
            packet,
            request_key=state.request_key,
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

    def _infer_direction(self, event: XdpPacketEvent) -> str | None:
        # 서버가 listen하는 port를 기준으로 방향을 추론한다.
        # dst_port가 server_port면 요청 방향, src_port가 server_port면 응답 방향이다.
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
        # 방향과 무관하게 key의 앞쪽은 client, 뒤쪽은 server가 되도록 정규화한다.
        # 이렇게 해야 응답 방향(dst_to_src)에서도 같은 connection을 안정적으로 찾을 수 있다.
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
        # event의 src/dst는 패킷 방향에 따라 바뀌므로, 먼저 client/server 관점으로 정규화한다.
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
        # TG metadata도 TCP payload 일부로 전송되므로, flow 종료 판단에는 metadata 20바이트까지 포함한다.
        total_payload_bytes = TG_METADATA_SIZE + tg_meta.size_bytes

        online_key = OnlineFlowKey(
            client_ip=client_ip,
            client_port=client_port,
            server_ip=server_ip,
            server_port=server_port,
            flow_id=tg_meta.flow_id,
            direction=direction,
        )
        request_key = None
        if self.src_index_by_ip is not None:
            src_index = self.src_index_by_ip.get(client_ip)
            if src_index is not None:
                request_key = {
                    "src_index": src_index,
                    "flow_id": tg_meta.flow_id,
                    "direction": direction,
                }

        return _DirectionalFlowState(
            online_key=online_key,
            request_key=request_key,
            remaining_tcp_payload_bytes=total_payload_bytes,
        )

    def _event_to_packet_record(self, event: XdpPacketEvent) -> PacketRecord:
        # 기존 dataset builder가 PacketRecord를 입력으로 받으므로,
        # XDP 이벤트를 offline pcap 파싱 결과와 같은 형태로 맞춰준다.
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
