import json
from collections.abc import Callable
from typing import Any


class RedisResultSubscriber:
    """Classifier -> Client 추론 결과를 Redis Pub/Sub으로 수신한다."""

    def __init__(
        self,
        redis_url: str,
        channel_name: str = "flow_results",
        on_result: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.redis_url = redis_url
        self.channel_name = channel_name
        self.on_result = on_result
        self._client = None
        self._pubsub = None

    def connect(self) -> None:
        if self._client is not None:
            return
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("redis 패키지가 필요합니다. requirements_redis.txt를 설치하세요.") from exc

        self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)
        self._pubsub = self._client.pubsub()
        self._pubsub.subscribe(self.channel_name)

    def listen_forever(self) -> None:
        self.connect()
        assert self._pubsub is not None

        for message in self._pubsub.listen():
            if message.get("type") != "message":
                continue
            result = json.loads(message["data"])
            if self.on_result is not None:
                self.on_result(result)
            else:
                print(json.dumps(result, ensure_ascii=False), flush=True)

    def close(self) -> None:
        if self._pubsub is not None:
            self._pubsub.close()
            self._pubsub = None
        if self._client is not None:
            self._client.close()
            self._client = None
