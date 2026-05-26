import subprocess
from pathlib import Path

from pipeline.models import PacketRecord


FIELDS = [
    # tshark에서 뽑아올 필드 목록이다. parse_tshark_values()의 매핑 순서와 같아야 한다.
    "frame.number",
    "frame.time_epoch",
    "frame.len",
    "ip.src",
    "ip.dst",
    "ip.len",
    "ip.hdr_len",
    "ip.ttl",
    "ip.dsfield.dscp",
    "ip.dsfield.ecn",
    "tcp.srcport",
    "tcp.dstport",
    "tcp.stream",
    "tcp.len",
    "tcp.hdr_len",
    "tcp.seq",
    "tcp.ack",
    "tcp.flags",
    "tcp.flags.syn",
    "tcp.flags.ack",
    "tcp.flags.push",
    "tcp.flags.fin",
    "tcp.flags.reset",
    "tcp.window_size",
    "tcp.time_relative",
    "tcp.time_delta",
    "tcp.analysis.retransmission",
    "tcp.analysis.out_of_order",
    "tcp.analysis.duplicate_ack",
    "tcp.analysis.fast_retransmission",
]


def _to_int(value: str, default: int = 0) -> int:
    """tshark field 값을 int로 변환한다."""
    if value is None or value == "":
        return default
    if "," in value:
        # tshark가 같은 필드를 여러 값으로 출력하면 첫 값만 사용한다.
        value = value.split(",", 1)[0]
    if value in ("True", "true"):
        return 1
    if value in ("False", "false"):
        return 0
    return int(float(value))


def _to_float(value: str, default: float = 0.0) -> float:
    """tshark field 값을 float으로 변환한다."""
    if value is None or value == "":
        return default
    if "," in value:
        value = value.split(",", 1)[0]
    return float(value)


def _to_bool(value: str) -> bool:
    """tcp.analysis.* 필드처럼 값 존재 여부가 의미인 필드를 bool로 변환한다."""
    return value not in ("", None, "0", "False", "false")


def parse_tshark_values(source_file: str, values: list[str]) -> PacketRecord:
    """tshark 한 줄 출력을 PacketRecord dataclass로 변환한다."""
    value_map = dict(zip(FIELDS, values))
    # frame.time_epoch는 초 단위 float이므로 microsecond 정수로 바꿔 일관되게 사용한다.
    ts_us = int(float(value_map["frame.time_epoch"]) * 1_000_000)

    return PacketRecord(
        source_file=source_file,
        frame_number=_to_int(value_map.get("frame.number", "")),
        ts_us=ts_us,

        src_ip=value_map.get("ip.src", ""),
        dst_ip=value_map.get("ip.dst", ""),
        src_port=_to_int(value_map.get("tcp.srcport", "")),
        dst_port=_to_int(value_map.get("tcp.dstport", "")),

        frame_len=_to_int(value_map.get("frame.len", "")),
        ip_len=_to_int(value_map.get("ip.len", "")),
        ip_hdr_len=_to_int(value_map.get("ip.hdr_len", "")),
        ip_ttl=_to_int(value_map.get("ip.ttl", "")),
        ip_dscp=_to_int(value_map.get("ip.dsfield.dscp", "")),
        ip_ecn=_to_int(value_map.get("ip.dsfield.ecn", "")),

        tcp_stream=_to_int(value_map.get("tcp.stream", "")),
        tcp_len=_to_int(value_map.get("tcp.len", "")),
        tcp_hdr_len=_to_int(value_map.get("tcp.hdr_len", "")),
        tcp_seq=_to_int(value_map.get("tcp.seq", "")),
        tcp_ack=_to_int(value_map.get("tcp.ack", "")),
        tcp_flags=value_map.get("tcp.flags", ""),
        tcp_syn=_to_int(value_map.get("tcp.flags.syn", "")),
        tcp_ack_flag=_to_int(value_map.get("tcp.flags.ack", "")),
        tcp_psh=_to_int(value_map.get("tcp.flags.push", "")),
        tcp_fin=_to_int(value_map.get("tcp.flags.fin", "")),
        tcp_rst=_to_int(value_map.get("tcp.flags.reset", "")),
        tcp_window_size=_to_int(value_map.get("tcp.window_size", "")),

        tcp_time_relative=_to_float(value_map.get("tcp.time_relative", "")),
        tcp_time_delta=_to_float(value_map.get("tcp.time_delta", "")),

        retransmission=_to_bool(value_map.get("tcp.analysis.retransmission", "")),
        out_of_order=_to_bool(value_map.get("tcp.analysis.out_of_order", "")),
        duplicate_ack=_to_bool(value_map.get("tcp.analysis.duplicate_ack", "")),
        fast_retransmission=_to_bool(value_map.get("tcp.analysis.fast_retransmission", "")),
    )


def iter_packets_from_pcap(path: str | Path):
    """pcap 파일을 tshark로 읽어서 PacketRecord를 하나씩 생성한다."""
    path = Path(path)

    # -T fields와 -e를 사용해 필요한 TCP/IP 필드만 탭 구분 텍스트로 출력한다.
    cmd = ["tshark", "-n", "-r", str(path), "-Y", "tcp", "-T", "fields"]
    for field in FIELDS:
        cmd.extend(["-e", field])

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            values = line.split("\t")

            if len(values) < len(FIELDS):
                # 비어 있는 trailing field는 split 결과에 빠질 수 있어 빈 문자열로 보정한다.
                values += [""] * (len(FIELDS) - len(values))
            elif len(values) > len(FIELDS):
                # 예상보다 많은 값이 나온 경우 현재 pipeline에서 쓰는 필드 수까지만 사용한다.
                values = values[:len(FIELDS)]

            yield parse_tshark_values(path.name, values)

        return_code = process.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(
                return_code,
                cmd,
            )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
