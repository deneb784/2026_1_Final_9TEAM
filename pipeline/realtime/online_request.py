import time

from pipeline.dataset.dataset_builder import FEATURE_NAMES, build_x_from_entry
from pipeline.models import FlowEntry


def build_online_flow_request(
    entry: FlowEntry,
    packet_count: int,
    run_id: str | None = None,
    capture_mode: str | None = None,
    feature_ready_wall_ns: int | None = None,
) -> dict:
    """온라인 추론 worker로 보낼 flow feature 요청 payload를 만든다."""
    x, seq_len = build_x_from_entry(entry, packet_count=packet_count)
    feature_packets = [pkt for pkt in entry.packets if pkt.tcp_len > 0][:packet_count]
    first_packet_ts_us = None
    last_packet_ts_us = None
    if feature_packets:
        first_packet_ts_us = getattr(feature_packets[0], "epoch_ts_us", feature_packets[0].ts_us)
        last_packet_ts_us = getattr(feature_packets[-1], "epoch_ts_us", feature_packets[-1].ts_us)

    request = {
        "request_key": {
            "src_index": entry.src_index,
            "flow_id": entry.flow_id,
            "direction": entry.direction,
        },
        "logical_flow_id": entry.logical_flow_id,
        "feature_names": FEATURE_NAMES,
        "x": x,
        "seq_len": seq_len,
        "max_packet_count": packet_count,
        "observed_directional_payload_bytes": entry.payload_bytes,
        "producer_metrics": {
            "feature_ready_wall_ns": feature_ready_wall_ns or time.time_ns(),
            "capture_mode": capture_mode,
            "first_packet_ts_us": first_packet_ts_us,
            "last_packet_ts_us": last_packet_ts_us,
            "feature_packet_count_observed": len(feature_packets),
        },
    }
    if run_id is not None:
        request["run_id"] = run_id
    return request
