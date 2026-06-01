#!/usr/bin/env python3
from __future__ import annotations

"""
온라인 분류 end-to-end latency 로그를 요약한다.

입력은 xdp/tg_xdp_capture.py의 --classification-log로 저장한 JSONL이다.
각 row는 worker의 Pub/Sub 결과를 subscriber가 받고 FlowCache에 반영한 뒤에
기록되므로, row가 존재한다는 것 자체가 해당 flow의 온라인 루프가 끝났다는 뜻이다.
"""

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

try:
    # 모델 환경에는 numpy가 있는 경우가 많으므로 있으면 np.percentile을 사용한다.
    # 단, 가벼운 시스템 Python에서도 분석 스크립트가 깨지지 않도록 fallback을 둔다.
    import numpy as np
except ImportError:
    np = None


# online_classification_latency.jsonl에 들어 있는 latency field 중 요약할 항목들.
# 각 field는 xdp/tg_xdp_capture.py의 add_e2e_latency_fields()와
# model/step_GRU/stream_worker.py에서 만들어진다.
LATENCY_FIELDS = [
    "ready_to_cache_updated_ms",
    "ready_to_request_built_ms",
    "request_built_to_worker_received_ms",
    "request_built_to_publish_enqueued_ms",
    "publish_enqueue_duration_ms",
    "publish_enqueued_to_publisher_dequeued_ms",
    "publisher_dequeued_to_worker_received_ms",
    "request_built_to_redis_publish_start_ms",
    "redis_publish_start_to_worker_received_ms",
    "stream_publish_to_worker_received_ms",
    "ready_to_worker_received_ms",
    "process_event_duration_ms",
    "worker_received_to_done_ms",
    "worker_done_to_publish_ms",
    "pubsub_publish_to_subscriber_ms",
    "subscriber_to_cache_updated_ms",
    "cache_apply_duration_ms",
    "inference_ms",
]


def percentile(values: list[float], q: float) -> float:
    """q 분위수 값을 계산한다. q=0.50이면 p50, q=0.95이면 p95다."""
    if np is not None:
        return float(np.percentile(values, q * 100))

    # numpy가 없는 환경에서는 정렬 후 선형 보간으로 percentile을 직접 계산한다.
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def summarize(values: list[float]) -> dict[str, float | int]:
    """한 latency field의 count/min/p50/p95/p99/max/mean을 만든다."""
    # JSONL row마다 모든 field가 있는 것은 아니므로 None은 요약에서 제외한다.
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
    """classification JSONL을 읽어 dict row 목록으로 반환한다."""
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
    """여러 실험이 같은 로그에 섞였을 때 run_id로 필요한 row만 고른다."""
    if run_id is None:
        return rows
    return [row for row in rows if row.get("run_id") == run_id]


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """전체 row 수와 latency field별 요약 통계를 만든다."""
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
        # field가 없는 row는 제외한다. 이전 버전 로그와도 함께 사용할 수 있게 하기 위함이다.
        summary[field] = summarize(
            [
                float(row[field])
                for row in rows
                if row.get(field) is not None
            ]
        )
    return summary


def print_summary(summary: dict[str, Any]) -> None:
    """터미널에서 바로 비교하기 쉬운 고정 폭 형식으로 출력한다."""
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
    # 1) JSONL 읽기
    # 2) run_id 필터링
    # 3) latency 요약
    # 4) 필요하면 JSON summary 저장
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
