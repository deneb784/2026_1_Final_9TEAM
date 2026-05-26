#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))


EPOCH_US_MIN = 946_684_800_000_000  # 2000-01-01T00:00:00Z


@dataclass
class LatencySample:
    stream_id: str
    run_id: str | None
    capture_mode: str | None
    logical_flow_id: str | None
    direction: str | None
    ready_to_redis_id_ms: float | None
    publish_to_redis_id_ms: float | None
    ready_to_xadd_done_ms: float | None
    xadd_duration_ms: float | None
    first_packet_to_xadd_done_ms: float | None
    last_packet_to_xadd_done_ms: float | None
    first_packet_to_redis_ms: float | None
    last_packet_to_redis_ms: float | None


def parse_stream_id_ns(stream_id: str) -> int:
    redis_ms = int(stream_id.split("-", 1)[0])
    return redis_ms * 1_000_000


def parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    values = sorted(values)
    pos = (len(values) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(values) - 1)
    weight = pos - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


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


def load_payload(fields: dict[str, str]) -> dict[str, Any]:
    payload = fields.get("payload")
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {}


def field_or_payload_metric(
    fields: dict[str, str],
    payload: dict[str, Any],
    key: str,
) -> Any:
    if key in fields:
        return fields[key]
    return (payload.get("producer_metrics") or {}).get(key)


def make_sample(stream_id: str, fields: dict[str, str]) -> LatencySample:
    payload = load_payload(fields)
    request_key = payload.get("request_key") or {}
    redis_arrival_ns = parse_stream_id_ns(stream_id)

    ready_ns = parse_int(field_or_payload_metric(fields, payload, "feature_ready_wall_ns"))
    publish_start_ns = parse_int(fields.get("publish_start_wall_ns"))
    publish_end_ns = parse_int(fields.get("publish_end_wall_ns"))
    first_packet_ts_us = parse_int(field_or_payload_metric(fields, payload, "first_packet_ts_us"))
    last_packet_ts_us = parse_int(field_or_payload_metric(fields, payload, "last_packet_ts_us"))

    first_packet_to_redis_ms = None
    if first_packet_ts_us is not None and first_packet_ts_us >= EPOCH_US_MIN:
        first_packet_to_redis_ms = (redis_arrival_ns - first_packet_ts_us * 1_000) / 1_000_000

    last_packet_to_redis_ms = None
    if last_packet_ts_us is not None and last_packet_ts_us >= EPOCH_US_MIN:
        last_packet_to_redis_ms = (redis_arrival_ns - last_packet_ts_us * 1_000) / 1_000_000

    first_packet_to_xadd_done_ms = None
    if (
        publish_end_ns is not None
        and first_packet_ts_us is not None
        and first_packet_ts_us >= EPOCH_US_MIN
    ):
        first_packet_to_xadd_done_ms = (publish_end_ns - first_packet_ts_us * 1_000) / 1_000_000

    last_packet_to_xadd_done_ms = None
    if (
        publish_end_ns is not None
        and last_packet_ts_us is not None
        and last_packet_ts_us >= EPOCH_US_MIN
    ):
        last_packet_to_xadd_done_ms = (publish_end_ns - last_packet_ts_us * 1_000) / 1_000_000

    return LatencySample(
        stream_id=stream_id,
        run_id=fields.get("run_id") or payload.get("run_id"),
        capture_mode=fields.get("capture_mode")
        or (payload.get("producer_metrics") or {}).get("capture_mode"),
        logical_flow_id=fields.get("logical_flow_id") or payload.get("logical_flow_id"),
        direction=request_key.get("direction"),
        ready_to_redis_id_ms=(redis_arrival_ns - ready_ns) / 1_000_000 if ready_ns is not None else None,
        publish_to_redis_id_ms=(redis_arrival_ns - publish_start_ns) / 1_000_000
        if publish_start_ns is not None
        else None,
        ready_to_xadd_done_ms=(publish_end_ns - ready_ns) / 1_000_000
        if publish_end_ns is not None and ready_ns is not None
        else None,
        xadd_duration_ms=(publish_end_ns - publish_start_ns) / 1_000_000
        if publish_end_ns is not None and publish_start_ns is not None
        else None,
        first_packet_to_xadd_done_ms=first_packet_to_xadd_done_ms,
        last_packet_to_xadd_done_ms=last_packet_to_xadd_done_ms,
        first_packet_to_redis_ms=first_packet_to_redis_ms,
        last_packet_to_redis_ms=last_packet_to_redis_ms,
    )


def print_summary(name: str, values: list[float]) -> None:
    summary = summarize(values)
    print(
        "%-24s count=%6d min=%9.3f p50=%9.3f p95=%9.3f p99=%9.3f max=%9.3f mean=%9.3f ms"
        % (
            name,
            summary["count"],
            summary["min"],
            summary["p50"],
            summary["p95"],
            summary["p99"],
            summary["max"],
            summary["mean"],
        )
    )


def write_csv(path: Path, samples: list[LatencySample]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "stream_id",
                "run_id",
                "capture_mode",
                "logical_flow_id",
                "direction",
                "ready_to_redis_id_ms",
                "publish_to_redis_id_ms",
                "ready_to_xadd_done_ms",
                "xadd_duration_ms",
                "first_packet_to_xadd_done_ms",
                "last_packet_to_xadd_done_ms",
                "first_packet_to_redis_ms",
                "last_packet_to_redis_ms",
            ],
        )
        writer.writeheader()
        for sample in samples:
            writer.writerow(sample.__dict__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Redis Stream에 적재된 online flow request의 Redis 도착 latency 분위수를 계산한다."
    )
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    parser.add_argument("--stream", default="flow_features")
    parser.add_argument("--start", default="-")
    parser.add_argument("--end", default="+")
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--capture-mode", default=None)
    parser.add_argument("--csv-out", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        import redis
    except ImportError as exc:
        raise RuntimeError("redis 패키지가 필요합니다. requirements_redis.txt를 설치하세요.") from exc

    client = redis.Redis.from_url(args.redis_url, decode_responses=True)
    entries = client.xrange(args.stream, min=args.start, max=args.end, count=args.count)
    latency_entries = client.xrange(
        args.stream + ":latency",
        min=args.start,
        max=args.end,
        count=args.count,
    )
    latency_by_source_id = {
        fields["source_stream_id"]: fields
        for _stream_id, fields in latency_entries
        if "source_stream_id" in fields
    }
    samples = []
    for stream_id, fields in entries:
        merged_fields = dict(fields)
        merged_fields.update(latency_by_source_id.get(str(stream_id), {}))
        sample = make_sample(str(stream_id), merged_fields)
        if args.run_id is not None and sample.run_id != args.run_id:
            continue
        if args.capture_mode is not None and sample.capture_mode != args.capture_mode:
            continue
        samples.append(sample)

    print(
        "stream=%s samples=%d run_id=%s capture_mode=%s"
        % (args.stream, len(samples), args.run_id or "*", args.capture_mode or "*")
    )
    print_summary(
        "ready_to_xadd_done",
        [sample.ready_to_xadd_done_ms for sample in samples if sample.ready_to_xadd_done_ms is not None],
    )
    print_summary(
        "xadd_duration",
        [sample.xadd_duration_ms for sample in samples if sample.xadd_duration_ms is not None],
    )
    print_summary(
        "ready_to_redis_id",
        [
            sample.ready_to_redis_id_ms
            for sample in samples
            if sample.ready_to_redis_id_ms is not None
        ],
    )
    print_summary(
        "publish_to_redis_id",
        [
            sample.publish_to_redis_id_ms
            for sample in samples
            if sample.publish_to_redis_id_ms is not None
        ],
    )
    print_summary(
        "first_packet_to_xadd_done",
        [
            sample.first_packet_to_xadd_done_ms
            for sample in samples
            if sample.first_packet_to_xadd_done_ms is not None
        ],
    )
    print_summary(
        "last_packet_to_xadd_done",
        [
            sample.last_packet_to_xadd_done_ms
            for sample in samples
            if sample.last_packet_to_xadd_done_ms is not None
        ],
    )
    print_summary(
        "first_packet_to_redis",
        [
            sample.first_packet_to_redis_ms
            for sample in samples
            if sample.first_packet_to_redis_ms is not None
        ],
    )
    print_summary(
        "last_packet_to_redis",
        [
            sample.last_packet_to_redis_ms
            for sample in samples
            if sample.last_packet_to_redis_ms is not None
        ],
    )

    if args.csv_out is not None:
        write_csv(Path(args.csv_out), samples)
        print("csv=%s" % args.csv_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
