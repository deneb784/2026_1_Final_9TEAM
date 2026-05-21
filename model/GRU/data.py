import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import torch
from torch.utils.data import Dataset


DIRECTION_TO_INDEX = {
    "src_to_dst": 0,
    "dst_to_src": 1,
}


@dataclass
class FeatureScaler:
    x_min: list[float]
    x_max: list[float]

    @property
    def ranges(self) -> list[float]:
        return [
            (hi - lo) if hi != lo else 1.0
            for lo, hi in zip(self.x_min, self.x_max)
        ]

    def transform(self, x: list[list[float]]) -> list[list[float]]:
        ranges = self.ranges
        return [
            [
                (float(value) - self.x_min[index]) / ranges[index]
                for index, value in enumerate(row)
            ]
            for row in x
        ]

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"x_min": self.x_min, "x_max": self.x_max}, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "FeatureScaler":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(x_min=payload["x_min"], x_max=payload["x_max"])


def load_jsonl(paths: str | Path | Iterable[str | Path]) -> list[dict]:
    if isinstance(paths, (str, Path)):
        paths = [paths]

    samples: list[dict] = []
    for path in paths:
        with Path(path).open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    samples.append(json.loads(line))
    return samples


def fit_feature_scaler(samples: list[dict], input_size: int = 11) -> FeatureScaler:
    x_min = [float("inf")] * input_size
    x_max = [float("-inf")] * input_size
    seen = False

    for sample in samples:
        seq_len = int(sample.get("seq_len", len(sample["x"])))
        for row in sample["x"][:seq_len]:
            seen = True
            for index, value in enumerate(row[:input_size]):
                value = float(value)
                x_min[index] = min(x_min[index], value)
                x_max[index] = max(x_max[index], value)

    if not seen:
        raise ValueError("cannot fit scaler from an empty dataset")

    return FeatureScaler(x_min=x_min, x_max=x_max)


def compute_cdf_targets(
    samples: list[dict],
    size_field: str = "flow_size_bytes",
    method: str = "average",
) -> list[float]:
    sizes = [float(sample[size_field]) for sample in samples]
    if len(sizes) <= 1:
        return [0.0 for _ in sizes]

    indexed_sizes = sorted((size, index) for index, size in enumerate(sizes))
    cdfs = [0.0] * len(sizes)

    if method == "ordinal":
        for rank, (_, index) in enumerate(indexed_sizes):
            cdfs[index] = rank / (len(sizes) - 1)
        return cdfs

    if method not in {"average", "min", "max"}:
        raise ValueError(f"unsupported cdf method: {method}")

    start = 0
    while start < len(indexed_sizes):
        end = start
        while end + 1 < len(indexed_sizes) and indexed_sizes[end + 1][0] == indexed_sizes[start][0]:
            end += 1

        if method == "min":
            rank = start
        elif method == "max":
            rank = end
        else:
            rank = (start + end) / 2

        cdf = rank / (len(sizes) - 1)
        for position in range(start, end + 1):
            _, original_index = indexed_sizes[position]
            cdfs[original_index] = cdf

        start = end + 1

    return cdfs


class FlowJsonlDataset(Dataset):
    def __init__(
        self,
        paths: str | Path | Iterable[str | Path],
        scaler: FeatureScaler | None = None,
        target: str = "label",
        cdf_method: str = "average",
        size_field: str = "flow_size_bytes",
    ):
        self.samples = load_jsonl(paths)
        self.scaler = scaler
        self.target = target
        self.size_field = size_field

        if target == "cdf":
            self.targets = compute_cdf_targets(self.samples, size_field=size_field, method=cdf_method)
        elif target == "label":
            self.targets = [float(sample["label"]) for sample in self.samples]
        else:
            raise ValueError(f"unsupported target: {target}")

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> dict:
        sample = self.samples[index]
        x = sample["x"]
        if self.scaler is not None:
            x = self.scaler.transform(x)

        direction = sample["flow_key"]["direction"]
        return {
            "x": torch.tensor(x, dtype=torch.float32),
            "direction": torch.tensor(DIRECTION_TO_INDEX[direction], dtype=torch.long),
            "seq_len": torch.tensor(int(sample.get("seq_len", len(sample["x"]))), dtype=torch.long),
            "label": torch.tensor(float(sample["label"]), dtype=torch.float32),
            "target": torch.tensor(float(self.targets[index]), dtype=torch.float32),
            "flow_size_bytes": torch.tensor(float(sample[self.size_field]), dtype=torch.float32),
            "run_id": sample.get("run_id"),
            "flow_key": sample.get("flow_key"),
            "trace_key": sample.get("trace_key"),
        }


def collate_flow_batch(batch: list[dict]) -> dict:
    return {
        "x": torch.stack([item["x"] for item in batch]),
        "direction": torch.stack([item["direction"] for item in batch]),
        "seq_len": torch.stack([item["seq_len"] for item in batch]),
        "label": torch.stack([item["label"] for item in batch]),
        "target": torch.stack([item["target"] for item in batch]).unsqueeze(-1),
        "flow_size_bytes": torch.stack([item["flow_size_bytes"] for item in batch]),
        "run_id": [item["run_id"] for item in batch],
        "flow_key": [item["flow_key"] for item in batch],
        "trace_key": [item["trace_key"] for item in batch],
    }
