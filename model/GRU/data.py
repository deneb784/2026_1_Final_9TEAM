import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import torch
from torch.utils.data import Dataset


DIRECTION_TO_INDEX = {
    # 방향 문자열을 embedding/조건 입력에 넣기 위한 정수 index로 바꾼다.
    "src_to_dst": 0,
    "dst_to_src": 1,
    "bidirectional": 0,
}


@dataclass
class FeatureScaler:
    """feature별 min/max를 저장하고 min-max 정규화를 수행한다."""

    x_min: list[float]
    x_max: list[float]

    @property
    def ranges(self) -> list[float]:
        # max == min인 feature는 0으로 나누지 않도록 range를 1.0으로 둔다.
        return [
            (hi - lo) if hi != lo else 1.0
            for lo, hi in zip(self.x_min, self.x_max)
        ]

    def transform(self, x: list[list[float]]) -> list[list[float]]:
        """패킷 시퀀스 x 전체를 feature별 min-max 값으로 정규화한다."""
        ranges = self.ranges
        return [
            [
                (float(value) - self.x_min[index]) / ranges[index]
                for index, value in enumerate(row)
            ]
            for row in x
        ]

    def save(self, path: str | Path) -> None:
        """학습 때 사용한 정규화 기준을 JSON으로 저장한다."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"x_min": self.x_min, "x_max": self.x_max}, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "FeatureScaler":
        """저장된 scaler JSON을 다시 읽어 같은 정규화 기준을 재사용한다."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(x_min=payload["x_min"], x_max=payload["x_max"])


def load_jsonl(paths: str | Path | Iterable[str | Path]) -> list[dict]:
    """하나 이상의 JSONL dataset 파일을 sample dict 목록으로 읽는다."""
    if isinstance(paths, (str, Path)):
        paths = [paths]

    samples: list[dict] = []
    for path in paths:
        with Path(path).open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    samples.append(json.loads(line))
    return samples


def fit_feature_scaler(samples: list[dict], input_size: int = 18) -> FeatureScaler:
    """학습 sample에서 feature별 min/max를 추정한다."""
    x_min = [float("inf")] * input_size
    x_max = [float("-inf")] * input_size
    seen = False

    for sample in samples:
        # padding으로 반복된 값까지 scaler에 반영하지 않도록 실제 seq_len까지만 본다.
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
    """flow size 순위를 0~1 CDF target으로 변환한다."""
    sizes = [float(sample[size_field]) for sample in samples]
    if len(sizes) <= 1:
        return [0.0 for _ in sizes]

    indexed_sizes = sorted((size, index) for index, size in enumerate(sizes))
    cdfs = [0.0] * len(sizes)

    if method == "ordinal":
        # 같은 크기 tie를 따로 처리하지 않고 정렬 순서 그대로 rank를 부여한다.
        for rank, (_, index) in enumerate(indexed_sizes):
            cdfs[index] = rank / (len(sizes) - 1)
        return cdfs

    if method not in {"average", "min", "max"}:
        raise ValueError(f"unsupported cdf method: {method}")

    start = 0
    while start < len(indexed_sizes):
        # 같은 size를 가진 sample들은 같은 CDF 값을 받도록 tie 구간을 찾는다.
        end = start
        while end + 1 < len(indexed_sizes) and indexed_sizes[end + 1][0] == indexed_sizes[start][0]:
            end += 1

        if method == "min":
            # tie 구간의 가장 낮은 rank를 사용한다.
            rank = start
        elif method == "max":
            # tie 구간의 가장 높은 rank를 사용한다.
            rank = end
        else:
            # 기본값은 tie 구간의 평균 rank다.
            rank = (start + end) / 2

        cdf = rank / (len(sizes) - 1)
        for position in range(start, end + 1):
            _, original_index = indexed_sizes[position]
            cdfs[original_index] = cdf

        start = end + 1

    return cdfs


class FlowJsonlDataset(Dataset):
    """JSONL flow sample을 PyTorch Dataset 형태로 감싼다."""

    def __init__(
        self,
        paths: str | Path | Iterable[str | Path],
        scaler: FeatureScaler | None = None,
        target: str = "label",
        cdf_method: str = "average",
        size_field: str = "flow_size_bytes",
    ):
        # samples는 원본 JSON dict를 유지하고, targets만 학습 목적에 맞게 따로 만든다.
        self.samples = load_jsonl(paths)
        self.scaler = scaler
        self.target = target
        self.size_field = size_field

        if target == "cdf":
            # 회귀처럼 CDF 값을 직접 예측하도록 학습할 때 사용한다.
            self.targets = compute_cdf_targets(self.samples, size_field=size_field, method=cdf_method)
        elif target == "label":
            # 기존 elephant/mice binary label을 target으로 사용한다.
            self.targets = [float(sample["label"]) for sample in self.samples]
        else:
            raise ValueError(f"unsupported target: {target}")

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> dict:
        """sample 하나를 tensor dict로 변환한다."""
        sample = self.samples[index]
        x = sample["x"]
        if self.scaler is not None:
            # 모델 입력 직전에 정규화를 적용한다.
            x = self.scaler.transform(x)

        direction = sample["flow_key"]["direction"]
        return {
            # x shape: [seq_len 또는 padded_len, feature_size]
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
    """가변 길이 flow 시퀀스를 batch 안에서 가장 긴 길이에 맞춰 padding한다."""
    seq_lens = torch.stack([item["seq_len"] for item in batch])
    max_len = max(item["x"].size(0) for item in batch)
    feature_size = batch[0]["x"].size(1)
    x = torch.zeros((len(batch), max_len, feature_size), dtype=torch.float32)
    for index, item in enumerate(batch):
        # 각 sample의 실제 길이만 앞쪽에 복사하고 나머지는 0 padding으로 둔다.
        length = item["x"].size(0)
        x[index, :length] = item["x"]

    return {
        "x": x,
        "direction": torch.stack([item["direction"] for item in batch]),
        "seq_len": seq_lens,
        "label": torch.stack([item["label"] for item in batch]),
        "target": torch.stack([item["target"] for item in batch]).unsqueeze(-1),
        "flow_size_bytes": torch.stack([item["flow_size_bytes"] for item in batch]),
        "run_id": [item["run_id"] for item in batch],
        "flow_key": [item["flow_key"] for item in batch],
        "trace_key": [item["trace_key"] for item in batch],
    }
