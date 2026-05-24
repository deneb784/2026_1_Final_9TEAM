from feature_pipeline.dataset_builder import FEATURE_NAMES, build_x_from_entry
from feature_pipeline.models import FlowEntry


def build_online_flow_request(
    entry: FlowEntry,
    packet_count: int,
    run_id: str | None = None,
) -> dict:
    """온라인 추론 worker로 보낼 flow feature 요청 payload를 만든다."""
    x, seq_len = build_x_from_entry(entry, packet_count=packet_count)

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
    }
    if run_id is not None:
        request["run_id"] = run_id
    return request
