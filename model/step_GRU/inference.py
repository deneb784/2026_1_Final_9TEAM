from __future__ import annotations

from pathlib import Path
from typing import Any

import torch

from .data import DIRECTION_TO_INDEX, FeatureScaler
from .models import DynamicPacketGRU, get_flow_stats


def _torch_load_checkpoint(model_path: str | Path, device: torch.device) -> Any:
    try:
        return torch.load(model_path, map_location=device, weights_only=True)
    except TypeError:
        return torch.load(model_path, map_location=device)


def _state_dict_from_checkpoint(checkpoint: Any) -> dict[str, torch.Tensor]:
    if isinstance(checkpoint, dict) and "model_state_dict" in checkpoint:
        return checkpoint["model_state_dict"]
    if isinstance(checkpoint, dict) and "state_dict" in checkpoint:
        return checkpoint["state_dict"]
    return checkpoint


def _infer_model_sizes(state: dict[str, torch.Tensor]) -> tuple[int | None, int | None]:
    input_size = None
    hidden_size = None

    layer_norm_weight = state.get("layer_norm.weight")
    if layer_norm_weight is not None:
        input_size = int(layer_norm_weight.shape[0])

    gru_weight = state.get("gru_cell.weight_ih")
    if gru_weight is not None:
        input_size = int(gru_weight.shape[1])
        hidden_size = int(gru_weight.shape[0] // 3)

    classifier_weight = state.get("classifier.weight")
    if classifier_weight is not None:
        hidden_size = int(classifier_weight.shape[1])

    return input_size, hidden_size


def load_model(
    model_path: str | Path,
    device: torch.device,
    input_size: int | None = None,
    hidden_size: int | None = None,
    steepness: float = 3.0,
) -> DynamicPacketGRU:
    """Load a DynamicPacketGRU from a state_dict or checkpoint dict."""
    checkpoint = _torch_load_checkpoint(model_path, device)
    state = _state_dict_from_checkpoint(checkpoint)
    inferred_input_size, inferred_hidden_size = _infer_model_sizes(state)

    if input_size is None:
        input_size = inferred_input_size or 18
    elif inferred_input_size is not None and input_size != inferred_input_size:
        raise ValueError(
            f"checkpoint expects input_size={inferred_input_size}, got input_size={input_size}"
        )

    if hidden_size is None:
        hidden_size = inferred_hidden_size or 64
    elif inferred_hidden_size is not None and hidden_size != inferred_hidden_size:
        raise ValueError(
            f"checkpoint expects hidden_size={inferred_hidden_size}, got hidden_size={hidden_size}"
        )

    model = DynamicPacketGRU(
        input_size=input_size,
        hidden_size=hidden_size,
        steepness=steepness,
    ).to(device)
    model.load_state_dict(state)
    model.eval()
    return model


class FlowClassifier:
    """Small runtime wrapper around model weights, optional scaler, and thresholding."""

    def __init__(
        self,
        model_path: str | Path,
        device: torch.device,
        scaler_path: str | Path | None = None,
        input_size: int | None = None,
        hidden_size: int | None = None,
        steepness: float = 3.0,
        threshold: float = 0.5,
        threshold_dataset_path: str | Path | None = None,
        threshold_size: int = 100000,
        tolerance: float = 0.05,
    ):
        self.device = device
        self.threshold = self._resolve_threshold(
            threshold,
            threshold_dataset_path,
            threshold_size,
        )
        self.threshold_source = "dataset_cdf" if threshold_dataset_path else "explicit"
        self.threshold_size = threshold_size
        self.tolerance = tolerance
        self.model = load_model(
            model_path=model_path,
            device=device,
            input_size=input_size,
            hidden_size=hidden_size,
            steepness=steepness,
        )
        self.scaler = FeatureScaler.load(scaler_path) if scaler_path else None
        self.input_size = int(self.model.input_size)

        if self.scaler and (
            len(self.scaler.x_min) != self.input_size or len(self.scaler.x_max) != self.input_size
        ):
            raise ValueError(
                "scaler feature count does not match model: "
                f"scaler={len(self.scaler.x_min)}, model_input_size={self.input_size}"
            )

    @staticmethod
    def _resolve_threshold(
        fallback_threshold: float,
        threshold_dataset_path: str | Path | None,
        threshold_size: int,
    ) -> float:
        if threshold_dataset_path is None:
            return float(fallback_threshold)

        threshold, _, _ = get_flow_stats(threshold_dataset_path, threshold_size)
        if threshold is None:
            raise ValueError(f"could not compute threshold from {threshold_dataset_path}")
        return float(threshold)

    def classify(
        self,
        x: list[list[float]],
        direction: str,
        seq_len: int | None = None,
    ) -> dict[str, float | int | str]:
        if not x:
            raise ValueError("x must contain at least one feature row")
        if any(len(row) != self.input_size for row in x):
            raise ValueError(f"model expects {self.input_size} features per row")

        model_input = self.scaler.transform(x) if self.scaler else x
        seq_len = int(seq_len or len(model_input))
        direction_idx = DIRECTION_TO_INDEX[direction]

        x_tensor = torch.tensor(model_input, dtype=torch.float32).unsqueeze(0).to(self.device)
        direction_tensor = torch.tensor([direction_idx], dtype=torch.long).to(self.device)

        with torch.no_grad():
            score, exit_step = self.model(
                x_tensor,
                direction_tensor,
                enable_early_exit=True,
                tolerance=self.tolerance,
            )

        label = "elephant" if float(score) >= self.threshold else "mice"
        return {
            "score": float(score),
            "predicted_label": label,
            "threshold": self.threshold,
            "threshold_source": self.threshold_source,
            "exit_step": int(exit_step),
        }
