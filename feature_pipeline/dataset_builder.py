import json
from glob import glob
from pathlib import Path

from feature_pipeline.meta_loader import load_all_request_meta, build_meta_index
from feature_pipeline.packet_loader import iter_packets_from_pcap
from feature_pipeline.matcher import match_packet
from feature_pipeline.flow_cache import FlowCache
from feature_pipeline.models import FlowEntry


MASKED_FEATURE_INDICES = {
    7,   # retransmission
    8,   # out_of_order
    9,   # duplicate_ack
    10,  # fast_retransmission
}


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
    for index in MASKED_FEATURE_INDICES:
        vector[index] = 0
    return vector, pkt.ts_us


def pad_feature_packets(x: list[list], packet_count: int) -> tuple[list[list], int]:
    seq_len = min(len(x), packet_count)
    padded = [list(row) for row in x[:packet_count]]

    if not padded:
        return padded, 0

    while len(padded) < packet_count:
        padded.append(list(padded[-1]))

    return padded, seq_len


def build_x_from_entry(entry: FlowEntry, packet_count: int) -> tuple[list[list], int]:
    packets = entry.packets[:packet_count]
    x: list[list] = []
    prev_ts_us = None

    for pkt in packets:
        vector, prev_ts_us = packet_to_vector(pkt, prev_ts_us)
        x.append(vector)

    return pad_feature_packets(x, packet_count)


def compute_directional_size_bytes(entry: FlowEntry) -> int:
    return sum(pkt.tcp_len for pkt in entry.packets)


def make_label(size_bytes: int, threshold: int) -> int:
    return 1 if size_bytes >= threshold else 0


def percentile(sorted_values: list[int], q: float) -> int:
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
) -> dict:
    directional_size_bytes = compute_directional_size_bytes(entry)
    x, seq_len = build_x_from_entry(entry, packet_count)

    return {
        "flow_key": {
            "src_index": entry.src_index,
            "flow_id": entry.flow_id,
            "direction": entry.direction,
        },
        "x": x,
        "seq_len": seq_len,
        "directional_size_bytes": directional_size_bytes,
        "parent_flow_size_bytes": parent_flow_size_bytes,
        "flow_size_bytes": directional_size_bytes,
        "label": make_label(directional_size_bytes, threshold=label_threshold),
    }


def run_dataset_builder(
    results_dir: str = "results",
    pcap_dir: str = "captured_packet",
    packet_count: int = 10,
    label_threshold: int | None = None,
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

    eligible_entries = [
        entry
        for entry in flow_cache.entries.values()
        if len(entry.packets) > 0
    ]

    parent_sizes: dict[tuple[int, int], int] = {}
    for entry in eligible_entries:
        key = (entry.src_index, entry.flow_id)
        directional_size = compute_directional_size_bytes(entry)
        parent_sizes[key] = max(parent_sizes.get(key, 0), directional_size)

    directional_sizes = [
        compute_directional_size_bytes(entry)
        for entry in eligible_entries
    ]

    if label_threshold is None:
        label_threshold = percentile(sorted(directional_sizes), 0.80)

    samples: list[dict] = []

    for entry in eligible_entries:
        parent_key = (entry.src_index, entry.flow_id)
        sample = build_dataset_sample(
            entry,
            packet_count=packet_count,
            label_threshold=label_threshold,
            parent_flow_size_bytes=parent_sizes[parent_key],
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
        packet_count=10,
        label_threshold=None,
        direction_filter=None,  # src_to_dst, dst_to_src 모두 포함
    )

    save_dataset_jsonl(samples, "dataset.jsonl")
    print(f"saved {len(samples)} samples to dataset.jsonl")

    for sample in samples[:3]:
        print(json.dumps(sample, ensure_ascii=False))
