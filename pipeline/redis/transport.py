import json
import time
from typing import Any


class RedisStreamProducer:
    """Client -> Classifier 추론 요청을 Redis Stream에 안정적으로 적재한다.

    Redis Stream은 Redis의 append-only 메시지 로그다. Pub/Sub처럼 "지금 구독 중인 사람"에게만
    날아가는 방식이 아니라, XADD로 쌓인 entry를 consumer가 나중에 ID 기준으로 읽을 수 있다.
    그래서 온라인 feature 요청처럼 누락되면 안 되는 작업 큐에 가깝게 사용한다.
    """

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
        # redis-py client는 실제 Redis 서버와 통신하는 객체다.
        # decode_responses=True를 켜면 Redis bytes 응답을 Python str로 받아 JSON 처리와 테스트가 단순해진다.
        if self._client is not None:
            return
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("redis 패키지가 필요합니다. requirements_redis.txt를 설치하세요.") from exc

        self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)
        self._client.ping()

    def publish(self, request: dict[str, Any]) -> str:
        """요청 dict를 Redis Stream entry로 추가하고, 생성된 stream ID를 반환한다."""
        if self._client is None:
            self.connect()
        assert self._client is not None

        metrics = request.get("producer_metrics") or {}
        publish_start_wall_ns = time.time_ns()
        # Redis Stream entry는 field-value 맵이다. 복잡한 dict/list는 JSON 문자열로 넣는다.
        # payload에는 worker가 추론에 필요한 전체 request를 담고,
        # 자주 필터링하거나 CSV로 뽑을 값은 별도 field로 한 번 더 복사한다.
        fields = {
            "logical_flow_id": request["logical_flow_id"],
            "payload": json.dumps(request, ensure_ascii=False),
            "publish_start_wall_ns": str(publish_start_wall_ns),
        }
        if request.get("online_flow_key") is not None:
            fields["online_flow_key"] = json.dumps(request["online_flow_key"], ensure_ascii=False)
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

        # maxlen을 주면 Redis가 Stream 길이를 대략적으로 제한한다.
        # approximate=True는 정확한 trim보다 빠르지만, 지정 길이보다 조금 더 남을 수 있다.
        kwargs = {}
        if self.maxlen is not None:
            kwargs = {"maxlen": self.maxlen, "approximate": True}

        # 핵심 요청 Stream. classifier worker는 이 Stream에서 새 entry를 읽고 payload를 JSON으로 복원한다.
        stream_id = self._client.xadd(self.stream_name, fields, **kwargs)
        publish_end_wall_ns = time.time_ns()
        # latency 전용 보조 Stream. 요청 본문과 분리해 두면 나중에 지연 분석만 빠르게 훑을 수 있다.
        # Redis Stream ID는 millisecond 정밀도라 sub-ms latency 분석에는 producer wall-clock이 더 안정적이다.
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
