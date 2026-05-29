from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATASET_ROOT = Path("dataset")
DEFAULT_DIRECTION_DATASET = "elephant_dst_to_src"
DATASET_TYPES = ("dctcp", "fb", "vl2", "univ1")
SEQUENCE_LENGTHS = (3, 5, 10)
SPLITS = ("dataset", "dataset_with_cdf", "train", "train_balanced", "test")


@dataclass(frozen=True)
class StepGruDatasetConfig:
    dataset_type: str = "fb"
    seq_len: int = 10
    direction_dataset: str = DEFAULT_DIRECTION_DATASET
    root: Path = DEFAULT_DATASET_ROOT

    @property
    def directory(self) -> Path:
        return self.root / self.direction_dataset / self.dataset_type / f"seq{self.seq_len}"

    @property
    def weights_path(self) -> Path:
        return self.directory / "weights.pt"

    def jsonl_path(self, split: str = "test") -> Path:
        if split not in SPLITS:
            raise ValueError(f"unsupported dataset split: {split}")
        return self.directory / f"{split}.jsonl"


def resolve_dataset_config(
    dataset_type: str = "fb",
    seq_len: int = 10,
    direction_dataset: str = DEFAULT_DIRECTION_DATASET,
    root: str | Path = DEFAULT_DATASET_ROOT,
) -> StepGruDatasetConfig:
    if dataset_type not in DATASET_TYPES:
        raise ValueError(f"unsupported dataset_type: {dataset_type}")
    if seq_len not in SEQUENCE_LENGTHS:
        raise ValueError(f"unsupported seq_len: {seq_len}")
    return StepGruDatasetConfig(
        dataset_type=dataset_type,
        seq_len=seq_len,
        direction_dataset=direction_dataset,
        root=Path(root),
    )


def require_existing_file(path: str | Path, description: str) -> Path:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{description} not found: {path}")
    return path
