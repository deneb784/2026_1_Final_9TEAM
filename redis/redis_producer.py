import redis.asyncio as redis
from typing import Optional


class RedisProducer:
    def __init__(self, host: str = "localhost", port: int = 6379, queue_name: str = "flow_features"):
        self.host = host
        self.port = port
        self.queue_name = queue_name
        self.redis_client: Optional[redis.Redis] = None

    async def connect(self):
        """Redis 연결을 초기화합니다."""
        self.redis_client = redis.Redis(host=self.host, port=self.port, decode_responses=True)

    async def publish(self, payload: str) -> str:
        """JSON 페이로드를 Redis 리스트 큐에 추가합니다."""
        if self.redis_client is None:
            await self.connect()
        await self.redis_client.rpush(self.queue_name, payload)
        return payload

    async def close(self):
        """Redis 연결을 종료합니다."""
        if self.redis_client:
            await self.redis_client.close()
            self.redis_client = None