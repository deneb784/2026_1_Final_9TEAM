import unittest

from analyze.online_e2e_latency import build_summary


class OnlineE2ELatencyTest(unittest.TestCase):
    def test_build_summary_counts_completed_flowcache_updates(self):
        rows = [
            {
                "logical_flow_id": "0:1:dst_to_src",
                "ready_to_cache_updated_ms": 4.0,
                "ready_to_worker_received_ms": 1.0,
                "worker_received_to_done_ms": 2.0,
                "worker_done_to_publish_ms": 0.1,
                "pubsub_publish_to_subscriber_ms": 0.5,
                "subscriber_to_cache_updated_ms": 0.2,
                "cache_apply_duration_ms": 0.05,
                "inference_ms": 1.8,
            },
            {
                "logical_flow_id": "0:2:dst_to_src",
                "ready_to_cache_updated_ms": 6.0,
                "ready_to_worker_received_ms": 2.0,
                "worker_received_to_done_ms": 3.0,
                "worker_done_to_publish_ms": 0.2,
                "pubsub_publish_to_subscriber_ms": 0.7,
                "subscriber_to_cache_updated_ms": 0.3,
                "cache_apply_duration_ms": 0.07,
                "inference_ms": 2.7,
            },
        ]

        summary = build_summary(rows)

        self.assertEqual(summary["classified_rows"], 2)
        self.assertEqual(summary["unique_logical_flows"], 2)
        self.assertEqual(summary["ready_to_cache_updated_ms"]["p50"], 5.0)
        self.assertEqual(summary["pubsub_publish_to_subscriber_ms"]["max"], 0.7)


if __name__ == "__main__":
    unittest.main()
