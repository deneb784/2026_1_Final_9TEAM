#!/usr/bin/env python3
from __future__ import annotations

"""
Summarize end-to-end online classification latency logs.

Input is the JSONL written by xdp/tg_xdp_capture.py --classification-log. Each
row is emitted only after a Pub/Sub result is received and applied to FlowCache,
so the row itself is evidence that the online loop completed for that flow.
"""

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


LATENCY_FIELDS = [
    "ready_to_cache_updated_ms",
    "ready_to_worker_received_ms",
    "worker_received_to_done_ms",
    "worker_done_to_publish_ms",
    "pubsub_publish_to_subscriber_ms",
    "subscriber_to_cache_updated_ms",
    "cache_apply_duration_ms",
    "inference_ms",
]


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def summarize(values: list[float]) -> dict[str, float | int]:
    clean = [value for value in values if value is not None]
    if not clean:
        return {
            "count": 0,
            "min": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
            "mean": 0.0,
        }
    return {
        "count": len(clean),
        "min": min(clean),
        "p50": percentile(clean, 0.50),
        "p95": percentile(clean, 0.95),
        "p99": percentile(clean, 0.99),
        "max": max(clean),
        "mean": statistics.fmean(clean),
    }


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


def filter_rows(rows: list[dict[str, Any]], run_id: str | None) -> list[dict[str, Any]]:
    if run_id is None:
        return rows
    return [row for row in rows if row.get("run_id") == run_id]


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "classified_rows": len(rows),
        "unique_logical_flows": len(
            {
                row.get("logical_flow_id")
                for row in rows
                if row.get("logical_flow_id") is not None
            }
        ),
    }
    for field in LATENCY_FIELDS:
        summary[field] = summarize(
            [
                float(row[field])
                for row in rows
                if row.get(field) is not None
            ]
        )
    return summary


def print_summary(summary: dict[str, Any]) -> None:
    print(
        "classified_rows=%d unique_logical_flows=%d"
        % (summary["classified_rows"], summary["unique_logical_flows"])
    )
    for field in LATENCY_FIELDS:
        item = summary[field]
        print(
            "%-32s count=%6d min=%9.3f p50=%9.3f p95=%9.3f p99=%9.3f max=%9.3f mean=%9.3f ms"
            % (
                field,
                item["count"],
                item["min"],
                item["p50"],
                item["p95"],
                item["p99"],
                item["max"],
                item["mean"],
            )
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FlowCache classification JSONL에서 end-to-end latency를 요약한다."
    )
    parser.add_argument("--classification-log", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json-out", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = filter_rows(read_jsonl(args.classification_log), args.run_id)
    summary = build_summary(rows)
    print_summary(summary)

    if args.json_out is not None:
        output_path = Path(args.json_out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("json_out=%s" % args.json_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
