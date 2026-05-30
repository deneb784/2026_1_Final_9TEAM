from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pipeline.models import FLOW_STATUSES, PacketRecord


@dataclass(frozen=True)
class OnlineFlowKey:
    """실시간 파이프라인에서 패킷 방향 그대로 쓰는 flow 식별자."""

    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    flow_id: int

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> "OnlineFlowKey":
        return cls(
            src_ip=str(mapping["src_ip"]),
            src_port=int(mapping["src_port"]),
            dst_ip=str(mapping["dst_ip"]),
            dst_port=int(mapping["dst_port"]),
            flow_id=int(mapping["flow_id"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "src_ip": self.src_ip,
            "src_port": self.src_port,
            "dst_ip": self.dst_ip,
            "dst_port": self.dst_port,
            "flow_id": self.flow_id,
        }

    def logical_flow_id(self, direction: str) -> str:
        return (
            f"{self.src_ip}:{self.src_port}->"
            f"{self.dst_ip}:{self.dst_port}:"
            f"{self.flow_id}:{direction}"
        )


@dataclass
class OnlineFlowEntry:
    """실시간 online flow별 packet buffer와 classifier 상태."""

    key: OnlineFlowKey
    direction: str
    packets: list[PacketRecord] = field(default_factory=list)
    cumulative_payload_bytes: int = 0
    status: str = "default"
    model_score: float | None = None
    classification_result: dict | None = None

    @property
    def flow_id(self) -> int:
        return self.key.flow_id

    @property
    def logical_flow_id(self) -> str:
        return self.key.logical_flow_id(self.direction)

    @property
    def online_flow_key(self) -> dict[str, Any]:
        return {**self.key.as_dict(), "direction": self.direction}


class RealtimeFlowCache:
    """실시간 pipeline 전용 flow cache.

    오프라인 dataset용 FlowCache는 `(src_index, flow_id, direction)`을 key로 쓰지만,
    이 cache는 온라인 캡처 packet과 TrafficGenerator payload에서 바로 얻을 수 있는
    `(src_ip, src_port, dst_ip, dst_port, flow_id)`를 key로 쓴다.
    """

    def __init__(self, feature_packet_count: int):
        self.feature_packet_count = feature_packet_count
        self.entries: dict[OnlineFlowKey, OnlineFlowEntry] = {}

    def get_or_create_entry(self, key: OnlineFlowKey, direction: str) -> OnlineFlowEntry:
        entry = self.entries.get(key)
        if entry is None:
            entry = OnlineFlowEntry(key=key, direction=direction)
            self.entries[key] = entry
        return entry

    def add_packet(
        self,
        key: OnlineFlowKey,
        direction: str,
        pkt: PacketRecord,
    ) -> OnlineFlowEntry:
        entry = self.get_or_create_entry(key, direction)

        if entry.status != "default":
            return entry
        if pkt.tcp_len <= 0:
            return entry

        entry.cumulative_payload_bytes += pkt.tcp_len
        if len(entry.packets) < self.feature_packet_count:
            entry.packets.append(pkt)
        return entry

    def is_ready(self, entry: OnlineFlowEntry) -> bool:
        return entry.status == "default" and len(entry.packets) >= self.feature_packet_count

    def set_status(self, entry: OnlineFlowEntry, status: str) -> None:
        if status not in FLOW_STATUSES:
            raise ValueError(f"invalid flow status: {status}")
        entry.status = status

    def mark_pending(self, entry: OnlineFlowEntry) -> None:
        self.set_status(entry, "pending")

    def mark_classified(self, entry: OnlineFlowEntry, label: str) -> None:
        if label not in ("elephant", "mice"):
            raise ValueError(f"invalid classification label: {label}")
        self.set_status(entry, label)

    def apply_classification_result(self, result: dict[str, Any]) -> OnlineFlowEntry | None:
        online_flow_key = result.get("online_flow_key")
        if not online_flow_key:
            return None
        entry = self.entries.get(OnlineFlowKey.from_mapping(online_flow_key))
        if entry is None:
            return None

        label = result.get("predicted_label") or result.get("label")
        self.mark_classified(entry, str(label))
        if result.get("score") is not None:
            entry.model_score = float(result["score"])
        entry.classification_result = dict(result)
        return entry
