import unittest

from pipeline.redis.transport import RedisStreamProducer


class FakeRedisClient:
    def __init__(self):
        self.calls = []

    def xadd(self, stream_name, fields, **kwargs):
        self.calls.append((stream_name, fields, kwargs))
        return "1234567890-0"


class RedisStreamProducerTest(unittest.TestCase):
    def test_publish_adds_latency_fields(self):
        producer = RedisStreamProducer(
            redis_url="redis://127.0.0.1:6379/0",
            stream_name="flow_features",
        )
        fake_client = FakeRedisClient()
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
        stream_name, fields, kwargs = fake_client.calls[0]
        self.assertEqual(stream_name, "flow_features")
        self.assertEqual(kwargs, {})
        self.assertEqual(fields["run_id"], "unit")
        self.assertEqual(fields["capture_mode"], "xdp")
        self.assertEqual(fields["feature_ready_wall_ns"], "123")
        self.assertEqual(fields["first_packet_ts_us"], "1000")
        self.assertEqual(fields["last_packet_ts_us"], "2000")
        self.assertIn("publish_start_wall_ns", fields)

        latency_stream_name, latency_fields, latency_kwargs = fake_client.calls[1]
        self.assertEqual(latency_stream_name, "flow_features:latency")
        self.assertEqual(latency_kwargs, {})
        self.assertEqual(latency_fields["source_stream_id"], "1234567890-0")
        self.assertEqual(latency_fields["feature_ready_wall_ns"], "123")
        self.assertIn("publish_end_wall_ns", latency_fields)


if __name__ == "__main__":
    unittest.main()
