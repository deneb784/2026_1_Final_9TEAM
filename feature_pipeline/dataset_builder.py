import json
from glob import glob
from pathlib import Path

from feature_pipeline.meta_loader import load_all_request_meta, build_meta_index
from feature_pipeline.packet_loader import iter_packets_from_pcap
from feature_pipeline.matcher import match_packet
from feature_pipeline.flow_cache import FlowCache
from feature_pipeline.models import FlowEntry


def find_pcap_files(pcap_dir: str | Path) -> list[str]:
    pattern = str(Path(pcap_dir) / "*.pcap")
    return sorted(glob(pattern))


def packet_to_vector(pkt, prev_ts_us: int | None) -> tuple[list, int]:
    iat_us = 0 if prev_ts_us is None else pkt.ts_us - prev_ts_us

    tcp_flags = pkt.tcp_flags
    if isinstance(tcp_flags, str):
        try:
            tcp_flags = int(tcp_flags, 16)
        except ValueError:
            tcp_flags = 0

    vector = [
        pkt.frame_len,
        pkt.ip_len,
        pkt.ip_ttl,
        pkt.tcp_len,
        tcp_flags,
        pkt.tcp_window_size,
        iat_us,
        int(pkt.retransmission),
        int(pkt.out_of_order),
        int(pkt.duplicate_ack),
        int(pkt.fast_retransmission),
    ]
    return vector, pkt.ts_us


def build_x_from_entry(entry: FlowEntry, packet_count: int) -> list[list]:
    packets = entry.packets[:packet_count]
    x: list[list] = []
    prev_ts_us = None

    for pkt in packets:
        vector, prev_ts_us = packet_to_vector(pkt, prev_ts_us)
        x.append(vector)

    return x


def compute_directional_size_bytes(entry: FlowEntry) -> int:
    return sum(pkt.tcp_len for pkt in entry.packets)


def make_label(size_bytes: int, threshold: int) -> int:
    return 1 if size_bytes >= threshold else 0


def build_dataset_sample(
    entry: FlowEntry,
    packet_count: int,
    label_threshold: int,
) -> dict:
    directional_size_bytes = compute_directional_size_bytes(entry)
    x = build_x_from_entry(entry, packet_count)

    return {
        "flow_key": {
            "src_index": entry.src_index,
            "flow_id": entry.flow_id,
            "direction": entry.direction,
        },
        "x": x,
        "directional_size_bytes": directional_size_bytes,
        "label": make_label(directional_size_bytes, threshold=label_threshold),
    }


def run_dataset_builder(
    results_dir: str = "results",
    pcap_dir: str = "captured_packet",
    packet_count: int = 8,
    label_threshold: int = 2000000,
    direction_filter: str | None = None,
) -> list[dict]:
    all_metas = load_all_request_meta(results_dir)
    meta_index = build_meta_index(all_metas)

    # 학습용 builder는 전체 방향 flow를 끝까지 모아야 하므로 큰 값으로 둔다
    flow_cache = FlowCache(feature_packet_count=10**9)

    pcap_files = find_pcap_files(pcap_dir)

    for pcap_file in pcap_files:
        for packet in iter_packets_from_pcap(pcap_file):
            matched = match_packet(packet, meta_index)
            if matched is None:
                continue

            meta, direction = matched

            if direction_filter is not None and direction != direction_filter:
                continue

            flow_cache.add_packet(meta, direction, packet)

    samples: list[dict] = []

    for entry in flow_cache.entries.values():
        if len(entry.packets) < packet_count:
            continue

        sample = build_dataset_sample(
            entry,
            packet_count=packet_count,
            label_threshold=label_threshold,
        )
        samples.append(sample)

    return samples


def save_dataset_jsonl(samples: list[dict], output_path: str | Path) -> None:
    output_path = Path(output_path)

    with open(output_path, "w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    samples = run_dataset_builder(
        results_dir="results",
        pcap_dir="captured_packet",
        packet_count=8,
        label_threshold=2000000,
        direction_filter=None,  # src_to_dst, dst_to_src 모두 포함
    )

    save_dataset_jsonl(samples, "dataset.jsonl")
    print(f"saved {len(samples)} samples to dataset.jsonl")

    for sample in samples[:3]:
        print(json.dumps(sample, ensure_ascii=False))
