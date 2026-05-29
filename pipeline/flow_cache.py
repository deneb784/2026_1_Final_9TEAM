from pipeline.models import FlowEntry, PacketRecord, RequestMeta


class FlowCache:
    """오프라인 dataset 생성용으로 logical flow별 패킷을 모아 두는 캐시.

    CSV/pcap 파이프라인이 RequestMeta와 direction을 넘겨 주면, 여기서는 같은 flow에
    속한 payload 패킷을 feature_packet_count개까지만 저장하고 ready 여부를 판단한다.
    실시간 XDP 경로는 `pipeline.realtime.online_flow_cache.RealtimeFlowCache`를 사용한다.
    """

    def __init__(self, feature_packet_count: int):
        # 모델 feature를 만들 때 필요한 "앞 N개 payload 패킷" 개수.
        # 이 개수만큼 packets가 쌓이면 classifier로 보낼 준비가 된 것으로 본다.
        self.feature_packet_count = feature_packet_count

        # key: (src_index, flow_id, direction)
        # value: 해당 논리 flow에서 지금까지 모은 패킷/바이트.
        #
        # direction을 key에 포함하는 이유는 같은 TrafficGenerator flow_id라도
        # 요청(src_to_dst)과 응답(dst_to_src)을 서로 다른 모델 입력으로 다루기 때문이다.
        self.entries: dict[tuple[int, int, str], FlowEntry] = {}

    # ------------------------------------------------------------------
    # 오프라인 데이터셋용 기본 함수
    #
    # 호출자가 이미 flow 매칭을 끝낸 뒤 RequestMeta, direction, PacketRecord를 넘겨 주면,
    # 여기서는 같은 logical flow의 앞 N개 payload 패킷을 모으고 ready 여부만 판단한다.
    # ------------------------------------------------------------------

    def _make_flow_key(self, meta: RequestMeta, direction: str) -> tuple[int, int, str]:
        # FlowEntry.flow_key와 같은 형식으로 캐시 조회용 key를 만든다.
        # src_index는 dataset/label에서 쓰는 송신 host 번호이고,
        # flow_id는 TrafficGenerator 또는 오프라인 trace에서 온 논리 flow 번호다.
        return (meta.src_index, meta.flow_id, direction)

    def _get_logical_flow_id(self, meta: RequestMeta, direction: str) -> str:
        # downstream 저장/로그/Redis payload에서 사람이 확인하기 쉬운 문자열 flow id를 고른다.
        # direction에 따라 RequestMeta 안에 미리 만들어 둔 양방향 id가 다르다.
        if direction == "src_to_dst":
            return meta.src_to_dst_flow_id
        if direction == "dst_to_src":
            return meta.dst_to_src_flow_id

        # 여기까지 왔다면 호출자가 지원하지 않는 방향 문자열을 넘긴 것이다.
        # 조용히 default로 처리하면 서로 다른 flow가 섞일 수 있으므로 즉시 실패시킨다.
        raise ValueError(f"invalid direction: {direction}")

    def get_or_create_entry(self, meta: RequestMeta, direction: str) -> FlowEntry:
        # 같은 (src_index, flow_id, direction)이 이미 있으면 기존 entry를 재사용하고,
        # 처음 보는 flow라면 빈 FlowEntry를 만들어 캐시에 등록한다.
        flow_key = self._make_flow_key(meta, direction)

        if flow_key not in self.entries:
            self.entries[flow_key] = FlowEntry(
                src_index=meta.src_index,
                flow_id=meta.flow_id,
                direction=direction,
                logical_flow_id=self._get_logical_flow_id(meta, direction),
            )

        return self.entries[flow_key]

    def add_packet(self, meta: RequestMeta, direction: str, pkt: PacketRecord) -> FlowEntry:
        # 패킷이 들어온 flow entry를 가져온다. 아직 없으면 여기서 새로 만든다.
        entry = self.get_or_create_entry(meta, direction)

        # 오프라인 dataset 생성 중에는 보통 default 상태만 사용한다. 상태가 바뀐 entry는
        # 호출자가 더 이상 입력 feature를 수정하지 않겠다는 의미로 보고 패킷을 추가하지 않는다.
        if entry.status != "default":
            return entry

        # tcp_len은 TCP payload 길이다. 0 이하인 패킷은 보통 ACK-only/control 패킷이라
        # payload 기반 feature에 넣지 않고 payload byte 누적에도 반영하지 않는다.
        if pkt.tcp_len <= 0:
            return entry

        # 전체 payload byte 수는 feature_packet_count 이후에도 계속 누적한다.
        # 즉, packets에는 앞 N개만 저장하지만 payload_bytes는 관측된 payload 총량을 나타낸다.
        entry.payload_bytes += pkt.tcp_len

        # 모델은 flow 초반 N개 payload 패킷을 feature로 사용하므로 그 이상은 저장하지 않는다.
        # 메모리를 아끼고, classifier에 보낼 입력 크기를 일정하게 유지하기 위한 제한이다.
        if len(entry.packets) < self.feature_packet_count:
            entry.packets.append(pkt)
        return entry

    def is_ready(self, entry: FlowEntry) -> bool:
        # 아직 처리되지 않은(default) entry가 feature_packet_count개 패킷을 모으면 ready.
        return entry.status == "default" and len(entry.packets) >= self.feature_packet_count
