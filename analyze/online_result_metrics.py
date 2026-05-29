#!/usr/bin/env python3
from __future__ import annotations

"""
Post-process step_GRU online result JSONL and compute classification metrics.

The online worker intentionally logs per-flow predictions only. This script joins
those predictions with a later truth source and computes aggregate metrics such
as recall, precision, and F1.
"""

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from model.step_GRU.metrics import classification_metrics, write_metrics_log
from pipeline.dataset.meta_loader import load_all_request_meta


FlowKey = tuple[int, int, str]


@dataclass
class PredictionRow:
    key: FlowKey
    run_id: str | None
    logical_flow_id: str | None
    score: float
    threshold: float | None
    predicted_label: str | None
    stream_id: str | None


@dataclass
class EvaluatedRow:
    prediction: PredictionRow
    true_label: int


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated quantile for size-based truth thresholds."""
    if not values:
        raise ValueError("cannot compute quantile from empty values")
    if q <= 0:
        return min(values)
    if q >= 1:
        return max(values)
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL row") from exc
    return rows


def key_from_mapping(mapping: dict[str, Any]) -> FlowKey:
    return (
        int(mapping["src_index"]),
        int(mapping["flow_id"]),
        str(mapping["direction"]),
    )


def load_predictions(path: str | Path, run_id: str | None = None) -> list[PredictionRow]:
    predictions = []
    for row in read_jsonl(path):
        if run_id is not None and row.get("run_id") != run_id:
            continue
        predictions.append(
            PredictionRow(
                key=key_from_mapping(row["request_key"]),
                run_id=row.get("run_id"),
                logical_flow_id=row.get("logical_flow_id"),
                score=float(row["score"]),
                threshold=float(row["threshold"]) if row.get("threshold") is not None else None,
                predicted_label=row.get("predicted_label"),
                stream_id=row.get("stream_id"),
            )
        )
    return predictions


def infer_prediction_threshold(
    predictions: list[PredictionRow],
    threshold_override: float | None,
) -> float:
    if threshold_override is not None:
        return threshold_override
    thresholds = [row.threshold for row in predictions if row.threshold is not None]
    if not thresholds:
        return 0.5
    first = thresholds[0]
    if any(value != first for value in thresholds):
        print(
            "[warn] multiple prediction thresholds found; using first threshold %.6f"
            % first,
            file=sys.stderr,
        )
    return first


def binary_label_from_value(value: Any, threshold: float) -> int:
    numeric = float(value)
    if numeric == 0.0:
        return 0
    if numeric == 1.0:
        return 1
    return 1 if numeric >= threshold else 0


def load_truth_from_jsonl(
    paths: list[str | Path],
    *,
    mode: str,
    label_field: str,
    label_threshold: float,
    size_field: str,
    size_threshold_bytes: float | None,
    size_quantile: float,
) -> tuple[dict[FlowKey, int], float | None]:
    rows = []
    for path in paths:
        rows.extend(read_jsonl(path))

    if mode == "size":
        sizes = [float(row[size_field]) for row in rows]
        threshold_bytes = (
            float(size_threshold_bytes)
            if size_threshold_bytes is not None
            else percentile(sizes, size_quantile)
        )
        return {
            key_from_mapping(row["flow_key"]): 1 if float(row[size_field]) >= threshold_bytes else 0
            for row in rows
        }, threshold_bytes

    return {
        key_from_mapping(row["flow_key"]): binary_label_from_value(
            row[label_field],
            label_threshold,
        )
        for row in rows
    }, None


def load_truth_from_meta_dir(
    meta_dir: str | Path,
    *,
    size_threshold_bytes: float | None,
    size_quantile: float,
) -> tuple[dict[FlowKey, int], float]:
    metas = load_all_request_meta(meta_dir)
    sizes = [float(meta.size_bytes) for meta in metas]
    threshold_bytes = (
        float(size_threshold_bytes)
        if size_threshold_bytes is not None
        else percentile(sizes, size_quantile)
    )

    truth = {}
    for meta in metas:
        label = 1 if float(meta.size_bytes) >= threshold_bytes else 0
        truth[(meta.src_index, meta.flow_id, "src_to_dst")] = label
        truth[(meta.src_index, meta.flow_id, "dst_to_src")] = label
    return truth, threshold_bytes


def evaluate_predictions(
    predictions: list[PredictionRow],
    truth: dict[FlowKey, int],
    prediction_threshold: float,
) -> tuple[dict[str, float | int], list[EvaluatedRow], list[PredictionRow]]:
    evaluated = []
    missing_truth = []
    for prediction in predictions:
        true_label = truth.get(prediction.key)
        if true_label is None:
            missing_truth.append(prediction)
            continue
        evaluated.append(EvaluatedRow(prediction=prediction, true_label=true_label))

    metrics = classification_metrics(
        [row.true_label for row in evaluated],
        [row.prediction.score for row in evaluated],
        threshold=prediction_threshold,
    )
    return metrics, evaluated, missing_truth


def write_eval_csv(path: str | Path, rows: list[EvaluatedRow]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "src_index",
                "flow_id",
                "direction",
                "logical_flow_id",
                "run_id",
                "stream_id",
                "score",
                "predicted_label",
                "true_label",
            ],
        )
        writer.writeheader()
        for row in rows:
            src_index, flow_id, direction = row.prediction.key
            writer.writerow(
                {
                    "src_index": src_index,
                    "flow_id": flow_id,
                    "direction": direction,
                    "logical_flow_id": row.prediction.logical_flow_id,
                    "run_id": row.prediction.run_id,
                    "stream_id": row.prediction.stream_id,
                    "score": row.prediction.score,
                    "predicted_label": row.prediction.predicted_label,
                    "true_label": row.true_label,
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="온라인 step_GRU result JSONL을 정답 소스와 join해 recall/f1 등을 계산한다."
    )
    parser.add_argument("--result-log", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--prediction-threshold", type=float, default=None)
    parser.add_argument(
        "--truth-jsonl",
        action="append",
        default=[],
        help="Dataset JSONL path. May be passed multiple times.",
    )
    parser.add_argument(
        "--meta-dir",
        default=None,
        help="Mininet results directory containing flows_*_meta.csv.",
    )
    parser.add_argument(
        "--truth-mode",
        choices=["label", "size"],
        default="label",
        help="For --truth-jsonl, use label field or size field to build y_true.",
    )
    parser.add_argument("--truth-label-field", default="label")
    parser.add_argument("--truth-size-field", default="directional_size_bytes")
    parser.add_argument("--label-threshold", type=float, default=None)
    parser.add_argument("--size-threshold-bytes", type=float, default=None)
    parser.add_argument(
        "--size-quantile",
        type=float,
        default=None,
        help="If size threshold bytes is omitted, derive it from this quantile.",
    )
    parser.add_argument("--metrics-out", default=None)
    parser.add_argument("--csv-out", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if bool(args.truth_jsonl) == bool(args.meta_dir):
        raise SystemExit("pass exactly one of --truth-jsonl or --meta-dir")

    predictions = load_predictions(args.result_log, run_id=args.run_id)
    if not predictions:
        raise SystemExit("no predictions matched the requested result log/run_id")

    prediction_threshold = infer_prediction_threshold(predictions, args.prediction_threshold)
    label_threshold = args.label_threshold
    if label_threshold is None:
        label_threshold = prediction_threshold
    size_quantile = args.size_quantile
    if size_quantile is None:
        size_quantile = label_threshold

    if args.truth_jsonl:
        truth, truth_size_threshold = load_truth_from_jsonl(
            args.truth_jsonl,
            mode=args.truth_mode,
            label_field=args.truth_label_field,
            label_threshold=label_threshold,
            size_field=args.truth_size_field,
            size_threshold_bytes=args.size_threshold_bytes,
            size_quantile=size_quantile,
        )
        truth_source = "jsonl"
    else:
        truth, truth_size_threshold = load_truth_from_meta_dir(
            args.meta_dir,
            size_threshold_bytes=args.size_threshold_bytes,
            size_quantile=size_quantile,
        )
        truth_source = "meta"

    metrics, evaluated, missing_truth = evaluate_predictions(
        predictions,
        truth,
        prediction_threshold,
    )
    extra = {
        "result_log": str(args.result_log),
        "run_id": args.run_id,
        "truth_source": truth_source,
        "truth_rows": len(truth),
        "prediction_rows": len(predictions),
        "matched_rows": len(evaluated),
        "missing_truth_rows": len(missing_truth),
        "label_threshold": label_threshold,
        "size_quantile": size_quantile,
    }
    if truth_size_threshold is not None:
        extra["size_threshold_bytes"] = truth_size_threshold

    print(json.dumps({**extra, **metrics}, ensure_ascii=False, indent=2))

    if args.metrics_out is not None:
        write_metrics_log(metrics, args.metrics_out, extra=extra)
        print("metrics_out=%s" % args.metrics_out)

    if args.csv_out is not None:
        write_eval_csv(args.csv_out, evaluated)
        print("csv_out=%s" % args.csv_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
