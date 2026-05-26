import argparse
import csv
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


TSHARK_FIELDS = [
    # UNI1 pcap에서 직접 뽑아올 tshark 필드 목록이다.
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
    "tcp.flags",
    "tcp.flags.syn",
    "tcp.flags.ack",
    "tcp.flags.push",
    "tcp.flags.fin",
    "tcp.flags.reset",
    "tcp.window_size",
    "tcp.analysis.retransmission",
    "tcp.analysis.out_of_order",
    "tcp.analysis.duplicate_ack",
    "tcp.analysis.fast_retransmission",
]

QUANTILES_FOR_CDF = [
    # TrafficGenerator CDF 파일로 저장할 분위수 지점들이다.
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

FEATURE_NAMES = [
    # dataset JSONL의 x 행 벡터 순서다. add_packet_to_direction()의 feature_packet과 같아야 한다.
    "direction",
    "frame_len",
    "ip_len",
    "ip_hdr_len",
    "ip_ttl",
    "ip_dscp",
    "ip_ecn",
    "tcp_len",
    "tcp_hdr_len",
    "tcp_syn",
    "tcp_ack",
    "tcp_psh",
    "tcp_fin",
    "tcp_rst",
    "tcp_window_size",
    "iat_us",
    "elapsed_us",
    "cum_payload_bytes",
]

DIRECTION_TO_VALUE = {
    "src_to_dst": 0,
    "dst_to_src": 1,
}


@dataclass
class DirectionState:
    """TCP stream의 한 방향(src_to_dst 또는 dst_to_src) 누적 상태."""

    packet_count: int = 0
    payload_bytes: int = 0
    first_ts_us: int | None = None
    last_ts_us: int | None = None
    prev_ts_us: int | None = None
    feature_packets: list[list[int]] = field(default_factory=list)


@dataclass
class StreamState:
    """pcap 파일 안의 tcp.stream 하나를 양방향 flow 단위로 추적하는 상태."""

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
    """tshark field 값을 int로 변환한다."""
    if value == "":
        return default
    if "," in value:
        # tshark가 같은 필드를 여러 값으로 출력하면 첫 번째 값만 사용한다.
        value = value.split(",", 1)[0]
    if value in ("True", "true"):
        return 1
    if value in ("False", "false"):
        return 0
    return int(float(value))


def to_ts_us(value: str) -> int:
    """초 단위 epoch timestamp를 microsecond 정수로 변환한다."""
    if "," in value:
        value = value.split(",", 1)[0]
    return int(float(value) * 1_000_000)


def tcp_flags_to_int(value: str) -> int:
    """16진수 TCP flags 문자열을 정수로 변환한다."""
    if value == "":
        return 0
    if "," in value:
        value = value.split(",", 1)[0]
    try:
        return int(value, 16)
    except ValueError:
        return 0


def to_flag(value: str) -> int:
    """tshark flag 필드를 0/1 feature로 변환한다."""
    return 0 if value in ("", "0", "False", "false") else 1


def univ1_pcap_index(path: Path) -> int | None:
    """univ1_pt1(.pcap) 형태의 파일명에서 trace 번호를 뽑는다."""
    if path.suffix not in ("", ".pcap"):
        return None

    name = path.stem if path.suffix == ".pcap" else path.name
    if not name.startswith("univ1_pt"):
        return None

    suffix = name.replace("univ1_pt", "", 1)
    if not suffix.isdigit():
        return None

    index = int(suffix)
    if 1 <= index <= 20:
        return index
    return None


def normalized_univ1_pcap_name(path: Path) -> str:
    """확장자가 없는 UNI1 pcap도 metadata에는 .pcap 이름으로 기록한다."""
    index = univ1_pcap_index(path)
    if index is None:
        return path.name
    return f"univ1_pt{index}.pcap"


def find_univ1_pcaps(pcap_dir: Path) -> list[Path]:
    """UNI1 trace 파일 중 univ1_pt1(.pcap)부터 univ1_pt20(.pcap)까지 찾는다."""
    files_by_index: dict[int, Path] = {}
    for path in pcap_dir.glob("univ1_pt*"):
        index = univ1_pcap_index(path)
        if index is None:
            continue

        previous = files_by_index.get(index)
        if previous is None or (previous.suffix == "" and path.suffix == ".pcap"):
            files_by_index[index] = path

    return [files_by_index[index] for index in sorted(files_by_index)]


def iter_tshark_rows(path: Path):
    """pcap을 tshark로 스트리밍 처리해 row dict를 하나씩 반환한다."""
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
            # 비어 있는 뒤쪽 필드는 split 결과에서 빠질 수 있으므로 빈 문자열로 채운다.
            values.extend([""] * (len(TSHARK_FIELDS) - len(values)))
        elif len(values) > len(TSHARK_FIELDS):
            # 예상보다 많은 값이 있으면 현재 feature pipeline에서 쓰는 필드만 남긴다.
            values = values[: len(TSHARK_FIELDS)]
        yield dict(zip(TSHARK_FIELDS, values))

    stderr = proc.stderr.read() if proc.stderr is not None else ""
    returncode = proc.wait()
    if returncode != 0:
        raise RuntimeError(f"tshark failed for {path}: {stderr}")


def add_packet_to_direction(
    direction: DirectionState,
    direction_name: str,
    row: dict[str, str],
    ts_us: int,
    packet_count: int,
    mask_analysis_features: bool = True,
) -> None:
    """payload가 있는 패킷 row 하나를 특정 방향 상태에 누적하고 feature로 저장한다."""
    tcp_len = to_int(row["tcp.len"])
    if tcp_len <= 0:
        return

    # iat_us는 해당 방향에서 직전 패킷과의 간격이다.
    iat_us = 0 if direction.prev_ts_us is None else ts_us - direction.prev_ts_us
    first_ts_us = ts_us if direction.first_ts_us is None else direction.first_ts_us
    # elapsed_us는 해당 방향의 첫 패킷 이후 경과 시간이다.
    elapsed_us = ts_us - first_ts_us
    direction.prev_ts_us = ts_us

    direction.packet_count += 1
    # payload_bytes는 라벨링과 flow 크기 계산의 기준으로 사용한다.
    direction.payload_bytes += tcp_len
    direction.first_ts_us = first_ts_us
    direction.last_ts_us = ts_us if direction.last_ts_us is None else max(direction.last_ts_us, ts_us)

    if len(direction.feature_packets) >= packet_count:
        # 학습 입력은 앞 packet_count개만 쓰지만, byte/packet 통계는 위에서 계속 누적한다.
        return

    feature_packet = [
        DIRECTION_TO_VALUE[direction_name],
        to_int(row["frame.len"]),
        to_int(row["ip.len"]),
        to_int(row["ip.hdr_len"]),
        to_int(row["ip.ttl"]),
        to_int(row["ip.dsfield.dscp"]),
        to_int(row["ip.dsfield.ecn"]),
        tcp_len,
        to_int(row["tcp.hdr_len"]),
        to_int(row["tcp.flags.syn"]),
        to_int(row["tcp.flags.ack"]),
        to_int(row["tcp.flags.push"]),
        to_int(row["tcp.flags.fin"]),
        to_int(row["tcp.flags.reset"]),
        to_int(row["tcp.window_size"]),
        iat_us,
        elapsed_us,
        direction.payload_bytes,
    ]
    direction.feature_packets.append(feature_packet)


def build_stream_states(
    pcap_files: list[Path],
    packet_count: int,
    mask_analysis_features: bool = True,
) -> dict[tuple[str, int], StreamState]:
    """여러 UNI1 pcap의 TCP stream을 읽어 양방향 상태로 묶는다."""
    streams: dict[tuple[str, int], StreamState] = {}

    for pcap_file in pcap_files:
        print(f"processing {pcap_file}")
        source_file = normalized_univ1_pcap_name(pcap_file)

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
                # stream의 첫 패킷 방향을 기준으로 forward 5-tuple을 고정한다.
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
                # 첫 패킷과 같은 5-tuple 방향은 src_to_dst로 본다.
                add_packet_to_direction(
                    stream.fwd,
                    "src_to_dst",
                    row,
                    ts_us,
                    packet_count,
                    mask_analysis_features=mask_analysis_features,
                )
            elif src_ip == stream.dst_ip and src_port == stream.dst_port and dst_ip == stream.src_ip and dst_port == stream.src_port:
                # 반대 5-tuple은 dst_to_src로 본다.
                add_packet_to_direction(
                    stream.rev,
                    "dst_to_src",
                    row,
                    ts_us,
                    packet_count,
                    mask_analysis_features=mask_analysis_features,
                )

    return streams


def percentile(sorted_values: list[int], q: float) -> int:
    """정렬된 값 목록에서 단순 분위수 값을 반환한다."""
    if not sorted_values:
        return 0
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    index = int((len(sorted_values) - 1) * q)
    return sorted_values[index]


def flow_size_bytes(stream: StreamState) -> int:
    """양방향 중 더 큰 payload byte를 flow 크기로 사용한다."""
    return max(stream.fwd.payload_bytes, stream.rev.payload_bytes)


def dominant_direction(stream: StreamState) -> tuple[str, DirectionState]:
    """payload가 더 큰 방향을 반환한다."""
    if stream.rev.payload_bytes > stream.fwd.payload_bytes:
        return "dst_to_src", stream.rev
    return "src_to_dst", stream.fwd


def iter_directional_states(stream: StreamState):
    """stream의 양방향 상태를 고정 순서로 순회한다."""
    yield "src_to_dst", stream.fwd
    yield "dst_to_src", stream.rev


def pad_feature_packets(feature_packets: list[list[int]], packet_count: int) -> tuple[list[list[int]], int]:
    """가변 길이 패킷 feature를 고정 길이로 padding한다."""
    seq_len = min(len(feature_packets), packet_count)
    padded = [list(row) for row in feature_packets[:packet_count]]

    if not padded:
        return padded, 0

    while len(padded) < packet_count:
        # 패킷이 부족한 flow는 마지막 관측 feature를 반복한다.
        padded.append(list(padded[-1]))

    return padded, seq_len


def write_stats_csv(streams: list[StreamState], output_path: Path) -> None:
    """stream별 기본 통계를 CSV로 저장한다."""
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
    raw_sequences: bool = False,
) -> int:
    """UNI1 stream 상태를 학습용 JSONL sample로 저장한다."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_path.open("w", encoding="utf-8") as f:
        for flow_id, stream in enumerate(streams, start=1):
            parent_size_bytes = flow_size_bytes(stream)
            if parent_size_bytes <= 0:
                # payload가 없는 stream은 학습 라벨을 만들 수 없으므로 제외한다.
                continue

            for direction_name, direction in iter_directional_states(stream):
                if not direction.feature_packets:
                    # 해당 방향에 관측된 feature packet이 없으면 sample을 만들지 않는다.
                    continue

                if raw_sequences:
                    x = [list(row) for row in direction.feature_packets[:packet_count]]
                    seq_len = None
                else:
                    x, seq_len = pad_feature_packets(direction.feature_packets, packet_count)

                sample = {
                    # UNI1은 TrafficGenerator의 src_index가 없으므로 src_index는 0으로 고정한다.
                    "flow_key": {
                        "src_index": 0,
                        "flow_id": flow_id,
                        "direction": direction_name,
                    },
                    "trace_key": {
                        # 원본 pcap/tcp.stream으로 다시 추적하기 위한 키다.
                        "source_file": stream.source_file,
                        "tcp_stream": stream.tcp_stream,
                    },
                    "feature_names": FEATURE_NAMES,
                    "x": x,
                    "directional_size_bytes": direction.payload_bytes,
                    "parent_flow_size_bytes": parent_size_bytes,
                    "flow_size_bytes": direction.payload_bytes,
                    "label": 1 if direction.payload_bytes >= threshold else 0,
                }
                if seq_len is not None:
                    sample["seq_len"] = seq_len
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
                count += 1
    return count


def write_cdf(sizes: list[int], output_path: Path) -> None:
    """UNI1 flow 크기 분포를 TrafficGenerator가 읽을 수 있는 CDF 형식으로 저장한다."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_sizes = sorted(sizes)
    rows: list[tuple[int, float]] = []

    for q in QUANTILES_FOR_CDF:
        value = percentile(sorted_sizes, q)
        if rows and rows[-1][0] == value:
            # 같은 byte 값이 연속되면 더 높은 분위수로 갱신해 CDF 행을 압축한다.
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
    """dataset sample을 만들 수 있는 방향이 하나라도 있는지 확인한다."""
    return any(
        direction.payload_bytes > 0 and len(direction.feature_packets) > 0
        for _, direction in iter_directional_states(stream)
    )


def write_summary(
    streams: list[StreamState],
    sizes: list[int],
    dataset_candidate_sizes: list[int],
    threshold: int,
    output_path: Path,
) -> None:
    """UNI1 변환 결과 요약 통계를 JSON으로 저장한다."""
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
    """UNI1 pcap trace에서 dataset, 통계 CSV, CDF, summary를 생성한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--pcap-dir", default="data/univ1_trace")
    parser.add_argument("--packet-count", type=int, default=8)
    parser.add_argument("--dataset-out", default="dataset_univ1.jsonl")
    parser.add_argument("--stats-out", default="analyze/univ1_flow_stats.csv")
    parser.add_argument("--summary-out", default="analyze/univ1_summary.json")
    parser.add_argument("--cdf-out", default="TrafficGenerator/conf/UNI1_CDF.txt")
    parser.add_argument(
        "--raw-sequences",
        action="store_true",
        help="write only observed packets in x and omit seq_len; padding can be applied later",
    )
    parser.add_argument(
        "--no-mask-analysis-features",
        action="store_true",
        help="deprecated; tcp.analysis.* fields are no longer emitted in model features",
    )
    args = parser.parse_args()

    pcap_files = find_univ1_pcaps(Path(args.pcap_dir))
    if not pcap_files:
        raise SystemExit(f"no univ1_pt*.pcap files found in {args.pcap_dir}")

    # pcap 전체를 TCP stream 단위로 먼저 모은 뒤, 아래에서 dataset/stat/CDF를 각각 쓴다.
    streams_by_key = build_stream_states(
        pcap_files,
        args.packet_count,
        mask_analysis_features=not args.no_mask_analysis_features,
    )
    streams = list(streams_by_key.values())
    sizes = [flow_size_bytes(stream) for stream in streams if flow_size_bytes(stream) > 0]
    dataset_candidate_sizes = [
        # 실제 JSONL sample이 만들어질 수 있는 방향별 payload 크기만 라벨 기준에 사용한다.
        direction.payload_bytes
        for stream in streams
        for _, direction in iter_directional_states(stream)
        if direction.payload_bytes > 0 and len(direction.feature_packets) > 0
    ]
    threshold = percentile(sorted(dataset_candidate_sizes), 0.80)

    write_stats_csv(streams, Path(args.stats_out))
    dataset_count = write_dataset_jsonl(
        streams,
        threshold,
        args.packet_count,
        Path(args.dataset_out),
        raw_sequences=args.raw_sequences,
    )
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
