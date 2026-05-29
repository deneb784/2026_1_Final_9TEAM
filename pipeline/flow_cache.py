from pipeline.models import FLOW_STATUSES, FlowEntry, PacketRecord, RequestMeta


class FlowCache:
    """논리 flow별로 모델 입력에 필요한 패킷들을 모아 두는 캐시.

    FlowCache는 flow를 직접 판별하지 않는다. CSV 파이프라인이나 온라인 XDP 파이프라인이
    RequestMeta와 direction을 넘겨 주면, 여기서는 같은 flow에 속한 payload 패킷을
    feature_packet_count개까지만 저장하고 현재 처리 상태를 관리한다.
    """

    def __init__(self, feature_packet_count: int):
        # 모델 feature를 만들 때 필요한 "앞 N개 payload 패킷" 개수.
        # 이 개수만큼 packets가 쌓이면 classifier로 보낼 준비가 된 것으로 본다.
        self.feature_packet_count = feature_packet_count

        # key: (src_index, flow_id, direction)
        # value: 해당 논리 flow에서 지금까지 모은 패킷/바이트/분류 상태.
        #
        # direction을 key에 포함하는 이유는 같은 TrafficGenerator flow_id라도
        # 요청(src_to_dst)과 응답(dst_to_src)을 서로 다른 모델 입력으로 다루기 때문이다.
        self.entries: dict[tuple[int, int, str], FlowEntry] = {}

    # ------------------------------------------------------------------
    # 공통/데이터셋용 기본 함수
    #
    # 오프라인 데이터셋 파이프라인과 온라인 XDP 파이프라인이 모두 사용하는 부분이다.
    # 호출자가 이미 flow 매칭을 끝낸 뒤 RequestMeta, direction, PacketRecord를 넘겨 주면,
    # 여기서는 같은 logical flow의 앞 N개 payload 패킷을 모으고 ready 여부만 판단한다.
    # ------------------------------------------------------------------

    def _make_flow_key(self, meta: RequestMeta, direction: str) -> tuple[int, int, str]:
        # FlowEntry.flow_key와 같은 형식으로 캐시 조회용 key를 만든다.
        # src_index는 dataset/label에서 쓰는 송신 host 번호이고,
        # flow_id는 TrafficGenerator 또는 오프라인 trace에서 온 논리 flow 번호다.
        return (meta.src_index, meta.flow_id, direction)

    def _make_flow_key_from_request_key(self, request_key: dict) -> tuple[int, int, str]:
        # classifier result payload의 request_key를 FlowCache 내부 key 형식으로 바꾼다.
        return (
            int(request_key["src_index"]),
            int(request_key["flow_id"]),
            str(request_key["direction"]),
        )

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

        # default가 아닌 entry는 이미 Redis로 보내졌거나(pending) 분류가 끝난 상태다.
        # 그 이후에 늦게 도착한 패킷을 추가하면 모델 입력이 바뀌므로 더 이상 수정하지 않는다.
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
        # 아직 전송/분류되지 않은(default) entry가 feature_packet_count개 패킷을 모으면 ready.
        # pending/elephant/mice 상태는 이미 처리 중이거나 처리 완료된 entry라 다시 ready로 보지 않는다.
        return entry.status == "default" and len(entry.packets) >= self.feature_packet_count

    # ------------------------------------------------------------------
    # 실시간/전송 상태 관리 함수
    #
    # Redis Stream 전송, classifier 결과 반영처럼 "한 번 ready 된 flow를 어떻게 처리 중인지"를
    # 표시하는 부분이다. 데이터셋만 만들 때는 보통 add_packet()/is_ready()/entries만으로 충분하고,
    # 실시간 경로에서는 pending 상태를 사용해 같은 flow가 반복 전송되는 것을 막는다.
    # ------------------------------------------------------------------

    def set_status(self, entry: FlowEntry, status: str) -> None:
        # 상태 문자열 오타가 생기면 ready_entries 필터링이나 결과 집계가 깨질 수 있으므로
        # models.FLOW_STATUSES에 등록된 값만 허용한다.
        if status not in FLOW_STATUSES:
            raise ValueError(f"invalid flow status: {status}")
        entry.status = status

    def mark_pending(self, entry: FlowEntry) -> None:
        # classifier worker로 보냈지만 아직 elephant/mice 결과를 받지 못한 상태.
        # 이 상태로 바꿔 두면 같은 flow를 Redis Stream에 중복 전송하지 않는다.
        self.set_status(entry, "pending")

    def mark_classified(self, entry: FlowEntry, label: str) -> None:
        # classifier가 반환하는 최종 label만 허용한다.
        # 현재 파이프라인의 binary classification 결과는 elephant 또는 mice다.
        if label not in ("elephant", "mice"):
            raise ValueError(f"invalid classification label: {label}")
        self.set_status(entry, label)

    def apply_classification_result(self, result: dict) -> FlowEntry | None:
        """classifier result를 캐시 entry에 반영한다.

        다른 XDP subprocess가 받은 Pub/Sub 결과도 같은 channel로 들어올 수 있으므로,
        이 캐시에 없는 request_key는 조용히 무시하고 None을 반환한다.
        """
        request_key = result.get("request_key")
        if not request_key:
            raise ValueError("classification result is missing request_key")

        entry = self.entries.get(self._make_flow_key_from_request_key(request_key))
        if entry is None:
            return None

        label = result.get("predicted_label") or result.get("label")
        self.mark_classified(entry, str(label))
        if result.get("score") is not None:
            entry.model_score = float(result["score"])
        entry.classification_result = dict(result)
        return entry

    def mark_feature_sent(self, entry: FlowEntry) -> None:
        # 이름은 "feature를 보냈다"는 의미이고, 내부 상태로는 pending과 같다.
        # 별도 메서드로 둔 덕분에 호출부는 Redis/worker 전송 의미를 더 분명하게 표현할 수 있다.
        self.mark_pending(entry)
