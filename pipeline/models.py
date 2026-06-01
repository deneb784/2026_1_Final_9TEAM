from dataclasses import dataclass, field


FLOW_STATUSES = {"default", "pending", "elephant", "mice"}


@dataclass
class RequestMeta:
    src_index: int
    flow_id: int
    server_id: int
    connection_id: int
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    size_bytes: int
    dscp: int
    rate_mbps: int
    start_time_us: int
    stop_time_us: int
    fct_us: int
    src_to_dst_flow_id: str
    dst_to_src_flow_id: str
    src_to_dst_tos: int
    dst_to_src_tos: int


@dataclass
class PacketRecord:
    source_file: str  # pcap 파일명 또는 실시간 관측 인터페이스 이름.
    frame_number: int  # 캡처된 패킷 순번.
    ts_us: int  # 패킷 시간(us).

    src_ip: str  # 출발지 IP.
    dst_ip: str  # 목적지 IP.
    src_port: int  # 출발지 TCP port.
    dst_port: int  # 목적지 TCP port.

    frame_len: int  # Ethernet frame 전체 길이.
    ip_len: int  # IP packet 길이.
    ip_hdr_len: int  # IP header 길이.
    ip_ttl: int  # IP TTL.
    ip_dscp: int  # IP DSCP 값.
    ip_ecn: int  # IP ECN 값.

    tcp_stream: int  # tshark TCP stream 번호. 실시간 경로에서는 0.
    tcp_len: int  # TCP payload 길이.
    tcp_hdr_len: int  # TCP header 길이.
    tcp_seq: int  # TCP sequence number.
    tcp_ack: int  # TCP ACK number.
    tcp_flags: str  # TCP flags 문자열.
    tcp_syn: int  # SYN flag 여부.
    tcp_ack_flag: int  # ACK flag 여부.
    tcp_psh: int  # PSH flag 여부.
    tcp_fin: int  # FIN flag 여부.
    tcp_rst: int  # RST flag 여부.
    tcp_window_size: int  # TCP window size.

    tcp_time_relative: float  # tshark 상대 시간. 실시간 경로에서는 0.0.
    tcp_time_delta: float  # tshark 이전 패킷과의 시간 차. 실시간 경로에서는 0.0.

    retransmission: bool  # 재전송 여부. 실시간 경로에서는 False.
    out_of_order: bool  # 순서 뒤바뀜 여부. 실시간 경로에서는 False.
    duplicate_ack: bool  # duplicate ACK 여부. 실시간 경로에서는 False.
    fast_retransmission: bool  # fast retransmission 여부. 실시간 경로에서는 False.


@dataclass
class FlowEntry:
    src_index: int
    flow_id: int
    direction: str
    logical_flow_id: str

    packets: list[PacketRecord] = field(default_factory=list)
    payload_bytes: int = 0
    status: str = "default"
    model_score: float | None = None
    classification_result: dict | None = None

    @property
    def flow_key(self) -> tuple[int, int, str]:
        return (self.src_index, self.flow_id, self.direction)
