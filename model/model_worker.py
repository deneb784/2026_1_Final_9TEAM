import redis.asyncio as redis
import json
import asyncio
from typing import Optional
from model.classifier import FlowClassifier, load_model, classify_flow


class ModelWorker:
    def __init__(self, host: str = "localhost", port: int = 6379, queue_name: str = "flow_features", model_path: str = "model_checkpoint.pth"):
        self.host = host
        self.port = port
        self.queue_name = queue_name
        self.redis_client: Optional[redis.Redis] = None
        self.model = load_model(model_path)

    async def connect(self):
        """Redis 연결을 초기화합니다."""
        self.redis_client = redis.Redis(host=self.host, port=self.port, decode_responses=True)

    async def consume(self) -> Optional[str]:
        """Redis 리스트 큐에서 작업을 가져옵니다. (BLPOP)"""
        if self.redis_client is None:
            await self.connect()
        result = await self.redis_client.blpop(self.queue_name, timeout=1)
        if result:
            return result[1]  # blpop returns (key, value)
        return None

    async def process_flow_request(self, payload: str):
        """수신된 플로우 요청을 처리합니다. (딥러닝 모델로 분류)"""
        try:
            flow_request = json.loads(payload)
            print(f"Processing flow request: {flow_request['request_key']}")

            # 특징 추출 및 분류
            features = flow_request["features"]
            classified_features = classify_flow(self.model, features)

            # 분류 결과 로깅
            predicted_class = classified_features["predicted_class"]
            confidence = classified_features["confidence"]
            print(f"Classification result - Class: {predicted_class}, Confidence: {confidence:.4f}")

            # 여기서 추가 처리 가능 (결과 저장, 알림 등)

        except json.JSONDecodeError as e:
            print(f"Invalid JSON payload: {e}")
        except KeyError as e:
            print(f"Missing key in flow request: {e}")
        except Exception as e:
            print(f"Error processing flow request: {e}")

    async def run(self):
        """작업 큐를 지속적으로 소비합니다."""
        await self.connect()
        print(f"Model worker started, consuming from queue: {self.queue_name}")
        try:
            while True:
                payload = await self.consume()
                if payload:
                    await self.process_flow_request(payload)
                else:
                    await asyncio.sleep(0.1)  # 큐가 비어있을 때 잠시 대기
        except KeyboardInterrupt:
            print("Model worker stopped.")
        finally:
            await self.close()

    async def close(self):
        """Redis 연결을 종료합니다."""
        if self.redis_client:
            await self.redis_client.close()
            self.redis_client = None


if __name__ == "__main__":
    worker = ModelWorker(model_path="model_checkpoint.pth")
    asyncio.run(worker.run())