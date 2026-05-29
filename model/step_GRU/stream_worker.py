#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[2]))

from model.step_GRU.dataset_catalog import (
    DATASET_TYPES,
    SEQUENCE_LENGTHS,
    require_existing_file,
    resolve_dataset_config,
)


LEGACY_MININET_FEATURE_NAMES = [
    "frame_len",
    "ip_len",
    "ip_ttl",
    "tcp_len",
    "tcp_hdr_len",
    "tcp_window_size",
    "iat_us",
    "retransmission",
    "out_of_order",
    "duplicate_ack",
    "fast_retransmission",
]

ZERO_DEFAULT_FEATURES = {
    "retransmission",
    "out_of_order",
    "duplicate_ack",
    "fast_retransmission",
}

FEATURE_ALIASES = {
    "tcp_len": ["tcp_len", "tcp_payload_bytes"],
}


def resolve_torch_device(torch_module, device: str):
    if device == "auto":
        return torch_module.device("cuda" if torch_module.cuda.is_available() else "cpu")

    resolved = torch_module.device(device)
    if resolved.type == "cuda" and not torch_module.cuda.is_available():
        raise RuntimeError(
            f"CUDA device was requested ({device}), but torch.cuda.is_available() is false"
        )
    return resolved


def _duration_ms(start_ns: int | None, end_ns: int | None) -> float | None:
    if start_ns is None or end_ns is None:
        return None
    return (end_ns - start_ns) / 1_000_000


def _compact_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    if not metrics:
        return {}
    return {
        key: metrics[key]
        for key in (
            "feature_ready_wall_ns",
            "capture_mode",
            "first_packet_ts_us",
            "last_packet_ts_us",
            "feature_packet_count_observed",
        )
        if metrics.get(key) is not None
    }


class StepGruStreamWorker:
    """Redis Stream request consumer and Pub/Sub result publisher."""

    def __init__(
        self,
        redis_url: str,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        response_channel: str,
        model_path: str,
        scaler_path: str | None = None,
        threshold: float = 0.5,
        tolerance: float = 0.05,
        input_size: int | None = None,
        hidden_size: int | None = None,
        device: str = "auto",
        result_log: str | None = None,
        idle_timeout_sec: float | None = None,
    ):
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("redis 패키지가 필요합니다. requirements_redis.txt를 설치하세요.") from exc
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("torch 패키지가 필요합니다. GRU 추론 환경에서 실행하세요.") from exc
        from model.step_GRU.inference import FlowClassifier

        self.torch = torch
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.response_channel = response_channel
        self.device = resolve_torch_device(torch, device)
        if idle_timeout_sec is not None and idle_timeout_sec <= 0:
            raise ValueError("idle_timeout_sec must be positive")
        self.idle_timeout_sec = idle_timeout_sec
        self.classifier = FlowClassifier(
            model_path=model_path,
            scaler_path=scaler_path,
            device=self.device,
            input_size=input_size,
            hidden_size=hidden_size,
            threshold=threshold,
            tolerance=tolerance,
        )
        self.result_log = Path(result_log) if result_log else None

    @property
    def input_size(self) -> int:
        return self.classifier.input_size

    def _project_features(self, request: dict) -> list[list[float]]:
        rows = request["x"]
        if not rows:
            raise ValueError("request x must contain at least one feature row")

        row_width = len(rows[0])
        if any(len(row) != row_width for row in rows):
            raise ValueError("request x rows must all have the same feature count")
        if row_width == self.input_size:
            return rows

        feature_names = request.get("feature_names")
        if not feature_names:
            raise ValueError(
                f"request has {row_width} features, but model expects {self.input_size}; "
                "include feature_names or use weights trained with the online feature schema"
            )
        if len(feature_names) != row_width:
            raise ValueError(
                f"feature_names has {len(feature_names)} entries, but request rows have {row_width} features"
            )
        if self.input_size == len(LEGACY_MININET_FEATURE_NAMES):
            return self._project_named_features(rows, feature_names, LEGACY_MININET_FEATURE_NAMES)

        raise ValueError(
            f"request feature schema has {row_width} columns, but model expects {self.input_size}"
        )

    def _project_named_features(
        self,
        rows: list[list[float]],
        feature_names: list[str],
        target_names: list[str],
    ) -> list[list[float]]:
        name_to_index = {name: index for index, name in enumerate(feature_names)}
        selectors: list[int | None] = []

        for target_name in target_names:
            source_names = FEATURE_ALIASES.get(target_name, [target_name])
            source_index = next(
                (name_to_index[name] for name in source_names if name in name_to_index),
                None,
            )
            if source_index is None and target_name not in ZERO_DEFAULT_FEATURES:
                raise ValueError(
                    f"request feature_names cannot be adapted to model schema; missing {target_name}"
                )
            selectors.append(source_index)

        return [
            [
                0.0 if source_index is None else row[source_index]
                for source_index in selectors
            ]
            for row in rows
        ]

    def ensure_group(self) -> None:
        try:
            self.redis.xgroup_create(
                self.stream_name,
                self.group_name,
                id="0",
                mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def infer(
        self,
        request: dict,
        *,
        stream_id: str,
        stream_fields: dict[str, str],
        worker_received_wall_ns: int,
    ) -> dict:
        flow_key = request.get("online_flow_key")
        if not flow_key:
            raise ValueError("request must contain online_flow_key")
        direction_name = flow_key["direction"]
        seq_len = int(request.get("seq_len", len(request["x"])))

        if self.device.type == "cuda":
            self.torch.cuda.synchronize()
        infer_start_perf = time.perf_counter()
        infer_start_wall_ns = time.time_ns()
        result = self.classifier.classify(
            self._project_features(request),
            direction=direction_name,
            seq_len=seq_len,
        )
        if self.device.type == "cuda":
            self.torch.cuda.synchronize()
        infer_end_wall_ns = time.time_ns()
        inference_ms = (time.perf_counter() - infer_start_perf) * 1000

        producer_metrics = _compact_metrics(request.get("producer_metrics"))
        feature_ready_wall_ns = producer_metrics.get("feature_ready_wall_ns")
        redis_publish_start_wall_ns = stream_fields.get("publish_start_wall_ns")
        response = {
            "online_flow_key": request.get("online_flow_key"),
            "logical_flow_id": request.get("logical_flow_id"),
            "run_id": request.get("run_id"),
            "stream_id": stream_id,
            "inference_ms": inference_ms,
            "producer_metrics": producer_metrics,
            "observed_directional_payload_bytes": request.get("observed_directional_payload_bytes"),
            "worker_received_wall_ns": worker_received_wall_ns,
            "worker_infer_start_wall_ns": infer_start_wall_ns,
            "worker_infer_end_wall_ns": infer_end_wall_ns,
            "worker_queue_wait_ms": _duration_ms(worker_received_wall_ns, infer_start_wall_ns),
            "worker_total_ms": _duration_ms(worker_received_wall_ns, infer_end_wall_ns),
            **result,
        }
        if feature_ready_wall_ns is not None:
            response["ready_to_worker_received_ms"] = _duration_ms(
                int(feature_ready_wall_ns),
                worker_received_wall_ns,
            )
            response["ready_to_worker_done_ms"] = _duration_ms(
                int(feature_ready_wall_ns),
                infer_end_wall_ns,
            )
        if redis_publish_start_wall_ns:
            response["redis_stream_publish_start_wall_ns"] = int(redis_publish_start_wall_ns)
            response["stream_publish_to_worker_received_ms"] = _duration_ms(
                int(redis_publish_start_wall_ns),
                worker_received_wall_ns,
            )
        return response

    def _append_result_log(self, response: dict) -> None:
        if self.result_log is None:
            return
        self.result_log.parent.mkdir(parents=True, exist_ok=True)
        row = {"logged_at_wall_ns": time.time_ns(), **response}
        with self.result_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def run(self) -> None:
        self.ensure_group()
        print(
            "[*] step_GRU stream worker started "
            f"(stream={self.stream_name}, group={self.group_name}, "
            f"channel={self.response_channel}, device={self.device})",
            flush=True,
        )
        processed_messages = 0
        last_message_at = time.monotonic()
        while True:
            messages = self.redis.xreadgroup(
                self.group_name,
                self.consumer_name,
                {self.stream_name: ">"},
                count=1,
                block=1000,
            )
            if not messages:
                if (
                    self.idle_timeout_sec is not None
                    and processed_messages > 0
                    and time.monotonic() - last_message_at >= self.idle_timeout_sec
                ):
                    print(
                        "[*] idle timeout reached after %.3f sec; stopping worker"
                        % self.idle_timeout_sec,
                        flush=True,
                    )
                    return
                continue

            for _stream, entries in messages:
                for stream_id, fields in entries:
                    last_message_at = time.monotonic()
                    worker_received_wall_ns = time.time_ns()
                    payload = fields.get("payload")
                    if payload is None:
                        self.redis.xack(self.stream_name, self.group_name, stream_id)
                        processed_messages += 1
                        continue

                    request = json.loads(payload)
                    response = self.infer(
                        request,
                        stream_id=stream_id,
                        stream_fields=fields,
                        worker_received_wall_ns=worker_received_wall_ns,
                    )
                    response["result_publish_wall_ns"] = time.time_ns()
                    self.redis.publish(
                        self.response_channel,
                        json.dumps(response, ensure_ascii=False),
                    )
                    response["result_publish_done_wall_ns"] = time.time_ns()
                    response["result_publish_duration_ms"] = _duration_ms(
                        response["result_publish_wall_ns"],
                        response["result_publish_done_wall_ns"],
                    )
                    self.redis.xack(self.stream_name, self.group_name, stream_id)
                    response["stream_xack_wall_ns"] = time.time_ns()
                    self._append_result_log(response)
                    print(
                        "[result] stream_id=%s flow=%s score=%.4f label=%s"
                        % (
                            stream_id,
                            response["logical_flow_id"],
                            response["score"],
                            response["predicted_label"],
                        ),
                        flush=True,
                    )
                    processed_messages += 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Redis Stream 기반 step_GRU classifier worker")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    parser.add_argument("--stream", default="flow_features")
    parser.add_argument("--group", default="gru_classifiers")
    parser.add_argument("--consumer", default="classifier-1")
    parser.add_argument("--response-channel", default="flow_results")
    parser.add_argument("--model-path")
    parser.add_argument("--dataset-root", default="dataset")
    parser.add_argument("--direction-dataset", default="elephant_dst_to_src")
    parser.add_argument("--dataset-type", choices=DATASET_TYPES, default="fb")
    parser.add_argument("--seq-len", type=int, choices=SEQUENCE_LENGTHS, default=10)
    parser.add_argument("--scaler-path")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--input-size", type=int)
    parser.add_argument("--hidden-size", type=int)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--result-log")
    parser.add_argument(
        "--idle-timeout-sec",
        type=float,
        default=None,
        help="Stop after this many idle seconds once at least one stream message was processed",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.model_path:
        model_path = require_existing_file(args.model_path, "model weights")
    else:
        dataset_config = resolve_dataset_config(
            dataset_type=args.dataset_type,
            seq_len=args.seq_len,
            direction_dataset=args.direction_dataset,
            root=args.dataset_root,
        )
        model_path = require_existing_file(dataset_config.weights_path, "model weights")

    worker = StepGruStreamWorker(
        redis_url=args.redis_url,
        stream_name=args.stream,
        group_name=args.group,
        consumer_name=args.consumer,
        response_channel=args.response_channel,
        model_path=str(model_path),
        scaler_path=args.scaler_path,
        threshold=args.threshold,
        tolerance=args.tolerance,
        input_size=args.input_size,
        hidden_size=args.hidden_size,
        device=args.device,
        result_log=args.result_log,
        idle_timeout_sec=args.idle_timeout_sec,
    )
    worker.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
