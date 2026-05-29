import json
import tempfile
import unittest
from pathlib import Path

from analyze.online_result_metrics import (
    evaluate_predictions,
    infer_prediction_threshold,
    load_predictions,
    load_truth_from_jsonl,
    load_truth_from_meta_dir,
)


class OnlineResultMetricsTest(unittest.TestCase):
    def test_evaluates_online_results_against_continuous_jsonl_labels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            result_log = tmp / "online.jsonl"
            truth_jsonl = tmp / "truth.jsonl"

            result_log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "request_key": {
                                    "src_index": 0,
                                    "flow_id": 1,
                                    "direction": "dst_to_src",
                                },
                                "run_id": "unit",
                                "logical_flow_id": "0:1:dst_to_src",
                                "score": 0.9,
                                "threshold": 0.8,
                                "predicted_label": "elephant",
                                "stream_id": "1-0",
                            }
                        ),
                        json.dumps(
                            {
                                "request_key": {
                                    "src_index": 0,
                                    "flow_id": 2,
                                    "direction": "dst_to_src",
                                },
                                "run_id": "unit",
                                "logical_flow_id": "0:2:dst_to_src",
                                "score": 0.2,
                                "threshold": 0.8,
                                "predicted_label": "mice",
                                "stream_id": "2-0",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            truth_jsonl.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "flow_key": {
                                    "src_index": 0,
                                    "flow_id": 1,
                                    "direction": "dst_to_src",
                                },
                                "label": 0.95,
                            }
                        ),
                        json.dumps(
                            {
                                "flow_key": {
                                    "src_index": 0,
                                    "flow_id": 2,
                                    "direction": "dst_to_src",
                                },
                                "label": 0.10,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            predictions = load_predictions(result_log, run_id="unit")
            threshold = infer_prediction_threshold(predictions, None)
            truth, size_threshold = load_truth_from_jsonl(
                [truth_jsonl],
                mode="label",
                label_field="label",
                label_threshold=threshold,
                size_field="directional_size_bytes",
                size_threshold_bytes=None,
                size_quantile=threshold,
            )
            metrics, evaluated, missing = evaluate_predictions(predictions, truth, threshold)

            self.assertIsNone(size_threshold)
            self.assertEqual(len(evaluated), 2)
            self.assertEqual(missing, [])
            self.assertEqual(metrics["tp"], 1)
            self.assertEqual(metrics["tn"], 1)
            self.assertEqual(metrics["recall"], 1.0)
            self.assertEqual(metrics["f1"], 1.0)

    def test_meta_dir_truth_uses_size_threshold_for_both_directions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            meta_path = tmp / "flows_0_meta.csv"
            meta_path.write_text(
                "\n".join(
                    [
                        "src_index,flow_id,server_id,connection_id,src_ip,src_port,dst_ip,dst_port,size_bytes,dscp,rate_mbps,start_time_us,stop_time_us,fct_us,src_to_dst_flow_id,dst_to_src_flow_id,src_to_dst_tos,dst_to_src_tos",
                        "0,1,0,0,10.0.0.1,1000,10.0.0.2,5001,100,0,80,1,2,1,0:1:src_to_dst,0:1:dst_to_src,0,1",
                        "0,2,0,0,10.0.0.1,1001,10.0.0.2,5001,300,0,80,1,2,1,0:2:src_to_dst,0:2:dst_to_src,0,1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            truth, threshold = load_truth_from_meta_dir(
                tmp,
                size_threshold_bytes=200,
                size_quantile=0.8,
            )

            self.assertEqual(threshold, 200)
            self.assertEqual(truth[(0, 1, "dst_to_src")], 0)
            self.assertEqual(truth[(0, 2, "dst_to_src")], 1)
            self.assertEqual(truth[(0, 2, "src_to_dst")], 1)


if __name__ == "__main__":
    unittest.main()
