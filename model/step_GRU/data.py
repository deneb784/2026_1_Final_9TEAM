from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


DIRECTION_TO_INDEX = {
    "src_to_dst": 0,
    "dst_to_src": 1,
    "bidirectional": 0,
}


@dataclass
class FeatureScaler:
    """Feature-wise min/max scaler saved by offline notebooks when needed."""

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
