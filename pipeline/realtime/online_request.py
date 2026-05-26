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
    """온라인 추론 worker로 보낼 flow feature 요청 payload를 만든다.

    이 함수의 반환값은 이후 Redis Stream의 entry payload로 들어간다.
    Redis Stream은 "나중에 소비자가 읽어 갈 수 있는 append-only 로그/큐"에 가깝기 때문에,
    worker가 재시도하거나 지연해서 읽더라도 필요한 식별자와 feature를 모두 담아 보내야 한다.
    """
    # FlowEntry에 쌓인 패킷을 학습/추론 때 쓰는 feature 행렬 형태로 변환한다.
    # build_x_from_entry는 tcp_len > 0인 payload 패킷만 feature로 사용하고,
    # packet_count보다 적으면 padding을 채워 모델 입력 shape를 고정한다.
    x, seq_len = build_x_from_entry(entry, packet_count=packet_count)

    # latency 분석에는 "feature로 실제 사용된 첫/마지막 payload 패킷"의 시간이 필요하다.
    # ACK-only 패킷은 모델 입력에 들어가지 않으므로 여기서도 제외한다.
    feature_packets = [pkt for pkt in entry.packets if pkt.tcp_len > 0][:packet_count]
    first_packet_ts_us = None
    last_packet_ts_us = None
    if feature_packets:
        # XDP 온라인 경로에서는 epoch_ts_us가 들어올 수 있고, pcap/offline 경로에서는
        # 상대 timestamp인 ts_us만 있을 수 있다. 가능한 경우 epoch 기준 시간을 우선 사용한다.
        first_packet_ts_us = getattr(feature_packets[0], "epoch_ts_us", feature_packets[0].ts_us)
        last_packet_ts_us = getattr(feature_packets[-1], "epoch_ts_us", feature_packets[-1].ts_us)

    # request_key는 결과가 돌아왔을 때 어떤 flow의 응답인지 다시 찾기 위한 최소 식별자다.
    # logical_flow_id는 사람이 로그/CSV를 볼 때 읽기 쉬운 문자열 ID이고,
    # x/seq_len/feature_names는 모델 worker가 바로 추론할 수 있는 feature 본문이다.
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
        # producer_metrics는 추론 내용이 아니라 관측/전송 지연을 재기 위한 메타데이터다.
        # Redis Stream에 올릴 때 transport.py가 일부 값을 별도 field로 복사해서
        # 스트림 조회만으로도 latency 분석을 쉽게 할 수 있게 한다.
        "producer_metrics": {
            "feature_ready_wall_ns": feature_ready_wall_ns or time.time_ns(),
            "capture_mode": capture_mode,
            "first_packet_ts_us": first_packet_ts_us,
            "last_packet_ts_us": last_packet_ts_us,
            "feature_packet_count_observed": len(feature_packets),
        },
    }
    if run_id is not None:
        # run_id는 여러 실험이 같은 Redis를 공유할 때 결과와 지연 로그를 실험 단위로 묶기 위한 값이다.
        request["run_id"] = run_id
    return request
