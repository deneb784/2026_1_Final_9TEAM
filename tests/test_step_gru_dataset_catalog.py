import unittest
from pathlib import Path

from model.step_GRU.dataset_catalog import resolve_dataset_config


class StepGruDatasetCatalogTest(unittest.TestCase):
    def test_resolves_dataset_weights_and_split_paths(self):
        config = resolve_dataset_config(dataset_type="fb", seq_len=10, root="dataset")

        self.assertEqual(
            config.weights_path,
            Path("dataset/elephant_dst_to_src/fb/seq10/weights.pt"),
        )
        self.assertEqual(
            config.jsonl_path("test"),
            Path("dataset/elephant_dst_to_src/fb/seq10/test.jsonl"),
        )


if __name__ == "__main__":
    unittest.main()
