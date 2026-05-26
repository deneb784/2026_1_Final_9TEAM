import json
import time
from typing import Any


class RedisStreamProducer:
    """Client -> Classifier 추론 요청을 Redis Stream에 안정적으로 적재한다."""

    def __init__(
        self,
        redis_url: str,
        stream_name: str = "flow_features",
        maxlen: int | None = None,
    ):
        self.redis_url = redis_url
        self.stream_name = stream_name
        self.maxlen = maxlen
        self._client = None

    def connect(self) -> None:
        if self._client is not None:
            return
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("redis 패키지가 필요합니다. requirements_redis.txt를 설치하세요.") from exc

        self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)
        self._client.ping()

    def publish(self, request: dict[str, Any]) -> str:
        if self._client is None:
            self.connect()
        assert self._client is not None

        metrics = request.get("producer_metrics") or {}
        publish_start_wall_ns = time.time_ns()
        fields = {
            "request_key": json.dumps(request["request_key"], ensure_ascii=False),
            "logical_flow_id": request["logical_flow_id"],
            "payload": json.dumps(request, ensure_ascii=False),
            "publish_start_wall_ns": str(publish_start_wall_ns),
        }
        if request.get("run_id") is not None:
            fields["run_id"] = str(request["run_id"])
        if metrics.get("capture_mode") is not None:
            fields["capture_mode"] = str(metrics["capture_mode"])
        if metrics.get("feature_ready_wall_ns") is not None:
            fields["feature_ready_wall_ns"] = str(metrics["feature_ready_wall_ns"])
        if metrics.get("first_packet_ts_us") is not None:
            fields["first_packet_ts_us"] = str(metrics["first_packet_ts_us"])
        if metrics.get("last_packet_ts_us") is not None:
            fields["last_packet_ts_us"] = str(metrics["last_packet_ts_us"])

        kwargs = {}
        if self.maxlen is not None:
            kwargs = {"maxlen": self.maxlen, "approximate": True}

        stream_id = self._client.xadd(self.stream_name, fields, **kwargs)
        publish_end_wall_ns = time.time_ns()
        # XADD가 끝난 뒤 같은 entry를 보강한다. Redis Stream ID는 millisecond 정밀도라
        # sub-ms latency 분석에는 producer wall-clock이 더 안정적이다.
        self._client.xadd(
            self.stream_name + ":latency",
            {
                "source_stream_id": str(stream_id),
                "publish_end_wall_ns": str(publish_end_wall_ns),
                "publish_start_wall_ns": str(publish_start_wall_ns),
                "feature_ready_wall_ns": str(metrics.get("feature_ready_wall_ns", "")),
                "run_id": str(request.get("run_id", "")),
                "capture_mode": str(metrics.get("capture_mode", "")),
                "logical_flow_id": request["logical_flow_id"],
            },
            **kwargs,
        )
        return str(stream_id)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
