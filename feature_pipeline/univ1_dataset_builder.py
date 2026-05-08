import argparse
import csv
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


TSHARK_FIELDS = [
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
    "tcp.flags",
    "tcp.window_size",
    "tcp.analysis.retransmission",
    "tcp.analysis.out_of_order",
    "tcp.analysis.duplicate_ack",
    "tcp.analysis.fast_retransmission",
]

QUANTILES_FOR_CDF = [
    0.0,
    0.01,
    0.05,
    0.10,
    0.20,
    0.30,
    0.40,
    0.50,
    0.60,
    0.70,
    0.80,
    0.90,
    0.95,
    0.97,
    0.99,
    0.995,
    0.999,
    1.0,
]


@dataclass
class DirectionState:
    packet_count: int = 0
    payload_bytes: int = 0
    first_ts_us: int | None = None
    last_ts_us: int | None = None
    prev_ts_us: int | None = None
    feature_packets: list[list[int]] = field(default_factory=list)


@dataclass
class StreamState:
    source_file: str
    tcp_stream: int
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    first_ts_us: int
    last_ts_us: int
    fwd: DirectionState = field(default_factory=DirectionState)
    rev: DirectionState = field(default_factory=DirectionState)


def to_int(value: str, default: int = 0) -> int:
    if value == "":
        return default
    if "," in value:
        value = value.split(",", 1)[0]
    return int(float(value))


def to_ts_us(value: str) -> int:
    if "," in value:
        value = value.split(",", 1)[0]
    return int(float(value) * 1_000_000)


def tcp_flags_to_int(value: str) -> int:
    if value == "":
        return 0
    if "," in value:
        value = value.split(",", 1)[0]
    try:
        return int(value, 16)
    except ValueError:
        return 0


def to_flag(value: str) -> int:
    return 0 if value in ("", "0", "False", "false") else 1


def find_univ1_pcaps(pcap_dir: Path) -> list[Path]:
    files = []
    for path in pcap_dir.glob("univ1_pt*.pcap"):
        if "old" in path.stem:
            continue
        suffix = path.stem.replace("univ1_pt", "")
        if suffix.isdigit() and 1 <= int(suffix) <= 20:
            files.append(path)
    return sorted(files, key=lambda path: int(path.stem.replace("univ1_pt", "")))


def iter_tshark_rows(path: Path):
    cmd = ["tshark", "-r", str(path), "-Y", "tcp", "-T", "fields"]
    for field_name in TSHARK_FIELDS:
        cmd.extend(["-e", field_name])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert proc.stdout is not None
    for line in proc.stdout:
        values = line.rstrip("\n").split("\t")
        if len(values) < len(TSHARK_FIELDS):
            values.extend([""] * (len(TSHARK_FIELDS) - len(values)))
        elif len(values) > len(TSHARK_FIELDS):
            values = values[: len(TSHARK_FIELDS)]
        yield dict(zip(TSHARK_FIELDS, values))

    stderr = proc.stderr.read() if proc.stderr is not None else ""
    returncode = proc.wait()
    if returncode != 0:
        raise RuntimeError(f"tshark failed for {path}: {stderr}")


def add_packet_to_direction(
    direction: DirectionState,
    row: dict[str, str],
    ts_us: int,
    packet_count: int,
) -> None:
    iat_us = 0 if direction.prev_ts_us is None else ts_us - direction.prev_ts_us
    direction.prev_ts_us = ts_us

    direction.packet_count += 1
    direction.payload_bytes += to_int(row["tcp.len"])
    direction.first_ts_us = ts_us if direction.first_ts_us is None else min(direction.first_ts_us, ts_us)
    direction.last_ts_us = ts_us if direction.last_ts_us is None else max(direction.last_ts_us, ts_us)

    if len(direction.feature_packets) >= packet_count:
        return

    direction.feature_packets.append([
        to_int(row["frame.len"]),
        to_int(row["ip.len"]),
        to_int(row["ip.ttl"]),
        to_int(row["tcp.len"]),
        tcp_flags_to_int(row["tcp.flags"]),
        to_int(row["tcp.window_size"]),
        iat_us,
        to_flag(row["tcp.analysis.retransmission"]),
        to_flag(row["tcp.analysis.out_of_order"]),
        to_flag(row["tcp.analysis.duplicate_ack"]),
        to_flag(row["tcp.analysis.fast_retransmission"]),
    ])


def build_stream_states(pcap_files: list[Path], packet_count: int) -> dict[tuple[str, int], StreamState]:
    streams: dict[tuple[str, int], StreamState] = {}

    for pcap_file in pcap_files:
        print(f"processing {pcap_file}")
        source_file = pcap_file.name

        for row in iter_tshark_rows(pcap_file):
            if row["tcp.stream"] == "" or row["ip.src"] == "" or row["ip.dst"] == "":
                continue

            tcp_stream = to_int(row["tcp.stream"])
            ts_us = to_ts_us(row["frame.time_epoch"])
            src_ip = row["ip.src"]
            dst_ip = row["ip.dst"]
            src_port = to_int(row["tcp.srcport"])
            dst_port = to_int(row["tcp.dstport"])

            key = (source_file, tcp_stream)
            stream = streams.get(key)

            if stream is None:
                stream = StreamState(
                    source_file=source_file,
                    tcp_stream=tcp_stream,
                    src_ip=src_ip,
                    src_port=src_port,
                    dst_ip=dst_ip,
                    dst_port=dst_port,
                    first_ts_us=ts_us,
                    last_ts_us=ts_us,
                )
                streams[key] = stream

            stream.first_ts_us = min(stream.first_ts_us, ts_us)
            stream.last_ts_us = max(stream.last_ts_us, ts_us)

            if src_ip == stream.src_ip and src_port == stream.src_port and dst_ip == stream.dst_ip and dst_port == stream.dst_port:
                add_packet_to_direction(stream.fwd, row, ts_us, packet_count)
            elif src_ip == stream.dst_ip and src_port == stream.dst_port and dst_ip == stream.src_ip and dst_port == stream.src_port:
                add_packet_to_direction(stream.rev, row, ts_us, packet_count)

    return streams


def percentile(sorted_values: list[int], q: float) -> int:
    if not sorted_values:
        return 0
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    index = int((len(sorted_values) - 1) * q)
    return sorted_values[index]


def flow_size_bytes(stream: StreamState) -> int:
    return max(stream.fwd.payload_bytes, stream.rev.payload_bytes)


def dominant_direction(stream: StreamState) -> tuple[str, DirectionState]:
    if stream.rev.payload_bytes > stream.fwd.payload_bytes:
        return "dst_to_src", stream.rev
    return "src_to_dst", stream.fwd


def write_stats_csv(streams: list[StreamState], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "source_file",
            "tcp_stream",
            "src_ip",
            "src_port",
            "dst_ip",
            "dst_port",
            "packets_fwd",
            "packets_rev",
            "bytes_fwd",
            "bytes_rev",
            "flow_size_bytes",
            "duration_us",
        ])
        for stream in streams:
            writer.writerow([
                stream.source_file,
                stream.tcp_stream,
                stream.src_ip,
                stream.src_port,
                stream.dst_ip,
                stream.dst_port,
                stream.fwd.packet_count,
                stream.rev.packet_count,
                stream.fwd.payload_bytes,
                stream.rev.payload_bytes,
                flow_size_bytes(stream),
                stream.last_ts_us - stream.first_ts_us,
            ])


def write_dataset_jsonl(
    streams: list[StreamState],
    threshold: int,
    packet_count: int,
    output_path: Path,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_path.open("w", encoding="utf-8") as f:
        for flow_id, stream in enumerate(streams, start=1):
            direction_name, direction = dominant_direction(stream)
            if flow_size_bytes(stream) <= 0 or len(direction.feature_packets) < packet_count:
                continue

            sample = {
                "flow_key": {
                    "src_index": 0,
                    "flow_id": flow_id,
                    "direction": direction_name,
                },
                "trace_key": {
                    "source_file": stream.source_file,
                    "tcp_stream": stream.tcp_stream,
                },
                "x": direction.feature_packets[:packet_count],
                "directional_size_bytes": direction.payload_bytes,
                "flow_size_bytes": flow_size_bytes(stream),
                "label": 1 if flow_size_bytes(stream) >= threshold else 0,
            }
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")
            count += 1
    return count


def write_cdf(sizes: list[int], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_sizes = sorted(sizes)
    rows: list[tuple[int, float]] = []

    for q in QUANTILES_FOR_CDF:
        value = percentile(sorted_sizes, q)
        if rows and rows[-1][0] == value:
            rows[-1] = (value, q)
        else:
            rows.append((value, q))

    if not rows:
        rows.append((0, 0.0))
    elif rows[0] != (0, 0.0):
        rows.insert(0, (0, 0.0))
    if rows[-1][1] != 1.0:
        rows.append((sorted_sizes[-1], 1.0))

    with output_path.open("w", encoding="utf-8") as f:
        for value, q in rows:
            f.write(f"{value} {q:g}\n")


def is_dataset_candidate(stream: StreamState, packet_count: int) -> bool:
    direction_name, direction = dominant_direction(stream)
    return bool(direction_name) and flow_size_bytes(stream) > 0 and len(direction.feature_packets) >= packet_count


def write_summary(
    streams: list[StreamState],
    sizes: list[int],
    dataset_candidate_sizes: list[int],
    threshold: int,
    output_path: Path,
) -> None:
    sorted_sizes = sorted(sizes)
    sorted_candidate_sizes = sorted(dataset_candidate_sizes)
    summary = {
        "flow_count": len(sizes),
        "packet_stream_count": len(streams),
        "dataset_candidate_count": len(dataset_candidate_sizes),
        "elephant_threshold_dataset_p80_bytes": threshold,
        "dataset_elephant_count": sum(1 for size in dataset_candidate_sizes if size >= threshold),
        "min_bytes": sorted_sizes[0] if sorted_sizes else 0,
        "avg_bytes": sum(sorted_sizes) / len(sorted_sizes) if sorted_sizes else 0,
        "p50_bytes": percentile(sorted_sizes, 0.50),
        "p80_bytes": percentile(sorted_sizes, 0.80),
        "p90_bytes": percentile(sorted_sizes, 0.90),
        "p95_bytes": percentile(sorted_sizes, 0.95),
        "p99_bytes": percentile(sorted_sizes, 0.99),
        "max_bytes": sorted_sizes[-1] if sorted_sizes else 0,
        "dataset_p50_bytes": percentile(sorted_candidate_sizes, 0.50),
        "dataset_p80_bytes": percentile(sorted_candidate_sizes, 0.80),
        "dataset_p90_bytes": percentile(sorted_candidate_sizes, 0.90),
        "dataset_p95_bytes": percentile(sorted_candidate_sizes, 0.95),
        "dataset_p99_bytes": percentile(sorted_candidate_sizes, 0.99),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pcap-dir", default="data/univ1_trace")
    parser.add_argument("--packet-count", type=int, default=8)
    parser.add_argument("--dataset-out", default="dataset_univ1.jsonl")
    parser.add_argument("--stats-out", default="analyze/univ1_flow_stats.csv")
    parser.add_argument("--summary-out", default="analyze/univ1_summary.json")
    parser.add_argument("--cdf-out", default="TrafficGenerator/conf/UNI1_CDF.txt")
    args = parser.parse_args()

    pcap_files = find_univ1_pcaps(Path(args.pcap_dir))
    if not pcap_files:
        raise SystemExit(f"no univ1_pt*.pcap files found in {args.pcap_dir}")

    streams_by_key = build_stream_states(pcap_files, args.packet_count)
    streams = list(streams_by_key.values())
    sizes = [flow_size_bytes(stream) for stream in streams if flow_size_bytes(stream) > 0]
    dataset_candidate_sizes = [
        flow_size_bytes(stream)
        for stream in streams
        if is_dataset_candidate(stream, args.packet_count)
    ]
    threshold = percentile(sorted(dataset_candidate_sizes), 0.80)

    write_stats_csv(streams, Path(args.stats_out))
    dataset_count = write_dataset_jsonl(streams, threshold, args.packet_count, Path(args.dataset_out))
    write_cdf(sizes, Path(args.cdf_out))
    write_summary(streams, sizes, dataset_candidate_sizes, threshold, Path(args.summary_out))

    print(f"pcap files: {len(pcap_files)}")
    print(f"tcp streams: {len(streams)}")
    print(f"flows with payload: {len(sizes)}")
    print(f"dataset candidate flows: {len(dataset_candidate_sizes)}")
    print(f"dataset elephant threshold p80: {threshold} bytes")
    print(f"dataset samples: {dataset_count}")
    print(f"wrote {args.dataset_out}")
    print(f"wrote {args.cdf_out}")
    print(f"wrote {args.stats_out}")
    print(f"wrote {args.summary_out}")


if __name__ == "__main__":
    main()
