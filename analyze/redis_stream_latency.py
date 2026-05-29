#!/usr/bin/env python3
from __future__ import annotations

"""
analyze/redis_stream_latency.py

이 스크립트는 온라인 파이프라인에서 Redis Stream으로 적재된
`flow_features` 항목과 보조 latency Stream(`flow_features:latency`)을 읽어,
producer(캡처/파싱/큐잉) 측면에서 측정한 여러 지연(latency) 통계를 계산합니다.

주요 아이디어:
- 각 `flow_features` 항목은 `payload`(요청 본문)와 함께 `publish_start_wall_ns`
  같은 필드를 가지며, 보조 Stream에는 `publish_end_wall_ns` 등이 기록됩니다.
- 이 두 스트림을 병합해 'feature_ready' 시점, 'XADD시작', 'XADD완료',
  패킷 관측 시점(first/last packet epoch)' 등을 기준으로 지연을 계산합니다.

지표(예):
- ready_to_xadd_done: feature_ready_wall_ns -> publish_end_wall_ns
- xadd_duration: publish_start_wall_ns -> publish_end_wall_ns
- first_packet_to_xadd_done / last_packet_to_xadd_done: 패킷 ts -> publish_end

참고: Redis Stream ID는 ms 정밀도이므로 sub-ms 비교에는 부적절합니다. 따라서
분석은 주로 producer 측에서 기록한 wall-clock ns 값을 사용합니다.
"""

import argparse
import csv
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    # 상위 디렉터리에 있는 로컬 모듈을 import할 수 있도록 경로 추가
    sys.path.append(str(Path(__file__).resolve().parents[1]))


# epoch 기준으로 의미있는 최소 마이크로초값 (2000-01-01) - 유효한 epoch 판정에 사용
EPOCH_US_MIN = 946_684_800_000_000  # 2000-01-01T00:00:00Z


@dataclass
class LatencySample:
    """스트림 항목 하나에서 추출한 지연 관련 값 집합.

    대부분의 필드는 ms 단위 float이며, 값이 없을 경우 None을 허용한다.
    """
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
    """Redis Stream ID의 타임스탬프(ms)를 nanosecond 단위로 변환.

    Redis Stream ID는 "<ms>-<seq>" 형태로, 앞부분은 millisecond 정밀도이다.
    분석에서는 이 값을 nanosecond 단위로 환산해 epoch 기반 계산에 사용한다.
    """
    redis_ms = int(stream_id.split("-", 1)[0])
    return redis_ms * 1_000_000


def parse_int(value: Any) -> int | None:
    """문자열 혹은 None을 정수로 안전하게 변환. 빈 문자열 -> None 반환."""
    if value in (None, ""):
        return None
    return int(value)


def percentile(values: list[float], q: float) -> float:
    """정렬된 리스트에서 선형 보간을 사용해 분위수(percentile)를 계산.

    q는 0.0~1.0 사이 (예: 0.99 -> p99)
    """
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
    """값 리스트에서 count, min, p50, p95, p99, max, mean을 반환.

    None 값은 무시한다. 빈 리스트면 0으로 채운 결과를 반환한다.
    """
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
    """스트림 필드에서 `payload`(JSON 문자열)를 안전히 파싱해 dict로 반환."""
    payload = fields.get("payload")
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        # payload가 깨져있으면 빈 dict를 반환해 분석이 중단되지 않게 함
        return {}


def field_or_payload_metric(
    fields: dict[str, str],
    payload: dict[str, Any],
    key: str,
) -> Any:
    """우선순위: 스트림 필드 > payload.producer_metrics.

    일부 지표는 XADD 시점에 직접 필드로 기록되기도 하고, payload 내부
    producer_metrics에 들어가기도 한다. 둘 중 존재하는 값을 반환한다.
    """
    if key in fields:
        return fields[key]
    return (payload.get("producer_metrics") or {}).get(key)


def make_sample(stream_id: str, fields: dict[str, str]) -> LatencySample:
    """하나의 Redis Stream entry에서 LatencySample을 구성한다.

    계산 원리:
    - stream_id -> Redis 도착 시점을 ns 단위로 변환(`redis_arrival_ns`).
    - producer가 기록한 wall-clock 필드들(`feature_ready_wall_ns`,
      `publish_start_wall_ns`, `publish_end_wall_ns`)를 읽는다.
    - payload 내부의 `first_packet_ts_us`, `last_packet_ts_us`(epoch us)
      가 유효하면 이를 기준으로 여러 지연을 계산한다.
    """
    payload = load_payload(fields)
    request_key = payload.get("request_key") or payload.get("online_flow_key") or {}
    redis_arrival_ns = parse_stream_id_ns(stream_id)

    # producer가 보낸 wall-clock ns 값들(문자열) 혹은 payload 내부 metric
    ready_ns = parse_int(field_or_payload_metric(fields, payload, "feature_ready_wall_ns"))
    publish_start_ns = parse_int(fields.get("publish_start_wall_ns"))
    publish_end_ns = parse_int(fields.get("publish_end_wall_ns"))
    first_packet_ts_us = parse_int(field_or_payload_metric(fields, payload, "first_packet_ts_us"))
    last_packet_ts_us = parse_int(field_or_payload_metric(fields, payload, "last_packet_ts_us"))

    # Redis arrival(id) 기준 지연: packet_ts(us) -> redis_arrival_ns (ns)
    first_packet_to_redis_ms = None
    if first_packet_ts_us is not None and first_packet_ts_us >= EPOCH_US_MIN:
        first_packet_to_redis_ms = (redis_arrival_ns - first_packet_ts_us * 1_000) / 1_000_000

    last_packet_to_redis_ms = None
    if last_packet_ts_us is not None and last_packet_ts_us >= EPOCH_US_MIN:
        last_packet_to_redis_ms = (redis_arrival_ns - last_packet_ts_us * 1_000) / 1_000_000

    # Redis XADD 완료 기준 지연: packet_ts(us) -> publish_end_ns (ns)
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
        # ready(ts) -> redis(id) 차이 (ms): Redis ID 기준 참고용
        ready_to_redis_id_ms=(redis_arrival_ns - ready_ns) / 1_000_000 if ready_ns is not None else None,
        # publish_start (producer wall-clock) -> redis(id) 차이 (ms)
        publish_to_redis_id_ms=(redis_arrival_ns - publish_start_ns) / 1_000_000
        if publish_start_ns is not None
        else None,
        # ready -> publish_end 차이 (producer wall-clock)
        ready_to_xadd_done_ms=(publish_end_ns - ready_ns) / 1_000_000
        if publish_end_ns is not None and ready_ns is not None
        else None,
        # publish duration (producer 측정)
        xadd_duration_ms=(publish_end_ns - publish_start_ns) / 1_000_000
        if publish_end_ns is not None and publish_start_ns is not None
        else None,
        first_packet_to_xadd_done_ms=first_packet_to_xadd_done_ms,
        last_packet_to_xadd_done_ms=last_packet_to_xadd_done_ms,
        first_packet_to_redis_ms=first_packet_to_redis_ms,
        last_packet_to_redis_ms=last_packet_to_redis_ms,
    )


def print_summary(name: str, values: list[float]) -> None:
    """요약 결과를 사람이 보기 쉬운 포맷으로 출력한다."""
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
    """samples를 CSV로 저장한다 (디버깅/외부 분석용)."""
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

    # Redis에서 본 스트림과 보조 latency 스트림을 읽는다.
    client = redis.Redis.from_url(args.redis_url, decode_responses=True)
    entries = client.xrange(args.stream, min=args.start, max=args.end, count=args.count)
    latency_entries = client.xrange(
        args.stream + ":latency",
        min=args.start,
        max=args.end,
        count=args.count,
    )

    # latency 보조 스트림은 source_stream_id로 본 스트림 항목을 참조하므로,
    # 이를 사전으로 만들어 병합에 사용한다.
    latency_by_source_id = {
        fields["source_stream_id"]: fields
        for _stream_id, fields in latency_entries
        if "source_stream_id" in fields
    }

    samples = []
    for stream_id, fields in entries:
        # 본 스트림 필드와 latency 보조 스트림 필드를 병합해 하나의 레코드로 만든다.
        merged_fields = dict(fields)
        merged_fields.update(latency_by_source_id.get(str(stream_id), {}))
        sample = make_sample(str(stream_id), merged_fields)
        # run_id / capture_mode 필터가 지정되면 그것에 맞는 샘플만 수집
        if args.run_id is not None and sample.run_id != args.run_id:
            continue
        if args.capture_mode is not None and sample.capture_mode != args.capture_mode:
            continue
        samples.append(sample)

    print(
        "stream=%s samples=%d run_id=%s capture_mode=%s"
        % (args.stream, len(samples), args.run_id or "*", args.capture_mode or "*")
    )

    # 다양한 지표에 대해 분위수를 출력한다.
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
