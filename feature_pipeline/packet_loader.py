import subprocess
from pathlib import Path

from feature_pipeline.models import PacketRecord


FIELDS = [
    "frame.number",
    "frame.time_epoch",
    "frame.len",
    "ip.src",
    "ip.dst",
    "ip.len",
    "ip.ttl",
    "tcp.srcport",
    "tcp.dstport",
    "tcp.stream",
    "tcp.len",
    "tcp.seq",
    "tcp.ack",
    "tcp.flags",
    "tcp.window_size",
    "tcp.time_relative",
    "tcp.time_delta",
    "tcp.analysis.retransmission",
    "tcp.analysis.out_of_order",
    "tcp.analysis.duplicate_ack",
    "tcp.analysis.fast_retransmission",
]


def _to_int(value: str, default: int = 0) -> int:
    if value is None or value == "":
        return default
    return int(float(value))


def _to_float(value: str, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _to_bool(value: str) -> bool:
    return value not in ("", None, "0", "False", "false")


def parse_tshark_values(source_file: str, values: list[str]) -> PacketRecord:
    value_map = dict(zip(FIELDS, values))
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
        ip_ttl=_to_int(value_map.get("ip.ttl", "")),

        tcp_stream=_to_int(value_map.get("tcp.stream", "")),
        tcp_len=_to_int(value_map.get("tcp.len", "")),
        tcp_seq=_to_int(value_map.get("tcp.seq", "")),
        tcp_ack=_to_int(value_map.get("tcp.ack", "")),
        tcp_flags=value_map.get("tcp.flags", ""),
        tcp_window_size=_to_int(value_map.get("tcp.window_size", "")),

        tcp_time_relative=_to_float(value_map.get("tcp.time_relative", "")),
        tcp_time_delta=_to_float(value_map.get("tcp.time_delta", "")),

        retransmission=_to_bool(value_map.get("tcp.analysis.retransmission", "")),
        out_of_order=_to_bool(value_map.get("tcp.analysis.out_of_order", "")),
        duplicate_ack=_to_bool(value_map.get("tcp.analysis.duplicate_ack", "")),
        fast_retransmission=_to_bool(value_map.get("tcp.analysis.fast_retransmission", "")),
    )


def iter_packets_from_pcap(path: str | Path):
    path = Path(path)

    cmd = ["tshark", "-r", str(path), "-T", "fields"]
    for field in FIELDS:
        cmd.extend(["-e", field])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
    )

    for line in result.stdout.splitlines():
        values = line.split("\t")

        if len(values) < len(FIELDS):
            values += [""] * (len(FIELDS) - len(values))
        elif len(values) > len(FIELDS):
            values = values[:len(FIELDS)]

        yield parse_tshark_values(path.name, values)
