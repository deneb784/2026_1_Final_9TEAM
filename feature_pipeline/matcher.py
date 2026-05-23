from feature_pipeline.models import PacketRecord, RequestMeta


def find_time_candidates(
    candidates: list[RequestMeta],
    ts_us: int,
) -> list[RequestMeta]:
    """start_time_us 기준 정렬된 후보에서 ts_us를 포함할 수 있는 구간만 찾는다."""
    left = 0
    right = len(candidates)
    while left < right:
        mid = (left + right) // 2
        if candidates[mid].start_time_us <= ts_us:
            left = mid + 1
        else:
            right = mid

    matched: list[RequestMeta] = []
    index = left - 1
    while index >= 0:
        meta = candidates[index]
        if meta.stop_time_us < ts_us:
            break
        matched.append(meta)
        index -= 1
    return matched


def get_packet_direction(pkt: PacketRecord, meta: RequestMeta) -> str | None:
    # 정방향: 메타의 src -> dst 와 패킷 방향이 같음
    if (
        pkt.src_ip == meta.src_ip
        and pkt.src_port == meta.src_port
        and pkt.dst_ip == meta.dst_ip
        and pkt.dst_port == meta.dst_port
    ):
        return "src_to_dst"

    # 역방향: 메타의 dst -> src 와 패킷 방향이 같음
    if (
        pkt.src_ip == meta.dst_ip
        and pkt.src_port == meta.dst_port
        and pkt.dst_ip == meta.src_ip
        and pkt.dst_port == meta.src_port
    ):
        return "dst_to_src"

    return None


def match_packet(
    pkt: PacketRecord,
    meta_index: dict[tuple[str, int, str, int], list[RequestMeta]],
) -> tuple[RequestMeta, str] | None:
    # 패킷이 정방향일 때 찾을 수 있는 키
    forward_key = (pkt.src_ip, pkt.src_port, pkt.dst_ip, pkt.dst_port)

    # 패킷이 역방향일 때 원래 요청 메타를 찾기 위한 뒤집은 키
    reverse_key = (pkt.dst_ip, pkt.dst_port, pkt.src_ip, pkt.src_port)

    candidates: list[RequestMeta] = []
    candidates.extend(find_time_candidates(meta_index.get(forward_key, []), pkt.ts_us))

    if reverse_key != forward_key:
        candidates.extend(find_time_candidates(meta_index.get(reverse_key, []), pkt.ts_us))

    matched: list[tuple[RequestMeta, str]] = []

    for meta in candidates:
        direction = get_packet_direction(pkt, meta)
        if direction is None:
            continue

        matched.append((meta, direction))

    if not matched:
        return None

    if len(matched) == 1:
        return matched[0]

    # 여러 요청이 겹치면 패킷 시각과 start_time이 가장 가까운 요청을 선택
    matched.sort(key=lambda item: abs(pkt.ts_us - item[0].start_time_us))
    return matched[0]
