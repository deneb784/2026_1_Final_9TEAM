import json
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

        fields = {
            "request_key": json.dumps(request["request_key"], ensure_ascii=False),
            "logical_flow_id": request["logical_flow_id"],
            "payload": json.dumps(request, ensure_ascii=False),
        }
        kwargs = {}
        if self.maxlen is not None:
            kwargs = {"maxlen": self.maxlen, "approximate": True}

        stream_id = self._client.xadd(self.stream_name, fields, **kwargs)
        return str(stream_id)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
