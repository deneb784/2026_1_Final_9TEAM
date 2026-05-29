from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pipeline.models import FLOW_STATUSES, PacketRecord


@dataclass(frozen=True)
class OnlineFlowKey:
    """실시간 파이프라인에서 패킷으로 직접 복원할 수 있는 flow 식별자."""

    client_ip: str
    client_port: int
    server_ip: str
    server_port: int
    flow_id: int
    direction: str

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> "OnlineFlowKey":
        return cls(
            client_ip=str(mapping["client_ip"]),
            client_port=int(mapping["client_port"]),
            server_ip=str(mapping["server_ip"]),
            server_port=int(mapping["server_port"]),
            flow_id=int(mapping["flow_id"]),
            direction=str(mapping["direction"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "client_ip": self.client_ip,
            "client_port": self.client_port,
            "server_ip": self.server_ip,
            "server_port": self.server_port,
            "flow_id": self.flow_id,
            "direction": self.direction,
        }

    @property
    def logical_flow_id(self) -> str:
        return (
            f"{self.client_ip}:{self.client_port}->"
            f"{self.server_ip}:{self.server_port}:"
            f"{self.flow_id}:{self.direction}"
        )


@dataclass
class OnlineFlowEntry:
    """실시간 online flow별 packet buffer와 classifier 상태."""

    key: OnlineFlowKey
    request_key: dict[str, Any] | None = None
    packets: list[PacketRecord] = field(default_factory=list)
    payload_bytes: int = 0
    status: str = "default"
    model_score: float | None = None
    classification_result: dict | None = None

    @property
    def flow_id(self) -> int:
        return self.key.flow_id

    @property
    def direction(self) -> str:
        return self.key.direction

    @property
    def logical_flow_id(self) -> str:
        if self.request_key is not None:
            return (
                f"{self.request_key['src_index']}:"
                f"{self.request_key['flow_id']}:"
                f"{self.request_key['direction']}"
            )
        return self.key.logical_flow_id

    @property
    def online_flow_key(self) -> dict[str, Any]:
        return self.key.as_dict()


class RealtimeFlowCache:
    """실시간 pipeline 전용 flow cache.

    오프라인 dataset용 FlowCache는 `(src_index, flow_id, direction)`을 key로 쓰지만,
    이 cache는 XDP packet과 TrafficGenerator payload에서 바로 얻을 수 있는
    `(client_ip, client_port, server_ip, server_port, flow_id, direction)`을 key로 쓴다.
    """

    def __init__(self, feature_packet_count: int):
        self.feature_packet_count = feature_packet_count
        self.entries: dict[OnlineFlowKey, OnlineFlowEntry] = {}
        self._request_key_index: dict[tuple[int, int, str], OnlineFlowKey] = {}

    def _request_key_tuple(self, request_key: dict[str, Any]) -> tuple[int, int, str]:
        return (
            int(request_key["src_index"]),
            int(request_key["flow_id"]),
            str(request_key["direction"]),
        )

    def get_or_create_entry(
        self,
        key: OnlineFlowKey,
        request_key: dict[str, Any] | None = None,
    ) -> OnlineFlowEntry:
        entry = self.entries.get(key)
        if entry is None:
            entry = OnlineFlowEntry(key=key, request_key=dict(request_key) if request_key else None)
            self.entries[key] = entry
        elif entry.request_key is None and request_key is not None:
            entry.request_key = dict(request_key)

        if entry.request_key is not None:
            self._request_key_index[self._request_key_tuple(entry.request_key)] = key
        return entry

    def add_packet(
        self,
        key: OnlineFlowKey,
        pkt: PacketRecord,
        request_key: dict[str, Any] | None = None,
    ) -> OnlineFlowEntry:
        entry = self.get_or_create_entry(key, request_key=request_key)

        if entry.status != "default":
            return entry
        if pkt.tcp_len <= 0:
            return entry

        entry.payload_bytes += pkt.tcp_len
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
        entry = None
        if online_flow_key:
            entry = self.entries.get(OnlineFlowKey.from_mapping(online_flow_key))
        elif result.get("request_key"):
            # 구버전 payload 호환용 fallback. 새 실시간 경로는 online_flow_key를 사용한다.
            key = self._request_key_index.get(self._request_key_tuple(result["request_key"]))
            if key is not None:
                entry = self.entries.get(key)

        if entry is None:
            return None

        label = result.get("predicted_label") or result.get("label")
        self.mark_classified(entry, str(label))
        if result.get("score") is not None:
            entry.model_score = float(result["score"])
        entry.classification_result = dict(result)
        return entry
