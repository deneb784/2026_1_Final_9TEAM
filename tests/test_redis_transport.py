import unittest

from pipeline.redis.transport import RedisStreamProducer


class FakeRedisClient:
    """실제 Redis 서버 대신 xadd 호출 내용을 메모리에 기록하는 테스트 더블."""

    def __init__(self):
        self.calls = []

    def xadd(self, stream_name, fields, **kwargs):
        # Redis Stream의 XADD는 stream 이름, field-value 맵, maxlen 같은 옵션을 받는다.
        # 테스트에서는 네트워크 없이 producer가 어떤 entry를 쓰려 했는지만 검증한다.
        self.calls.append((stream_name, fields, kwargs))
        return "1234567890-0"


class RedisStreamProducerTest(unittest.TestCase):
    def test_publish_adds_latency_fields(self):
        # publish는 실제 요청 Stream과 latency 보조 Stream에 각각 XADD를 한 번씩 호출한다.
        # Pub/Sub과 달리 Stream은 entry가 저장되므로, worker가 나중에 stream_id 기준으로 읽을 수 있다.
        producer = RedisStreamProducer(
            redis_url="redis://127.0.0.1:6379/0",
            stream_name="flow_features",
        )
        fake_client = FakeRedisClient()
        # connect()를 타지 않도록 fake client를 직접 주입한다.
        producer._client = fake_client

        stream_id = producer.publish(
            {
                "request_key": {
                    "src_index": 0,
                    "flow_id": 7,
                    "direction": "dst_to_src",
                },
                "logical_flow_id": "0:7:dst_to_src",
                "run_id": "unit",
                "payload": [],
                "producer_metrics": {
                    "capture_mode": "xdp",
                    "feature_ready_wall_ns": 123,
                    "first_packet_ts_us": 1_000,
                    "last_packet_ts_us": 2_000,
                },
            }
        )

        self.assertEqual(stream_id, "1234567890-0")
        self.assertEqual(len(fake_client.calls), 2)
        # 첫 번째 XADD는 classifier worker가 읽어 갈 본 요청 Stream entry다.
        stream_name, fields, kwargs = fake_client.calls[0]
        self.assertEqual(stream_name, "flow_features")
        self.assertEqual(kwargs, {})
        self.assertEqual(fields["run_id"], "unit")
        self.assertEqual(fields["capture_mode"], "xdp")
        self.assertEqual(fields["feature_ready_wall_ns"], "123")
        self.assertEqual(fields["first_packet_ts_us"], "1000")
        self.assertEqual(fields["last_packet_ts_us"], "2000")
        self.assertIn("publish_start_wall_ns", fields)

        # 두 번째 XADD는 지연 분석용 보조 Stream entry다.
        # source_stream_id로 본 요청 entry와 다시 연결할 수 있게 한다.
        latency_stream_name, latency_fields, latency_kwargs = fake_client.calls[1]
        self.assertEqual(latency_stream_name, "flow_features:latency")
        self.assertEqual(latency_kwargs, {})
        self.assertEqual(latency_fields["source_stream_id"], "1234567890-0")
        self.assertEqual(latency_fields["feature_ready_wall_ns"], "123")
        self.assertIn("publish_end_wall_ns", latency_fields)


if __name__ == "__main__":
    unittest.main()
