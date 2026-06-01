import json
from glob import glob
from pathlib import Path

from pipeline.dataset.meta_loader import load_all_request_meta, build_meta_index
from pipeline.dataset.packet_loader import iter_packets_from_pcap
from pipeline.dataset.matcher import match_packet
from pipeline.dataset.flow_cache import FlowCache
from pipeline.dataset.feature_extractor import build_packet_features


def find_pcap_files(pcap_dir: str | Path) -> list[str]:
    pattern = str(Path(pcap_dir) / "*.pcap")
    return sorted(glob(pattern))


def build_flow_request(meta, entry, packets: list[dict]) -> dict:
    return {
        "request_key": {
            "src_index": meta.src_index,
            "flow_id": meta.flow_id,
        },
        "direction": entry.direction,
        "logical_flow_id": entry.logical_flow_id,
        "meta": {
            "src_ip": meta.src_ip,
            "src_port": meta.src_port,
            "dst_ip": meta.dst_ip,
            "dst_port": meta.dst_port,
            "start_time_us": meta.start_time_us,
            "stop_time_us": meta.stop_time_us,
            "fct_us": meta.fct_us,
            "size_bytes": meta.size_bytes,
            "dscp": meta.dscp,
            "rate_mbps": meta.rate_mbps,
        },
        "packets": packets,
    }


def serialize_flow_request(flow_request: dict) -> str:
    return json.dumps(flow_request, ensure_ascii=False)


def run_pipeline(
    results_dir: str = "results",
    pcap_dir: str = "captured_packet",
    feature_packet_count: int = 10,
) -> tuple[list[dict], list[str]]:
    all_metas = load_all_request_meta(results_dir)
    meta_index = build_meta_index(all_metas)
    flow_cache = FlowCache(feature_packet_count=feature_packet_count)

    flow_requests: list[dict] = []
    flow_request_jsons: list[str] = []

    pcap_files = find_pcap_files(pcap_dir)

    for pcap_file in pcap_files:
        for packet in iter_packets_from_pcap(pcap_file):
            matched = match_packet(packet, meta_index)
            if matched is None:
                continue

            meta, direction = matched
            entry = flow_cache.add_packet(meta, direction, packet)

            if flow_cache.is_ready(entry):
                packet_features = build_packet_features(entry)
                flow_request = build_flow_request(meta, entry, packet_features)
                flow_request_json = serialize_flow_request(flow_request)

                flow_requests.append(flow_request)
                flow_request_jsons.append(flow_request_json)

                flow_cache.mark_feature_sent(entry)

    return flow_requests, flow_request_jsons


if __name__ == "__main__":
    flow_requests, flow_request_jsons = run_pipeline(
        results_dir="results",
        pcap_dir="captured_packet",
        feature_packet_count=10,
    )

    print(f"generated {len(flow_requests)} flow requests")

    for req_json in flow_request_jsons[:3]:
        print(req_json)
