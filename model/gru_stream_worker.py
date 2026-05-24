#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))


DIRECTION_TO_INDEX = {
    "src_to_dst": 0,
    "dst_to_src": 1,
    "bidirectional": 0,
}


class GruStreamWorker:
    """Redis Stream에서 feature 요청을 읽고 Pub/Sub으로 추론 결과를 응답한다."""

    def __init__(
        self,
        redis_url: str,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        response_channel: str,
        model_path: str,
        scaler_path: str,
        threshold: float = 0.5,
        tolerance: float = 0.01,
        input_size: int = 18,
        hidden_size: int = 64,
        device: str = "auto",
    ):
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("redis 패키지가 필요합니다. requirements_redis.txt를 설치하세요.") from exc
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("torch 패키지가 필요합니다. GRU 추론 환경에서 실행하세요.") from exc
        from model.GRU.data import FeatureScaler
        from model.GRU.evaluate import load_model

        self.torch = torch
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.response_channel = response_channel
        self.threshold = threshold
        self.tolerance = tolerance
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(device)
        self.scaler = FeatureScaler.load(scaler_path)
        self.model = load_model(
            model_path=model_path,
            device=self.device,
            input_size=input_size,
            hidden_size=hidden_size,
        )

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

    def infer(self, request: dict) -> dict:
        x = self.scaler.transform(request["x"])
        direction_name = request["request_key"]["direction"]
        direction_idx = DIRECTION_TO_INDEX[direction_name]
        seq_len = int(request.get("seq_len", len(x)))

        torch = self.torch
        x_tensor = torch.tensor(x, dtype=torch.float32).unsqueeze(0).to(self.device)
        direction_tensor = torch.tensor([direction_idx], dtype=torch.long).to(self.device)
        seq_len_tensor = torch.tensor([seq_len], dtype=torch.long).to(self.device)

        if self.device.type == "cuda":
            torch.cuda.synchronize()
        start = time.perf_counter()
        with torch.no_grad():
            score, exit_step = self.model(
                x_tensor,
                direction_tensor,
                seq_len=seq_len_tensor,
                enable_early_exit=True,
                tolerance=self.tolerance,
            )
        if self.device.type == "cuda":
            torch.cuda.synchronize()
        inference_ms = (time.perf_counter() - start) * 1000

        predicted_label = "elephant" if float(score) >= self.threshold else "mice"
        return {
            "request_key": request["request_key"],
            "logical_flow_id": request.get("logical_flow_id"),
            "score": float(score),
            "predicted_label": predicted_label,
            "threshold": self.threshold,
            "exit_step": int(exit_step),
            "inference_ms": inference_ms,
            "run_id": request.get("run_id"),
        }

    def run(self) -> None:
        self.ensure_group()
        print(
            "[*] GRU stream worker started "
            f"(stream={self.stream_name}, group={self.group_name}, channel={self.response_channel})",
            flush=True,
        )
        while True:
            messages = self.redis.xreadgroup(
                self.group_name,
                self.consumer_name,
                {self.stream_name: ">"},
                count=1,
                block=1000,
            )
            if not messages:
                continue

            for _stream, entries in messages:
                for stream_id, fields in entries:
                    payload = fields.get("payload")
                    if payload is None:
                        self.redis.xack(self.stream_name, self.group_name, stream_id)
                        continue

                    request = json.loads(payload)
                    response = self.infer(request)
                    response["stream_id"] = stream_id
                    self.redis.publish(
                        self.response_channel,
                        json.dumps(response, ensure_ascii=False),
                    )
                    self.redis.xack(self.stream_name, self.group_name, stream_id)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Redis Stream 기반 GRU classifier worker")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    parser.add_argument("--stream", default="flow_features")
    parser.add_argument("--group", default="gru_classifiers")
    parser.add_argument("--consumer", default="classifier-1")
    parser.add_argument("--response-channel", default="flow_results")
    parser.add_argument("--model-path", default="runs/gru/mininet_pretrain/best.pt")
    parser.add_argument("--scaler-path", default="runs/gru/mininet_pretrain/scaler.json")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--tolerance", type=float, default=0.01)
    parser.add_argument("--input-size", type=int, default=18)
    parser.add_argument("--hidden-size", type=int, default=64)
    parser.add_argument("--device", default="auto")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    worker = GruStreamWorker(
        redis_url=args.redis_url,
        stream_name=args.stream,
        group_name=args.group,
        consumer_name=args.consumer,
        response_channel=args.response_channel,
        model_path=args.model_path,
        scaler_path=args.scaler_path,
        threshold=args.threshold,
        tolerance=args.tolerance,
        input_size=args.input_size,
        hidden_size=args.hidden_size,
        device=args.device,
    )
    worker.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
