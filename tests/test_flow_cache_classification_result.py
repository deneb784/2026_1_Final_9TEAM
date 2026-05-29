import unittest

from pipeline.flow_cache import FlowCache
from pipeline.models import FlowEntry


class FlowCacheClassificationResultTest(unittest.TestCase):
    def test_apply_classification_result_updates_entry(self):
        cache = FlowCache(feature_packet_count=1)
        entry = FlowEntry(
            src_index=0,
            flow_id=7,
            direction="dst_to_src",
            logical_flow_id="0:7:dst_to_src",
            status="pending",
        )
        cache.entries[entry.flow_key] = entry

        updated = cache.apply_classification_result(
            {
                "request_key": {
                    "src_index": 0,
                    "flow_id": 7,
                    "direction": "dst_to_src",
                },
                "predicted_label": "elephant",
                "score": 0.91,
            }
        )

        self.assertIs(updated, entry)
        self.assertEqual(entry.status, "elephant")
        self.assertEqual(entry.model_score, 0.91)
        self.assertEqual(entry.classification_result["predicted_label"], "elephant")

    def test_apply_classification_result_ignores_other_process_results(self):
        cache = FlowCache(feature_packet_count=1)

        updated = cache.apply_classification_result(
            {
                "request_key": {
                    "src_index": 99,
                    "flow_id": 7,
                    "direction": "dst_to_src",
                },
                "predicted_label": "mice",
            }
        )

        self.assertIsNone(updated)


if __name__ == "__main__":
    unittest.main()
