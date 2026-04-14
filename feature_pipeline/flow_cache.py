from feature_pipeline.models import FlowEntry, PacketRecord, RequestMeta


class FlowCache:
    def __init__(self, feature_packet_count: int):
        self.feature_packet_count = feature_packet_count
        self.entries: dict[tuple[int, int, str], FlowEntry] = {}

    def _make_flow_key(self, meta: RequestMeta, direction: str) -> tuple[int, int, str]:
        return (meta.src_index, meta.flow_id, direction)

    def _get_logical_flow_id(self, meta: RequestMeta, direction: str) -> str:
        if direction == "src_to_dst":
            return meta.src_to_dst_flow_id
        if direction == "dst_to_src":
            return meta.dst_to_src_flow_id
        raise ValueError(f"invalid direction: {direction}")

    def get_or_create_entry(self, meta: RequestMeta, direction: str) -> FlowEntry:
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
        entry = self.get_or_create_entry(meta, direction)
        entry.packets.append(pkt)
        return entry

    def is_ready(self, entry: FlowEntry) -> bool:
        return (not entry.feature_sent) and len(entry.packets) >= self.feature_packet_count

    def mark_feature_sent(self, entry: FlowEntry) -> None:
        entry.feature_sent = True
