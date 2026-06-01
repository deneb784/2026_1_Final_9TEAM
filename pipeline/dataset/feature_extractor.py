from pipeline.models import FlowEntry


def build_packet_features(entry: FlowEntry) -> list[dict]:
    packets = entry.packets
    result: list[dict] = []

    prev_ts_us = None

    for idx, pkt in enumerate(packets):
        if prev_ts_us is None:
            iat_us = 0
        else:
            iat_us = pkt.ts_us - prev_ts_us

        result.append({
            "packet_index": idx,
            "frame_len": pkt.frame_len,
            "ip_len": pkt.ip_len,
            "ip_ttl": pkt.ip_ttl,
            "tcp_payload_bytes": pkt.tcp_len,
            "tcp_flags": pkt.tcp_flags,
            "tcp_window_size": pkt.tcp_window_size,
            "iat_us": iat_us,
            "retransmission": int(pkt.retransmission),
            "out_of_order": int(pkt.out_of_order),
            "duplicate_ack": int(pkt.duplicate_ack),
            "fast_retransmission": int(pkt.fast_retransmission),
        })

        prev_ts_us = pkt.ts_us

    return result
