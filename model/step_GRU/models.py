from __future__ import annotations

import json
import math
from pathlib import Path

import torch
import torch.nn as nn


class DynamicPacketGRU(nn.Module):
    """Packet sequence GRU used by the step_GRU notebooks and online worker."""

    def __init__(self, input_size: int = 18, hidden_size: int = 64, steepness: float = 3.0):
        super().__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.steepness = float(steepness)

        self.direction_embedding = nn.Embedding(num_embeddings=2, embedding_dim=hidden_size)
        self.layer_norm = nn.LayerNorm(input_size)
        self.gru_cell = nn.GRUCell(input_size=input_size, hidden_size=hidden_size)
        self.classifier = nn.Linear(hidden_size, 1)
        self.activation = nn.Sigmoid()

    def _normalize_lengths(
        self,
        seq_len: int | list[int] | torch.Tensor | None,
        batch_size: int,
        max_len: int,
        device: torch.device,
    ) -> torch.Tensor:
        if seq_len is None:
            lengths = torch.full((batch_size,), max_len, dtype=torch.long, device=device)
        elif isinstance(seq_len, int):
            lengths = torch.full((batch_size,), seq_len, dtype=torch.long, device=device)
        elif isinstance(seq_len, torch.Tensor):
            lengths = seq_len.to(device=device, dtype=torch.long).view(-1)
        else:
            lengths = torch.tensor(seq_len, dtype=torch.long, device=device).view(-1)

        if lengths.numel() == 1 and batch_size > 1:
            lengths = lengths.expand(batch_size)
        if lengths.numel() != batch_size:
            raise ValueError(f"seq_len must have {batch_size} values, got {lengths.numel()}")

        return lengths.clamp(min=1, max=max_len)

    def forward(
        self,
        x: torch.Tensor,
        direction_idx: torch.Tensor,
        seq_len: int | list[int] | torch.Tensor | None = None,
        enable_early_exit: bool = False,
        tolerance: float = 0.05,
        max_packets: int | None = None,
    ):
        """
        Args:
            x: packet feature sequence, shape ``[batch, steps, input_size]``.
            direction_idx: direction embedding index, shape ``[batch]``.
            seq_len: true sequence lengths before padding.
            enable_early_exit: return ``(score, exit_step)`` for batch_size=1.
            tolerance: stop when two adjacent scores differ by less than this value.
            max_packets: optional upper bound for observed steps.
        """
        batch_size, max_len, feature_size = x.size()
        if feature_size != self.input_size:
            raise ValueError(f"expected input_size={self.input_size}, got {feature_size}")
        if enable_early_exit and batch_size != 1:
            raise ValueError("early-exit inference supports batch_size=1")

        lengths = self._normalize_lengths(seq_len, batch_size, max_len, x.device)
        if max_packets is not None:
            max_packets_tensor = torch.full_like(lengths, max_packets)
            lengths = torch.minimum(lengths, max_packets_tensor).clamp(min=1)

        actual_steps = int(lengths[0].item()) if enable_early_exit else int(lengths.max().item())
        x = self.layer_norm(x)
        h_t = self.direction_embedding(direction_idx)
        all_outputs = []

        for step in range(actual_steps):
            next_h = self.gru_cell(x[:, step, :], h_t)
            active = (step < lengths).unsqueeze(-1)
            h_t = torch.where(active, next_h, h_t)

            raw_logit = self.classifier(h_t)
            pred = self.activation(self.steepness * raw_logit)
            all_outputs.append(pred)

            if enable_early_exit and step >= 1:
                current_prob = pred.item()
                previous_prob = all_outputs[-2].item()
                if abs(current_prob - previous_prob) < tolerance:
                    return current_prob, step + 1

        if enable_early_exit:
            return all_outputs[-1].item(), actual_steps

        return torch.stack(all_outputs, dim=1)


def get_flow_stats(
    filename: str | Path,
    target_flow_size: int = 100000,
) -> tuple[float | None, list[float] | None, list[float] | None]:
    """Return floored CDF threshold and feature mean/variance for a JSONL dataset."""
    total_flow_count = 0
    target_flow_count = 0
    total_packet_count = 0
    feature_sums: list[float] | None = None
    feature_sq_sums: list[float] | None = None

    try:
        with Path(filename).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue

                item = json.loads(line)
                if "flow_size_bytes" in item:
                    total_flow_count += 1
                    if item["flow_size_bytes"] <= target_flow_size:
                        target_flow_count += 1

                rows = item.get("x")
                if not rows:
                    continue
                row_width = len(rows[0])
                if row_width == 0 or any(len(row) != row_width for row in rows):
                    continue

                if feature_sums is None:
                    feature_sums = [0.0] * row_width
                    feature_sq_sums = [0.0] * row_width

                if len(feature_sums) != row_width or feature_sq_sums is None:
                    continue

                for row in rows:
                    total_packet_count += 1
                    for index, value in enumerate(row):
                        numeric_value = float(value)
                        feature_sums[index] += numeric_value
                        feature_sq_sums[index] += numeric_value * numeric_value
    except (FileNotFoundError, json.JSONDecodeError):
        return None, None, None

    if total_flow_count == 0:
        return None, None, None

    cdf_value = target_flow_count / total_flow_count
    floored_cdf = math.floor(cdf_value * 100) / 100.0
    if floored_cdf == 1:
        floored_cdf = 0.98

    if total_packet_count == 0 or feature_sums is None or feature_sq_sums is None:
        return floored_cdf, None, None

    feature_means = [value / total_packet_count for value in feature_sums]
    feature_vars = [
        max((sq_sum / total_packet_count) - (feature_means[index] ** 2), 0.0)
        for index, sq_sum in enumerate(feature_sq_sums)
    ]
    return floored_cdf, feature_means, feature_vars
