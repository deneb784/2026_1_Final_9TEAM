from dataclasses import dataclass, field


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
    source_file: str
    frame_number: int
    ts_us: int

    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int

    frame_len: int
    ip_len: int
    ip_ttl: int

    tcp_stream: int
    tcp_len: int
    tcp_seq: int
    tcp_ack: int
    tcp_flags: str
    tcp_window_size: int

    tcp_time_relative: float
    tcp_time_delta: float

    retransmission: bool
    out_of_order: bool
    duplicate_ack: bool
    fast_retransmission: bool


@dataclass
class FlowEntry:
    src_index: int
    flow_id: int
    direction: str
    logical_flow_id: str

    packets: list[PacketRecord] = field(default_factory=list)
    feature_sent: bool = False

    @property
    def flow_key(self) -> tuple[int, int, str]:
        return (self.src_index, self.flow_id, self.direction)