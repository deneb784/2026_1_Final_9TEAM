from feature_pipeline.models import FlowEntry


def _safe_mean(values) -> float:
    if not values:
        return 0.0
    return float(sum(values)) / len(values)


def compute_features(entry: FlowEntry) -> dict:
    packets = entry.packets

    if not packets:
        return {
            "packet_count": 0,
            "total_frame_bytes": 0,
            "total_tcp_payload_bytes": 0,
            "mean_frame_len": 0.0,
            "min_frame_len": 0,
            "max_frame_len": 0,
            "flow_duration_us": 0,
            "mean_iat_us": 0.0,
            "min_iat_us": 0,
            "max_iat_us": 0,
            "retransmission_count": 0,
            "duplicate_ack_count": 0,
            "out_of_order_count": 0,
            "fast_retransmission_count": 0,
        }

    frame_lengths = [pkt.frame_len for pkt in packets]
    tcp_lengths = [pkt.tcp_len for pkt in packets]
    timestamps = [pkt.ts_us for pkt in packets]

    iats = []
    for i in range(1, len(timestamps)):
        iats.append(timestamps[i] - timestamps[i - 1])

    return {
        "packet_count": len(packets),
        "total_frame_bytes": sum(frame_lengths),
        "total_tcp_payload_bytes": sum(tcp_lengths),
        "mean_frame_len": _safe_mean(frame_lengths),
        "min_frame_len": min(frame_lengths),
        "max_frame_len": max(frame_lengths),
        "flow_duration_us": timestamps[-1] - timestamps[0],
        "mean_iat_us": _safe_mean(iats),
        "min_iat_us": min(iats) if iats else 0,
        "max_iat_us": max(iats) if iats else 0,
        "retransmission_count": sum(1 for pkt in packets if pkt.retransmission),
        "duplicate_ack_count": sum(1 for pkt in packets if pkt.duplicate_ack),
        "out_of_order_count": sum(1 for pkt in packets if pkt.out_of_order),
        "fast_retransmission_count": sum(1 for pkt in packets if pkt.fast_retransmission),
    }
