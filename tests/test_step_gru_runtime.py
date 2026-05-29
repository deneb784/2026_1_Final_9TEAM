import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path


try:
    import torch
except ImportError:  # pragma: no cover
    torch = None


@unittest.skipIf(torch is None, "torch is not installed")
class StepGruRuntimeTest(unittest.TestCase):
    def test_resolve_torch_device_uses_cuda_for_auto_when_available(self):
        from model.step_GRU.stream_worker import resolve_torch_device

        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: True),
            device=torch.device,
        )

        self.assertEqual(resolve_torch_device(fake_torch, "auto").type, "cuda")

    def test_resolve_torch_device_rejects_unavailable_cuda(self):
        from model.step_GRU.stream_worker import resolve_torch_device

        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: False),
            device=torch.device,
        )

        with self.assertRaisesRegex(RuntimeError, "CUDA device was requested"):
            resolve_torch_device(fake_torch, "cuda")

    def test_load_model_infers_sizes_from_state_dict(self):
        from model.step_GRU.inference import FlowClassifier, load_model
        from model.step_GRU.models import DynamicPacketGRU

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "weights.pt"
            source_model = DynamicPacketGRU(input_size=18, hidden_size=8)
            torch.save(source_model.state_dict(), path)

            loaded = load_model(path, device=torch.device("cpu"))

            self.assertEqual(loaded.input_size, 18)
            self.assertEqual(loaded.hidden_size, 8)

            classifier = FlowClassifier(path, device=torch.device("cpu"), threshold=0.5)
            result = classifier.classify(
                x=[[0.0] * 18, [1.0] * 18, [2.0] * 18],
                direction="dst_to_src",
                seq_len=3,
            )

            self.assertIn(result["predicted_label"], ("elephant", "mice"))
            self.assertGreaterEqual(result["exit_step"], 1)

    def test_forward_accepts_seq_len_and_early_exit(self):
        from model.step_GRU.models import DynamicPacketGRU

        model = DynamicPacketGRU(input_size=18, hidden_size=8)
        x = torch.randn(2, 4, 18)
        direction = torch.tensor([0, 1], dtype=torch.long)
        seq_len = torch.tensor([2, 4], dtype=torch.long)

        outputs = model(x, direction, seq_len=seq_len)

        self.assertEqual(tuple(outputs.shape), (2, 4, 1))

        score, exit_step = model(
            x[:1],
            direction[:1],
            seq_len=seq_len[:1],
            enable_early_exit=True,
            tolerance=1.0,
        )

        self.assertIsInstance(score, float)
        self.assertGreaterEqual(exit_step, 1)
        self.assertLessEqual(exit_step, 2)


if __name__ == "__main__":
    unittest.main()
