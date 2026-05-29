from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def confusion_counts(y_true: list[int], y_pred: list[int]) -> dict[str, int]:
    tp = sum(1 for true, pred in zip(y_true, y_pred) if true == 1 and pred == 1)
    tn = sum(1 for true, pred in zip(y_true, y_pred) if true == 0 and pred == 0)
    fp = sum(1 for true, pred in zip(y_true, y_pred) if true == 0 and pred == 1)
    fn = sum(1 for true, pred in zip(y_true, y_pred) if true == 1 and pred == 0)
    return {"tn": tn, "fp": fp, "fn": fn, "tp": tp}


def classification_metrics(
    y_true: list[int],
    scores: list[float],
    threshold: float = 0.5,
) -> dict[str, float | int]:
    y_pred = [1 if score >= threshold else 0 for score in scores]
    cm = confusion_counts(y_true, y_pred)
    total = len(y_true)
    accuracy = (cm["tp"] + cm["tn"]) / total if total else 0.0
    precision = cm["tp"] / (cm["tp"] + cm["fp"]) if (cm["tp"] + cm["fp"]) else 0.0
    recall = cm["tp"] / (cm["tp"] + cm["fn"]) if (cm["tp"] + cm["fn"]) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "threshold": threshold,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        **cm,
    }


def write_metrics_log(
    metrics: dict[str, Any],
    path: str | Path,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one offline train/test metrics row as JSONL."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "logged_at_wall_ns": time.time_ns(),
        **(extra or {}),
        **metrics,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
