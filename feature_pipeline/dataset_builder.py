import argparse
import json
import sys
from glob import glob
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from feature_pipeline.meta_loader import load_all_request_meta, build_meta_index
from feature_pipeline.packet_loader import iter_packets_from_pcap
from feature_pipeline.matcher import match_packet
from feature_pipeline.flow_cache import FlowCache
from feature_pipeline.models import FlowEntry


FEATURE_NAMES = [
    # 모델 입력 x의 각 열 이름이다. packet_to_vector()의 vector 순서와 반드시 같아야 한다.
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


def host_ip_to_edge_pcap_name(host_ip: str) -> str | None:
    """Mininet fat-tree host IP(10.pod.edge.host)를 host-facing edge pcap 이름으로 바꾼다."""
    parts = host_ip.split(".")
    if len(parts) != 4 or parts[0] != "10":
        return None
    try:
        pod = int(parts[1])
        edge = int(parts[2])
        host = int(parts[3])
    except ValueError:
        return None
    return f"edge_p{pod}_e{edge}-eth{host}.pcap"


def find_pcap_files(pcap_dir: str | Path, host_ips: set[str] | None = None) -> list[str]:
    """pcap 디렉터리에서 학습 데이터 생성에 사용할 캡처 파일 목록을 찾는다."""
    pcap_dir = Path(pcap_dir)
    if host_ips:
        inferred_files = []
        for host_ip in sorted(host_ips):
            pcap_name = host_ip_to_edge_pcap_name(host_ip)
            if pcap_name is None:
                continue
            pcap_path = pcap_dir / pcap_name
            if pcap_path.exists():
                inferred_files.append(str(pcap_path))
        if inferred_files:
            return sorted(set(inferred_files), key=lambda path: (Path(path).stat().st_size, path))

    pattern = str(Path(pcap_dir) / "*.pcap")
    return sorted(glob(pattern), key=lambda path: (Path(path).stat().st_size, path))


def packet_fingerprint(pkt) -> tuple:
    """여러 switch port에서 중복 캡처된 같은 TCP 패킷을 구분하기 위한 key."""
    return (
        pkt.ts_us,
        pkt.src_ip,
        pkt.src_port,
        pkt.dst_ip,
        pkt.dst_port,
        pkt.tcp_seq,
        pkt.tcp_ack,
        pkt.tcp_len,
        pkt.tcp_flags,
    )


def packet_to_vector(
    pkt,
    direction: str,
    prev_ts_us: int | None,
    first_ts_us: int | None,
    cum_payload_bytes: int,
) -> tuple[list, int, int, int]:
    """패킷 1개를 모델 입력 feature vector 1행으로 변환한다."""
    # iat_us는 같은 flow 안에서 직전 패킷과의 시간 간격이다.
    iat_us = 0 if prev_ts_us is None else pkt.ts_us - prev_ts_us
    first_ts_us = pkt.ts_us if first_ts_us is None else first_ts_us
    # elapsed_us는 flow의 첫 패킷부터 현재 패킷까지 지난 시간이다.
    elapsed_us = pkt.ts_us - first_ts_us
    # 누적 payload byte는 초반 패킷만 보더라도 지금까지 전송량을 표현하기 위한 feature다.
    cum_payload_bytes += pkt.tcp_len

    vector = [
        DIRECTION_TO_VALUE[direction],
        pkt.frame_len,
        pkt.ip_len,
        pkt.ip_hdr_len,
        pkt.ip_ttl,
        pkt.ip_dscp,
        pkt.ip_ecn,
        pkt.tcp_len,
        pkt.tcp_hdr_len,
        pkt.tcp_syn,
        pkt.tcp_ack_flag,
        pkt.tcp_psh,
        pkt.tcp_fin,
        pkt.tcp_rst,
        pkt.tcp_window_size,
        iat_us,
        elapsed_us,
        cum_payload_bytes,
    ]
    return vector, pkt.ts_us, first_ts_us, cum_payload_bytes


def pad_feature_packets(x: list[list], packet_count: int) -> tuple[list[list], int]:
    """가변 길이 패킷 시퀀스를 고정 길이로 맞춘다."""
    seq_len = min(len(x), packet_count)
    padded = [list(row) for row in x[:packet_count]]

    if not padded:
        return padded, 0

    while len(padded) < packet_count:
        # 패킷이 부족하면 마지막 관측 패킷을 반복해서 padding한다.
        padded.append(list(padded[-1]))

    return padded, seq_len


def build_x_from_entry(
    entry: FlowEntry,
    packet_count: int,
    raw_sequences: bool = False,
) -> tuple[list[list], int | None]:
    """단방향 FlowEntry에서 모델 입력 x를 만든다."""
    packets = entry.packets[:packet_count]
    x: list[list] = []
    prev_ts_us = None
    first_ts_us = None
    cum_payload_bytes = 0

    for pkt in packets:
        vector, prev_ts_us, first_ts_us, cum_payload_bytes = packet_to_vector(
            pkt,
            entry.direction,
            prev_ts_us,
            first_ts_us,
            cum_payload_bytes,
        )
        x.append(vector)

    if raw_sequences:
        # raw_sequences 모드에서는 padding을 나중 단계에 맡기고 관측된 패킷만 저장한다.
        return [list(row) for row in x], None

    return pad_feature_packets(x, packet_count)


def build_x_from_directional_packets(
    directional_packets: list[tuple[str, object]],
    packet_count: int,
    raw_sequences: bool = False,
) -> tuple[list[list], int | None]:
    """양방향 패킷을 시간순으로 섞어 request 단위 입력 x를 만든다."""
    packets = sorted(directional_packets, key=lambda item: item[1].ts_us)[:packet_count]
    x: list[list] = []
    prev_ts_us = None
    first_ts_us = None
    cum_payload_bytes = 0

    for direction, pkt in packets:
        vector, prev_ts_us, first_ts_us, cum_payload_bytes = packet_to_vector(
            pkt,
            direction,
            prev_ts_us,
            first_ts_us,
            cum_payload_bytes,
        )
        x.append(vector)

    if raw_sequences:
        return [list(row) for row in x], None

    return pad_feature_packets(x, packet_count)


def compute_directional_size_bytes(entry: FlowEntry) -> int:
    """해당 방향에서 실제 캡처된 TCP payload byte 합을 계산한다."""
    return entry.payload_bytes


def make_label(size_bytes: int, threshold: int) -> int:
    """threshold 이상이면 elephant flow(1), 아니면 mice flow(0)로 라벨링한다."""
    return 1 if size_bytes >= threshold else 0


def percentile(sorted_values: list[int], q: float) -> int:
    """정렬된 값 목록에서 단순 분위수 값을 반환한다."""
    if not sorted_values:
        return 0
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    return sorted_values[int((len(sorted_values) - 1) * q)]


def build_dataset_sample(
    entry: FlowEntry,
    packet_count: int,
    label_threshold: int,
    parent_flow_size_bytes: int,
    raw_sequences: bool = False,
) -> dict:
    """단방향 flow 하나를 JSONL sample 하나로 변환한다."""
    directional_size_bytes = compute_directional_size_bytes(entry)
    x, seq_len = build_x_from_entry(entry, packet_count, raw_sequences=raw_sequences)

    sample = {
        # flow_key는 나중에 원본 flow를 추적하기 위한 식별자다.
        "flow_key": {
            "src_index": entry.src_index,
            "flow_id": entry.flow_id,
            "direction": entry.direction,
        },
        "feature_names": FEATURE_NAMES,
        "x": x,
        "directional_size_bytes": directional_size_bytes,
        "parent_flow_size_bytes": parent_flow_size_bytes,
        "flow_size_bytes": directional_size_bytes,
        "label": make_label(directional_size_bytes, threshold=label_threshold),
    }
    if seq_len is not None:
        sample["seq_len"] = seq_len
    return sample


def build_request_sample(
    src_index: int,
    flow_id: int,
    entries: list[FlowEntry],
    packet_count: int,
    label_threshold: int,
    flow_size_bytes: int,
    raw_sequences: bool = False,
) -> dict | None:
    """src_to_dst와 dst_to_src를 합쳐 request 단위 sample 하나를 만든다."""
    directional_packets = [
        (entry.direction, pkt)
        for entry in entries
        for pkt in entry.packets
    ]
    if not directional_packets:
        return None

    x, seq_len = build_x_from_directional_packets(
        directional_packets,
        packet_count=packet_count,
        raw_sequences=raw_sequences,
    )
    directional_sizes = {
        # 방향별 byte 수를 같이 저장해 request 라벨과 단방향 크기를 비교할 수 있게 한다.
        entry.direction: compute_directional_size_bytes(entry)
        for entry in entries
    }
    sample = {
        "flow_key": {
            "src_index": src_index,
            "flow_id": flow_id,
            "direction": "bidirectional",
        },
        "feature_names": FEATURE_NAMES,
        "x": x,
        "directional_size_bytes": max(directional_sizes.values(), default=0),
        "directional_size_bytes_by_direction": directional_sizes,
        "parent_flow_size_bytes": flow_size_bytes,
        "flow_size_bytes": flow_size_bytes,
        "label": make_label(flow_size_bytes, threshold=label_threshold),
    }
    if seq_len is not None:
        sample["seq_len"] = seq_len
    return sample


def run_dataset_builder(
    results_dir: str = "results",
    pcap_dir: str = "captured_packet",
    packet_count: int = 10,
    label_threshold: int | None = None,
    direction_filter: str | None = None,
    raw_sequences: bool = False,
    sample_mode: str = "direction",
) -> list[dict]:
    """Mininet run 디렉터리의 results와 pcap을 합쳐 학습용 sample 목록을 만든다."""
    # results의 meta 정보는 packet이 어떤 logical flow에 속하는지 매칭하는 기준이다.
    all_metas = load_all_request_meta(results_dir)
    meta_index = build_meta_index(all_metas)
    meta_sizes = {
        (meta.src_index, meta.flow_id): meta.size_bytes
        for meta in all_metas
    }

    # feature에는 앞 packet_count개만 필요하다. 전체 payload 크기는 FlowEntry.payload_bytes에
    # 숫자로 누적해 큰 pcap에서도 패킷 객체를 전부 메모리에 보관하지 않는다.
    flow_cache = FlowCache(feature_packet_count=packet_count)

    if direction_filter == "src_to_dst":
        capture_host_ips = {meta.src_ip for meta in all_metas}
    elif direction_filter == "dst_to_src":
        capture_host_ips = {meta.dst_ip for meta in all_metas}
    else:
        # PacketCapturer records host-facing pcaps with a "src host <host_ip>" filter.
        # Reading only request src hosts misses server-originated dst_to_src packets.
        capture_host_ips = {
            host_ip
            for meta in all_metas
            for host_ip in (meta.src_ip, meta.dst_ip)
        }
    pcap_files = find_pcap_files(pcap_dir, host_ips=capture_host_ips)
    request_packet_counts: dict[tuple[int, int], int] = {}
    request_seen_packets: dict[tuple[int, int], set[tuple]] = {}
    complete_requests: set[tuple[int, int]] = set()

    for pcap_file in pcap_files:
        for packet in iter_packets_from_pcap(pcap_file):
            # pcap 패킷을 TrafficGenerator meta와 대조해 flow_id와 방향을 찾는다.
            matched = match_packet(packet, meta_index)
            if matched is None:
                continue

            meta, direction = matched
            parent_key = (meta.src_index, meta.flow_id)

            if direction_filter is not None and direction != direction_filter:
                continue

            if sample_mode == "request":
                if parent_key in complete_requests:
                    continue

                fingerprint = packet_fingerprint(packet)
                seen_packets = request_seen_packets.setdefault(parent_key, set())
                if fingerprint in seen_packets:
                    continue
                seen_packets.add(fingerprint)

            flow_cache.add_packet(meta, direction, packet)

            if sample_mode == "request":
                request_packet_counts[parent_key] = request_packet_counts.get(parent_key, 0) + 1
                if request_packet_counts[parent_key] >= packet_count:
                    complete_requests.add(parent_key)
                    request_seen_packets.pop(parent_key, None)
                    if len(complete_requests) >= len(meta_sizes):
                        break
        if sample_mode == "request" and len(complete_requests) >= len(meta_sizes):
            break

    eligible_entries = [
        # 패킷이 하나 이상 매칭된 flow만 dataset 후보로 사용한다.
        entry
        for entry in flow_cache.entries.values()
        if len(entry.packets) > 0
    ]

    entries_by_parent: dict[tuple[int, int], list[FlowEntry]] = {}
    parent_sizes: dict[tuple[int, int], int] = {}
    for entry in eligible_entries:
        # parent key는 같은 요청의 양방향 flow를 묶는 기준이다.
        key = (entry.src_index, entry.flow_id)
        entries_by_parent.setdefault(key, []).append(entry)
        directional_size = compute_directional_size_bytes(entry)
        parent_sizes[key] = max(parent_sizes.get(key, 0), directional_size)

    if sample_mode == "request":
        label_sizes = [
            meta_sizes.get(parent_key, parent_sizes[parent_key])
            for parent_key in entries_by_parent
        ]
    elif sample_mode == "direction":
        label_sizes = [
            compute_directional_size_bytes(entry)
            for entry in eligible_entries
        ]
    else:
        raise ValueError(f"unsupported sample_mode: {sample_mode}")

    if label_threshold is None:
        # threshold를 명시하지 않으면 현재 dataset의 p80을 elephant 기준으로 사용한다.
        label_threshold = percentile(sorted(label_sizes), 0.80)

    samples: list[dict] = []

    if sample_mode == "request":
        # request 모드는 양방향 패킷을 하나의 sample로 합친다.
        for parent_key, entries in entries_by_parent.items():
            flow_size_bytes = meta_sizes.get(parent_key, parent_sizes[parent_key])
            sample = build_request_sample(
                src_index=parent_key[0],
                flow_id=parent_key[1],
                entries=entries,
                packet_count=packet_count,
                label_threshold=label_threshold,
                flow_size_bytes=flow_size_bytes,
                raw_sequences=raw_sequences,
            )
            if sample is not None:
                samples.append(sample)
        return samples

    for entry in eligible_entries:
        # direction 모드는 src_to_dst, dst_to_src를 각각 별도 sample로 저장한다.
        parent_key = (entry.src_index, entry.flow_id)
        sample = build_dataset_sample(
            entry,
            packet_count=packet_count,
            label_threshold=label_threshold,
            parent_flow_size_bytes=parent_sizes[parent_key],
            raw_sequences=raw_sequences,
        )
        samples.append(sample)

    return samples


def save_dataset_jsonl(samples: list[dict], output_path: str | Path) -> None:
    """sample 목록을 한 줄에 JSON 하나씩 저장한다."""
    output_path = Path(output_path)

    with open(output_path, "w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")


def main() -> None:
    """CLI 인자를 받아 단일 Mininet run에서 dataset JSONL을 생성한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--pcap-dir", default="captured_packet")
    parser.add_argument("--packet-count", type=int, default=10)
    parser.add_argument("--label-threshold", type=int, default=None)
    parser.add_argument("--direction-filter", choices=["src_to_dst", "dst_to_src"], default=None)
    parser.add_argument("--sample-mode", choices=["direction", "request"], default="direction")
    parser.add_argument("--output", default="dataset.jsonl")
    parser.add_argument(
        "--raw-sequences",
        action="store_true",
        help="write only observed packets in x and omit seq_len; padding can be applied later",
    )
    args = parser.parse_args()

    samples = run_dataset_builder(
        results_dir=args.results_dir,
        pcap_dir=args.pcap_dir,
        packet_count=args.packet_count,
        label_threshold=args.label_threshold,
        direction_filter=args.direction_filter,
        raw_sequences=args.raw_sequences,
        sample_mode=args.sample_mode,
    )

    save_dataset_jsonl(samples, args.output)
    print(f"saved {len(samples)} samples to {args.output}")

    for sample in samples[:3]:
        print(json.dumps(sample, ensure_ascii=False))


if __name__ == "__main__":
    main()
