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


# 예전 Mininet 학습 코드에서 사용하던 11개 feature 순서입니다.
# 실시간 파이프라인이 더 많은 feature를 보내더라도, legacy weight를 쓰는 경우
# 아래 순서에 맞춰 필요한 열만 골라 모델 입력으로 재구성합니다.
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

# 일부 TCP 이벤트 feature는 tshark 캡처에서 항상 관측되지 않을 수 있습니다.
# legacy 모델에는 해당 열이 필요하므로, 요청에 없을 때 0.0으로 채워 넣습니다.
ZERO_DEFAULT_FEATURES = {
    "retransmission",
    "out_of_order",
    "duplicate_ack",
    "fast_retransmission",
}

# 실시간 파이프라인과 학습 데이터셋 사이에서 이름만 달라진 feature를 연결합니다.
# 예: legacy 모델은 tcp_len을 기대하지만 온라인 feature는 tcp_payload_bytes로 올 수 있습니다.
FEATURE_ALIASES = {
    "tcp_len": ["tcp_len", "tcp_payload_bytes"],
}


def resolve_torch_device(torch_module, device: str):
    """CLI에서 받은 device 문자열을 torch.device로 변환하고 CUDA 사용 가능 여부를 검증합니다."""
    if device == "auto":
        return torch_module.device("cuda" if torch_module.cuda.is_available() else "cpu")

    resolved = torch_module.device(device)
    if resolved.type == "cuda" and not torch_module.cuda.is_available():
        raise RuntimeError(
            f"CUDA device was requested ({device}), but torch.cuda.is_available() is false"
        )
    return resolved


def _duration_ms(start_ns: int | None, end_ns: int | None) -> float | None:
    """나노초 단위 wall-clock timestamp 두 개를 밀리초 duration으로 변환합니다."""
    if start_ns is None or end_ns is None:
        return None
    return (end_ns - start_ns) / 1_000_000


def _compact_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    """producer가 보낸 상세 metrics 중 결과 로그와 지연시간 분석에 필요한 값만 남깁니다."""
    if not metrics:
        return {}
    return {
        key: metrics[key]
        for key in (
            "feature_ready_wall_ns",
            "ready_detected_wall_ns",
            "request_built_wall_ns",
            "publish_enqueue_start_wall_ns",
            "publish_enqueued_wall_ns",
            "publisher_dequeued_wall_ns",
            "event_received_wall_ns",
            "process_event_start_wall_ns",
            "process_event_end_wall_ns",
            "capture_mode",
            "first_packet_ts_us",
            "last_packet_ts_us",
            "feature_packet_count_observed",
        )
        if metrics.get(key) is not None
    }


class StepGruStreamWorker:
    """Redis Stream 요청을 소비하고 step_GRU 추론 결과를 Pub/Sub로 발행하는 워커."""

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
        read_count: int = 1,
        quiet_results: bool = False,
    ):
        # redis와 torch는 stream worker 실행에만 필요한 무거운 의존성입니다.
        # import 오류 메시지를 실행 목적에 맞게 보여주기 위해 생성자 안에서 지연 import합니다.
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
        # idle_timeout_sec은 배치 실험/측정 스크립트에서 워커를 자동 종료시키기 위한 옵션입니다.
        # 메시지를 하나도 처리하지 않은 상태에서는 producer 시작 지연을 기다릴 수 있도록 종료하지 않습니다.
        if idle_timeout_sec is not None and idle_timeout_sec <= 0:
            raise ValueError("idle_timeout_sec must be positive")
        self.idle_timeout_sec = idle_timeout_sec
        if read_count <= 0:
            raise ValueError("read_count must be positive")
        self.read_count = read_count
        self.quiet_results = quiet_results
        self.classifier = FlowClassifier(
            model_path=model_path,
            scaler_path=scaler_path,
            device=self.device,
            input_size=input_size,
            hidden_size=hidden_size,
            threshold=threshold,
            tolerance=tolerance,
        )
        # result_log가 지정되면 Pub/Sub 발행과 별개로 JSON Lines 형식의 로컬 감사 로그를 남깁니다.
        self.result_log = Path(result_log) if result_log else None

    @property
    def input_size(self) -> int:
        return self.classifier.input_size

    def _project_features(self, request: dict) -> list[list[float]]:
        """요청 feature 행렬을 현재 로드된 모델의 input_size에 맞게 변환합니다."""
        rows = request["x"]
        if not rows:
            raise ValueError("request x must contain at least one feature row")

        # GRU 입력은 [seq_len, feature_count] 형태의 고정 폭 행렬이어야 합니다.
        # 행마다 feature 개수가 다르면 스케일링/텐서 변환 시 의미가 깨지므로 즉시 거부합니다.
        row_width = len(rows[0])
        if any(len(row) != row_width for row in rows):
            raise ValueError("request x rows must all have the same feature count")
        if row_width == self.input_size:
            return rows

        # 온라인 feature schema와 모델 학습 schema가 다를 수 있으므로 feature_names가 있어야
        # 어떤 열을 어떤 모델 입력으로 보낼지 안전하게 매핑할 수 있습니다.
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

        # 현재는 legacy 11-feature 모델로의 변환만 명시적으로 지원합니다.
        # 다른 input_size는 잘못된 weight/schema 조합일 가능성이 높아 조용히 추론하지 않습니다.
        raise ValueError(
            f"request feature schema has {row_width} columns, but model expects {self.input_size}"
        )

    def _project_named_features(
        self,
        rows: list[list[float]],
        feature_names: list[str],
        target_names: list[str],
    ) -> list[list[float]]:
        """feature_names를 기준으로 rows를 target_names 순서의 행렬로 재배열합니다."""
        name_to_index = {name: index for index, name in enumerate(feature_names)}
        selectors: list[int | None] = []

        for target_name in target_names:
            # target feature 하나가 여러 source 이름 중 하나로 들어올 수 있으므로 alias를 순서대로 탐색합니다.
            source_names = FEATURE_ALIASES.get(target_name, [target_name])
            source_index = next(
                (name_to_index[name] for name in source_names if name in name_to_index),
                None,
            )
            # 일부 이벤트성 feature는 미관측을 0으로 해석할 수 있지만,
            # 길이/TTL/window 같은 기본 feature가 없으면 모델 입력을 구성할 수 없습니다.
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
        """Redis Stream consumer group이 없으면 만들고, 이미 있으면 그대로 사용합니다."""
        try:
            self.redis.xgroup_create(
                self.stream_name,
                self.group_name,
                id="0",
                mkstream=True,
            )
        except Exception as exc:
            # Redis는 이미 존재하는 group 생성 시 BUSYGROUP 에러를 냅니다.
            # 여러 워커/재실행 상황에서 정상적인 상태이므로 이 경우만 무시합니다.
            if "BUSYGROUP" not in str(exc):
                raise

    def warmup(self) -> None:
        """첫 실측 요청에 모델/디바이스 초기화 비용이 섞이지 않도록 더미 추론을 한 번 실행합니다."""
        dummy_x = [[0.0] * self.input_size]
        try:
            self.classifier.classify(dummy_x, direction="dst_to_src", seq_len=1)
        except Exception as exc:
            print(f"[*] warmup skipped: {exc}", flush=True)

    def infer(
        self,
        request: dict,
        *,
        stream_id: str,
        stream_fields: dict[str, str],
        worker_received_wall_ns: int,
    ) -> dict:
        """단일 stream payload를 모델에 넣고 결과와 latency metrics를 합친 응답 dict를 만듭니다."""
        flow_key = request.get("online_flow_key")
        if not flow_key:
            raise ValueError("request must contain online_flow_key")
        direction_name = flow_key["direction"]
        seq_len = int(request.get("seq_len", len(request["x"])))

        # CUDA 연산은 비동기 실행되므로 synchronize를 넣어야 inference_ms가 실제 GPU 실행 시간을 포함합니다.
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
        # response는 모델 결과뿐 아니라 producer->worker->publisher 전 구간의 timestamp를 포함합니다.
        # result_subscriber나 로그 분석 스크립트가 병목 구간을 분리해서 계산할 수 있게 하기 위함입니다.
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
            # feature가 준비된 시점부터 worker 수신/추론 완료까지의 end-to-end 지연입니다.
            response["ready_to_worker_received_ms"] = _duration_ms(
                int(feature_ready_wall_ns),
                worker_received_wall_ns,
            )
            response["ready_to_worker_done_ms"] = _duration_ms(
                int(feature_ready_wall_ns),
                infer_end_wall_ns,
            )
        if redis_publish_start_wall_ns:
            # producer가 Redis Stream publish를 시작한 시각이 stream field에 있으면
            # Redis enqueue + consumer group wakeup까지의 시간을 별도로 추적합니다.
            response["redis_stream_publish_start_wall_ns"] = int(redis_publish_start_wall_ns)
            response["stream_publish_to_worker_received_ms"] = _duration_ms(
                int(redis_publish_start_wall_ns),
                worker_received_wall_ns,
            )
        return response

    def _append_result_log(self, response: dict) -> None:
        """추론 응답을 JSON Lines 파일에 한 줄로 추가합니다."""
        if self.result_log is None:
            return
        self.result_log.parent.mkdir(parents=True, exist_ok=True)
        row = {"logged_at_wall_ns": time.time_ns(), **response}
        with self.result_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def run(self) -> None:
        """Redis Stream을 계속 읽으면서 요청별 추론, 결과 발행, ACK 처리를 수행합니다."""
        self.ensure_group()
        self.warmup()
        print(
            "[*] step_GRU stream worker started "
            f"(stream={self.stream_name}, group={self.group_name}, "
            f"channel={self.response_channel}, device={self.device})",
            flush=True,
        )
        processed_messages = 0
        last_message_at = time.monotonic()
        while True:
            # ">"는 이 consumer group에서 아직 어떤 consumer에게도 전달되지 않은 새 메시지만 읽겠다는 뜻입니다.
            # block=1000으로 1초씩 깨어나 idle timeout 조건도 주기적으로 확인합니다.
            messages = self.redis.xreadgroup(
                self.group_name,
                self.consumer_name,
                {self.stream_name: ">"},
                count=self.read_count,
                block=1000,
            )
            if not messages:
                # 실험 자동화에서는 producer가 모든 요청을 보낸 뒤 worker를 직접 종료하기 번거롭습니다.
                # 적어도 하나의 메시지를 처리한 후 지정 시간 동안 새 메시지가 없으면 정상 종료합니다.
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
                        # payload 없는 메시지는 처리할 수 없지만 pending entry로 남기면 재전달이 반복됩니다.
                        # ACK 후 건너뛰어 stream group 상태를 깨끗하게 유지합니다.
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
                    # 결과는 response_channel Pub/Sub로 즉시 발행합니다.
                    # stream에는 요청만 쌓고, 결과는 subscriber가 실시간으로 받는 구조입니다.
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
                    # Pub/Sub 발행까지 끝난 뒤 ACK합니다. 중간 오류가 나면 Redis pending 상태에 남아
                    # 필요할 경우 재처리/점검할 수 있습니다.
                    self.redis.xack(self.stream_name, self.group_name, stream_id)
                    response["stream_xack_wall_ns"] = time.time_ns()
                    self._append_result_log(response)
                    if not self.quiet_results:
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
    """CLI 옵션을 정의합니다. 모델 경로를 직접 주지 않으면 dataset_catalog에서 기본 weight를 찾습니다."""
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
    parser.add_argument(
        "--read-count",
        type=int,
        default=1,
        help="Maximum Redis Stream entries to fetch per xreadgroup wakeup",
    )
    parser.add_argument(
        "--quiet-results",
        action="store_true",
        help="Do not print one line per classified stream entry",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    # --model-path가 있으면 해당 파일을 최우선으로 사용합니다.
    # 없으면 dataset 종류/방향/seq_len 조합으로 프로젝트 표준 weight 위치를 계산합니다.
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
        read_count=args.read_count,
        quiet_results=args.quiet_results,
    )
    worker.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
