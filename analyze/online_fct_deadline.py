#!/usr/bin/env python3
from __future__ import annotations

"""
온라인 분류 결과가 FCT 전에 FlowCache에 반영됐는지 검증한다.

TrafficGenerator metadata 파일에는 request별 완료 시각이 들어 있다.
`flows_*_meta.csv` -> `stop_time_us`.

XDP 온라인 latency 로그에는 classifier 결과가 FlowCache에 반영된 시각이 들어 있다.
`online_classification_latency.jsonl` -> `cache_updated_wall_ns`.

검증 기준:
`cache_updated_wall_ns / 1000 <= stop_time_us`이면 모델 결과가 FCT 전에
FlowCache에 반영된 것으로 본다.
"""

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import Any

try:
    # numpy가 있으면 검증된 np.percentile을 사용한다.
    # 없는 환경에서도 스크립트가 실행되도록 아래 percentile()에 fallback을 둔다.
    import numpy as np
except ImportError:
    np = None


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
    """margin/late_by 같은 값들의 count/min/p50/p95/p99/max/mean을 만든다."""
    if not values:
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
        "count": len(values),
        "min": min(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """online_classification_latency.jsonl을 읽어 dict row 목록으로 반환한다."""
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


def flow_join_key_from_meta(row: dict[str, str]) -> tuple[str, int, str, int, int]:
    """flows_*_meta.csv row에서 온라인 결과와 join할 key를 만든다."""
    # TrafficGenerator의 request는 src/dst 5-tuple과 flow_id 조합으로 식별한다.
    return (
        row["src_ip"],
        int(row["src_port"]),
        row["dst_ip"],
        int(row["dst_port"]),
        int(row["flow_id"]),
    )


def flow_join_key_from_result(row: dict[str, Any]) -> tuple[str, int, str, int, int] | None:
    """classification result row에서 flow metadata와 join할 key를 만든다."""
    key = row.get("online_flow_key") or {}

    # 현재 online_flow_key는 패킷 방향 그대로 src/dst를 담는다.
    if all(key.get(field) is not None for field in ("src_ip", "src_port", "dst_ip", "dst_port", "flow_id")):
        src_ip = str(key["src_ip"])
        src_port = int(key["src_port"])
        dst_ip = str(key["dst_ip"])
        dst_port = int(key["dst_port"])
        flow_id = int(key["flow_id"])
        direction = key.get("direction")
        if direction == "dst_to_src":
            # flow metadata는 TrafficGenerator request 기준(src client -> dst server)이므로
            # 응답 방향 결과는 src/dst를 뒤집어 metadata key와 맞춘다.
            return (dst_ip, dst_port, src_ip, src_port, flow_id)
        return (src_ip, src_port, dst_ip, dst_port, flow_id)

    # 예전 로그 호환: client/server로 정규화된 key를 읽는다.
    required = ("client_ip", "client_port", "server_ip", "server_port", "flow_id")
    if any(key.get(field) is None for field in required):
        return None
    return (
        str(key["client_ip"]),
        int(key["client_port"]),
        str(key["server_ip"]),
        int(key["server_port"]),
        int(key["flow_id"]),
    )


def read_flow_metadata(paths: list[str]) -> dict[tuple[str, int, str, int, int], dict[str, Any]]:
    """하나 이상의 flows_*_meta.csv를 읽어 join key -> metadata dict로 만든다."""
    metas: dict[tuple[str, int, str, int, int], dict[str, Any]] = {}
    for pattern in paths:
        # shell이 glob을 확장하지 않도록 README에서는 따옴표로 감싼다.
        # 여기서 Python이 직접 glob을 확장한다.
        matched_paths = sorted(Path().glob(pattern))
        if not matched_paths:
            raise FileNotFoundError(f"no flow metadata files matched: {pattern}")
        for path in matched_paths:
            with path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = flow_join_key_from_meta(row)
                    # 분석에 필요한 값만 int/str로 정규화해서 저장한다.
                    metas[key] = {
                        "meta_file": str(path),
                        "src_index": int(row["src_index"]),
                        "flow_id": int(row["flow_id"]),
                        "src_ip": row["src_ip"],
                        "src_port": int(row["src_port"]),
                        "dst_ip": row["dst_ip"],
                        "dst_port": int(row["dst_port"]),
                        "size_bytes": int(row["size_bytes"]),
                        "start_time_us": int(row["start_time_us"]),
                        "stop_time_us": int(row["stop_time_us"]),
                        "fct_us": int(row["fct_us"]),
                    }
    return metas


def build_rows(
    metas: dict[tuple[str, int, str, int, int], dict[str, Any]],
    result_rows: list[dict[str, Any]],
    run_id: str | None,
) -> tuple[list[dict[str, Any]], int]:
    """classification 결과와 flow metadata를 join해 flow별 FCT-before row를 만든다."""
    matched = []
    unmatched_results = 0
    best_by_key: dict[tuple[str, int, str, int, int], dict[str, Any]] = {}

    for result in result_rows:
        # 같은 classification log에 여러 run이 append되어 있을 수 있으므로 run_id로 거른다.
        if run_id is not None and result.get("run_id") != run_id:
            continue
        key = flow_join_key_from_result(result)
        if key is None or key not in metas:
            # run_id는 맞지만 flow metadata와 매칭되지 않는 결과 수를 따로 센다.
            # 이 값이 크면 서로 다른 실험의 로그/metadata가 섞였을 가능성이 있다.
            unmatched_results += 1
            continue
        previous = best_by_key.get(key)
        # 같은 flow 결과가 여러 번 있으면 가장 먼저 cache에 반영된 결과를 사용한다.
        if previous is None or int(result["cache_updated_wall_ns"]) < int(
            previous["cache_updated_wall_ns"]
        ):
            best_by_key[key] = result

    for key, result in best_by_key.items():
        meta = metas[key]
        # classification log는 ns 단위 wall-clock이고, flow metadata는 us 단위다.
        # 비교를 위해 cache update 시각을 us로 변환한다.
        cache_updated_us = int(result["cache_updated_wall_ns"]) / 1000.0
        stop_time_us = int(meta["stop_time_us"])
        start_time_us = int(meta["start_time_us"])
        # margin이 양수이면 FCT 전에 cache update가 끝난 것이고,
        # 음수이면 abs(margin)만큼 FCT 이후에 늦게 반영된 것이다.
        margin_us = stop_time_us - cache_updated_us
        ready_us = int((result.get("producer_metrics") or {})["feature_ready_wall_ns"]) / 1000.0
        matched.append(
            {
                **meta,
                "logical_flow_id": result.get("logical_flow_id"),
                "predicted_label": result.get("predicted_label"),
                "score": result.get("score"),
                "feature_ready_us": ready_us,
                "cache_updated_us": cache_updated_us,
                "ready_offset_from_start_us": ready_us - start_time_us,
                "cache_update_offset_from_start_us": cache_updated_us - start_time_us,
                "cache_update_margin_us": margin_us,
                "cache_update_margin_ms": margin_us / 1000.0,
                "cache_before_fct": margin_us >= 0,
                "ready_to_cache_updated_ms": result.get("ready_to_cache_updated_ms"),
            }
        )

    return matched, unmatched_results


def build_summary(rows: list[dict[str, Any]], total_meta_count: int, unmatched_results: int) -> dict[str, Any]:
    """FCT-before 성공률, classification coverage, margin 요약을 만든다."""
    success_rows = [row for row in rows if row["cache_before_fct"]]
    missed_rows = [row for row in rows if not row["cache_before_fct"]]
    margins_ms = [float(row["cache_update_margin_ms"]) for row in rows]
    late_by_ms = [-float(row["cache_update_margin_ms"]) for row in missed_rows]
    return {
        "flow_meta_rows": total_meta_count,
        "classified_unique_flows": len(rows),
        "unmatched_classification_rows": unmatched_results,
        "cache_before_fct_count": len(success_rows),
        "cache_before_fct_rate": (len(success_rows) / len(rows)) if rows else 0.0,
        "missed_fct_count": len(missed_rows),
        # 전체 flow metadata 중 온라인 분류 결과와 매칭된 flow 비율이다.
        # feature packet count를 만족하지 못한 flow는 여기에 포함되지 않을 수 있다.
        "classified_coverage_rate": (len(rows) / total_meta_count) if total_meta_count else 0.0,
        "cache_update_margin_ms": summarize(margins_ms),
        # FCT를 놓친 flow에 대해서만 얼마나 늦었는지 양수 ms로 요약한다.
        "late_by_ms": summarize(late_by_ms),
    }


def print_summary(summary: dict[str, Any]) -> None:
    """터미널에서 바로 비교하기 쉬운 형식으로 결과를 출력한다."""
    print("flow_meta_rows=%d" % summary["flow_meta_rows"])
    print("classified_unique_flows=%d" % summary["classified_unique_flows"])
    print("unmatched_classification_rows=%d" % summary["unmatched_classification_rows"])
    print(
        "cache_before_fct=%d/%d (%.2f%%)"
        % (
            summary["cache_before_fct_count"],
            summary["classified_unique_flows"],
            summary["cache_before_fct_rate"] * 100.0,
        )
    )
    print(
        "classified_coverage=%d/%d (%.2f%%)"
        % (
            summary["classified_unique_flows"],
            summary["flow_meta_rows"],
            summary["classified_coverage_rate"] * 100.0,
        )
    )
    for name in ("cache_update_margin_ms", "late_by_ms"):
        item = summary[name]
        print(
            "%-28s count=%6d min=%9.3f p50=%9.3f p95=%9.3f p99=%9.3f max=%9.3f mean=%9.3f ms"
            % (
                name,
                item["count"],
                item["min"],
                item["p50"],
                item["p95"],
                item["p99"],
                item["max"],
                item["mean"],
            )
        )


def write_csv(path: str | Path, rows: list[dict[str, Any]]) -> None:
    """flow별 FCT-before 판정 결과를 CSV로 저장한다."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "src_index",
        "flow_id",
        "src_ip",
        "src_port",
        "dst_ip",
        "dst_port",
        "size_bytes",
        "start_time_us",
        "stop_time_us",
        "fct_us",
        "feature_ready_us",
        "cache_updated_us",
        "ready_offset_from_start_us",
        "cache_update_offset_from_start_us",
        "cache_update_margin_us",
        "cache_update_margin_ms",
        "cache_before_fct",
        "ready_to_cache_updated_ms",
        "predicted_label",
        "score",
        "logical_flow_id",
        "meta_file",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="온라인 분류 결과가 FCT 전에 FlowCache에 반영됐는지 분석한다."
    )
    parser.add_argument("--classification-log", required=True)
    parser.add_argument(
        "--flow-meta",
        nargs="+",
        required=True,
        help="flows_*_meta.csv path or glob. Quote globs so the script expands them.",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--csv-out", default=None)
    parser.add_argument("--json-out", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    # 1) flow metadata 읽기
    # 2) classification JSONL 읽기
    # 3) flow별 join 및 FCT-before 판정
    # 4) 요약 출력 및 선택적 CSV/JSON 저장
    metas = read_flow_metadata(args.flow_meta)
    rows, unmatched_results = build_rows(
        metas,
        read_jsonl(args.classification_log),
        args.run_id,
    )
    rows.sort(key=lambda row: (row["src_index"], row["flow_id"]))
    summary = build_summary(rows, len(metas), unmatched_results)
    print_summary(summary)

    if args.csv_out is not None:
        write_csv(args.csv_out, rows)
        print("csv_out=%s" % args.csv_out)

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
