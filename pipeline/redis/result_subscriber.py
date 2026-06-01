import json
import threading
import time
from collections.abc import Callable
from typing import Any


class RedisResultSubscriber:
    """Classifier -> Client 추론 결과를 Redis Pub/Sub으로 수신한다.

    Redis Pub/Sub은 Stream과 다르게 메시지를 저장하지 않는 실시간 방송 채널이다.
    publish 시점에 구독 중인 subscriber만 메시지를 받으며, 나중에 접속한 쪽은 과거 메시지를
    다시 읽을 수 없다. 여기서는 classifier 결과를 즉시 받아 화면/로그/후처리에 넘기는 용도로 쓴다.
    """

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
        self._thread: threading.Thread | None = None
        self._closed = False

    def connect(self) -> None:
        # Pub/Sub도 Redis 서버 연결은 redis-py client를 사용한다.
        # pubsub()으로 구독 전용 객체를 만들고, channel_name을 subscribe하면 listen()에서 메시지가 나온다.
        if self._client is not None:
            return
        self._closed = False
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("redis 패키지가 필요합니다. requirements_redis.txt를 설치하세요.") from exc

        self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)
        self._pubsub = self._client.pubsub()
        self._pubsub.subscribe(self.channel_name)

    def listen_forever(self) -> None:
        """결과 채널을 계속 구독하면서 메시지마다 callback 또는 stdout 출력을 수행한다."""
        self.connect()
        assert self._pubsub is not None

        try:
            for message in self._pubsub.listen():
                # subscribe 직후 Redis는 "subscribe 성공" 같은 control 메시지도 보낸다.
                # 실제 classifier 결과는 type == "message"인 항목만 해당한다.
                if message.get("type") != "message":
                    continue
                subscriber_received_wall_ns = time.time_ns()
                # classifier는 결과 dict를 JSON 문자열로 publish한다고 가정한다.
                result = json.loads(message["data"])
                result["subscriber_received_wall_ns"] = subscriber_received_wall_ns
                if self.on_result is not None:
                    # 테스트나 애플리케이션 코드가 직접 처리하고 싶을 때 callback을 사용한다.
                    self.on_result(result)
                else:
                    # callback이 없으면 CLI subscriber처럼 한 줄 JSON으로 바로 흘려보낸다.
                    print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception:
            if not self._closed:
                raise

    def start_background(self) -> threading.Thread:
        """결과 구독 loop를 daemon thread에서 시작한다."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(target=self.listen_forever, daemon=True)
        self._thread.start()
        return self._thread

    def close(self) -> None:
        # pubsub 객체와 Redis client를 모두 닫아야 listen loop/소켓 리소스가 정리된다.
        self._closed = True
        if self._pubsub is not None:
            self._pubsub.close()
            self._pubsub = None
        if self._client is not None:
            self._client.close()
            self._client = None
