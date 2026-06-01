#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from pipeline.realtime.online_request import build_online_flow_request
from pipeline.realtime.online_tg_flow_cache import (
    TrafficGeneratorOnlineFlowCache,
    OnlinePacketEvent,
    parse_tg_metadata,
)
from pipeline.redis.result_subscriber import RedisResultSubscriber
from pipeline.redis.transport import RedisStreamProducer


MAX_PAYLOAD_PREFIX = 20

FIELDS = [
    "frame.number",
    "frame.time_epoch",
    "frame.len",
    "ip.src",
    "ip.dst",
    "ip.len",
    "ip.hdr_len",
    "ip.ttl",
    "ip.dsfield.dscp",
    "ip.dsfield.ecn",
    "tcp.srcport",
    "tcp.dstport",
    "tcp.len",
    "tcp.hdr_len",
    "tcp.seq",
    "tcp.ack",
    "tcp.flags",
    "tcp.window_size",
    "tcp.payload",
]


def _to_int(value: str, default: int = 0) -> int:
    if value is None or value == "":
        return default
    if "," in value:
        value = value.split(",", 1)[0]
    return int(float(value))


def _to_epoch_us(value: str) -> int:
    return int(float(value) * 1_000_000)


def _parse_flags(value: str) -> int:
    if not value:
        return 0
    if "," in value:
        value = value.split(",", 1)[0]
    return int(value, 16) if value.startswith("0x") else int(value)


def _payload_prefix(value: str) -> bytes:
    if not value:
        return b""
    payload_hex = value.replace(":", "")
    if len(payload_hex) > MAX_PAYLOAD_PREFIX * 2:
        payload_hex = payload_hex[: MAX_PAYLOAD_PREFIX * 2]
    try:
        return bytes.fromhex(payload_hex)
    except ValueError:
        return b""


def _duration_ms(start_ns: int | None, end_ns: int | None) -> float | None:
    if start_ns is None or end_ns is None:
        return None
    return (end_ns - start_ns) / 1_000_000


def _int_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _append_jsonl(path: str | None, row: dict) -> None:
    if path is None:
        return
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def add_e2e_latency_fields(result: dict, cache_apply_start_wall_ns: int, cache_updated_wall_ns: int) -> dict:
    producer_metrics = result.get("producer_metrics") or {}
    feature_ready_wall_ns = _int_or_none(producer_metrics.get("feature_ready_wall_ns"))
    request_built_wall_ns = _int_or_none(producer_metrics.get("request_built_wall_ns"))
    publish_enqueue_start_wall_ns = _int_or_none(
        producer_metrics.get("publish_enqueue_start_wall_ns")
    )
    publish_enqueued_wall_ns = _int_or_none(producer_metrics.get("publish_enqueued_wall_ns"))
    publisher_dequeued_wall_ns = _int_or_none(producer_metrics.get("publisher_dequeued_wall_ns"))
    process_event_start_wall_ns = _int_or_none(producer_metrics.get("process_event_start_wall_ns"))
    process_event_end_wall_ns = _int_or_none(producer_metrics.get("process_event_end_wall_ns"))
    worker_received_wall_ns = _int_or_none(result.get("worker_received_wall_ns"))
    worker_done_wall_ns = _int_or_none(result.get("worker_infer_end_wall_ns"))
    redis_stream_publish_start_wall_ns = _int_or_none(
        result.get("redis_stream_publish_start_wall_ns")
    )
    result_publish_wall_ns = _int_or_none(result.get("result_publish_wall_ns"))
    subscriber_received_wall_ns = _int_or_none(result.get("subscriber_received_wall_ns"))

    result["cache_apply_start_wall_ns"] = cache_apply_start_wall_ns
    result["cache_updated_wall_ns"] = cache_updated_wall_ns
    result["cache_apply_duration_ms"] = _duration_ms(cache_apply_start_wall_ns, cache_updated_wall_ns)
    result["ready_to_cache_updated_ms"] = _duration_ms(feature_ready_wall_ns, cache_updated_wall_ns)
    result["ready_to_request_built_ms"] = _duration_ms(feature_ready_wall_ns, request_built_wall_ns)
    result["request_built_to_worker_received_ms"] = _duration_ms(request_built_wall_ns, worker_received_wall_ns)
    result["request_built_to_publish_enqueued_ms"] = _duration_ms(
        request_built_wall_ns,
        publish_enqueued_wall_ns,
    )
    result["publish_enqueue_duration_ms"] = _duration_ms(
        publish_enqueue_start_wall_ns,
        publish_enqueued_wall_ns,
    )
    result["publish_enqueued_to_publisher_dequeued_ms"] = _duration_ms(
        publish_enqueued_wall_ns,
        publisher_dequeued_wall_ns,
    )
    result["publisher_dequeued_to_worker_received_ms"] = _duration_ms(
        publisher_dequeued_wall_ns,
        worker_received_wall_ns,
    )
    result["request_built_to_redis_publish_start_ms"] = _duration_ms(
        request_built_wall_ns,
        redis_stream_publish_start_wall_ns,
    )
    result["redis_publish_start_to_worker_received_ms"] = _duration_ms(
        redis_stream_publish_start_wall_ns,
        worker_received_wall_ns,
    )
    result["process_event_duration_ms"] = _duration_ms(process_event_start_wall_ns, process_event_end_wall_ns)
    result["ready_to_worker_received_ms"] = _duration_ms(feature_ready_wall_ns, worker_received_wall_ns)
    result["worker_received_to_done_ms"] = _duration_ms(worker_received_wall_ns, worker_done_wall_ns)
    result["worker_done_to_publish_ms"] = _duration_ms(worker_done_wall_ns, result_publish_wall_ns)
    result["pubsub_publish_to_subscriber_ms"] = _duration_ms(
        result_publish_wall_ns,
        subscriber_received_wall_ns,
    )
    result["subscriber_to_cache_updated_ms"] = _duration_ms(
        subscriber_received_wall_ns,
        cache_updated_wall_ns,
    )
    return result


def parse_tshark_line(line: str, ifname: str, fallback_frame_number: int) -> OnlinePacketEvent | None:
    values = line.rstrip("\n").split("\t")
    if len(values) < len(FIELDS):
        values += [""] * (len(FIELDS) - len(values))
    elif len(values) > len(FIELDS):
        values = values[: len(FIELDS)]

    value_map = dict(zip(FIELDS, values))
    if not value_map.get("ip.src") or not value_map.get("ip.dst"):
        return None

    epoch_ts_us = _to_epoch_us(value_map["frame.time_epoch"])
    tcp_flags = _parse_flags(value_map.get("tcp.flags", ""))
    return OnlinePacketEvent(
        ts_us=epoch_ts_us,
        epoch_ts_us=epoch_ts_us,
        frame_number=_to_int(value_map.get("frame.number", ""), fallback_frame_number),
        ifname=ifname,
        src_ip=value_map.get("ip.src", ""),
        dst_ip=value_map.get("ip.dst", ""),
        src_port=_to_int(value_map.get("tcp.srcport", "")),
        dst_port=_to_int(value_map.get("tcp.dstport", "")),
        frame_len=_to_int(value_map.get("frame.len", "")),
        ip_len=_to_int(value_map.get("ip.len", "")),
        ip_hdr_len=_to_int(value_map.get("ip.hdr_len", "")),
        ip_ttl=_to_int(value_map.get("ip.ttl", "")),
        ip_dscp=_to_int(value_map.get("ip.dsfield.dscp", "")),
        ip_ecn=_to_int(value_map.get("ip.dsfield.ecn", "")),
        tcp_len=_to_int(value_map.get("tcp.len", "")),
        tcp_hdr_len=_to_int(value_map.get("tcp.hdr_len", "")),
        tcp_seq=_to_int(value_map.get("tcp.seq", "")),
        tcp_ack=_to_int(value_map.get("tcp.ack", "")),
        tcp_flags=tcp_flags,
        tcp_window_size=_to_int(value_map.get("tcp.window_size", "")),
        payload_prefix=_payload_prefix(value_map.get("tcp.payload", "")),
    )


def build_tshark_command(interface: str, capture_filter: str | None) -> list[str]:
    cmd = [
        "tshark",
        "-n",
        "-l",
        "-i",
        interface,
        "-T",
        "fields",
        "-E",
        "separator=\t",
    ]
    for field in FIELDS:
        cmd += ["-e", field]
    if capture_filter:
        cmd += ["-f", capture_filter]
    return cmd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="tshark로 TrafficGenerator TCP 패킷을 온라인 캡처한다.")
    parser.add_argument("-i", "--interface", required=True)
    parser.add_argument("--capture-filter", default="tcp")
    parser.add_argument("--feature-packet-count", type=int, default=10)
    parser.add_argument("--server-port", type=int, default=5001)
    parser.add_argument("--print-ready", action="store_true")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--redis-url", default=None)
    parser.add_argument("--redis-stream", default="flow_features")
    parser.add_argument("--redis-stream-maxlen", type=int, default=None)
    parser.add_argument("--redis-response-channel", default="flow_results")
    parser.add_argument("--classification-log", default=None)
    parser.add_argument(
        "--publish-direction",
        choices=["src_to_dst", "dst_to_src", "all"],
        default="dst_to_src",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    online_cache = TrafficGeneratorOnlineFlowCache(
        feature_packet_count=args.feature_packet_count,
        server_port=args.server_port,
    )
    stats = {
        "events": 0,
        "metadata": 0,
        "matched": 0,
        "ready": 0,
        "published": 0,
        "publish_queued": 0,
        "parse_errors": 0,
        "publish_errors": 0,
        "classified": 0,
    }
    direction_counts = {"src_to_dst": 0, "dst_to_src": 0, "unknown": 0}
    stats_lock = threading.Lock()
    stream_producer = (
        RedisStreamProducer(
            redis_url=args.redis_url,
            stream_name=args.redis_stream,
            maxlen=args.redis_stream_maxlen,
        )
        if args.redis_url
        else None
    )
    result_subscriber = None
    publish_queue: queue.Queue[tuple[dict, object] | object] = queue.Queue()
    publisher_stop = object()
    publisher_thread = None

    def on_classifier_result(result: dict) -> None:
        cache_apply_start_wall_ns = time.time_ns()
        updated = online_cache.apply_classification_result(result)
        cache_updated_wall_ns = time.time_ns()
        if updated is None:
            return
        add_e2e_latency_fields(result, cache_apply_start_wall_ns, cache_updated_wall_ns)
        updated.classification_result = dict(result)
        _append_jsonl(args.classification_log, result)
        with stats_lock:
            stats["classified"] += 1
        if args.print_ready:
            key = updated.key
            print(
                "[classified] src=%s:%s dst=%s:%s flow_id=%s direction=%s label=%s score=%.4f e2e_ms=%s"
                % (
                    key.src_ip,
                    key.src_port,
                    key.dst_ip,
                    key.dst_port,
                    key.flow_id,
                    updated.direction,
                    updated.status,
                    updated.model_score if updated.model_score is not None else -1.0,
                    (
                        "%.3f" % result["ready_to_cache_updated_ms"]
                        if result.get("ready_to_cache_updated_ms") is not None
                        else "n/a"
                    ),
                )
            )

    def should_publish(entry) -> bool:
        return args.publish_direction == "all" or entry.direction == args.publish_direction

    def publisher_loop() -> None:
        assert stream_producer is not None
        while True:
            item = publish_queue.get()
            try:
                if item is publisher_stop:
                    return
                request, entry = item
                request["producer_metrics"]["publisher_dequeued_wall_ns"] = time.time_ns()
                stream_id = stream_producer.publish(request)
                with stats_lock:
                    stats["published"] += 1
                if args.print_ready:
                    flow_key = request["online_flow_key"]
                    print(
                        "[stream] id=%s stream=%s src=%s:%s dst=%s:%s flow_id=%s direction=%s"
                        % (
                            stream_id,
                            args.redis_stream,
                            flow_key["src_ip"],
                            flow_key["src_port"],
                            flow_key["dst_ip"],
                            flow_key["dst_port"],
                            flow_key["flow_id"],
                            flow_key["direction"],
                        )
                    )
            except Exception as exc:
                with stats_lock:
                    stats["publish_errors"] += 1
                if item is not publisher_stop:
                    _request, entry = item
                    online_cache.flow_cache.set_status(entry, "default")
                print(f"[redis] stream publish failed: {exc}", file=sys.stderr)
            finally:
                publish_queue.task_done()

    def process_event(event: OnlinePacketEvent, event_received_wall_ns: int) -> None:
        if event.dst_port == args.server_port:
            direction_counts["src_to_dst"] += 1
        elif event.src_port == args.server_port:
            direction_counts["dst_to_src"] += 1
        else:
            direction_counts["unknown"] += 1

        if parse_tg_metadata(event.payload_prefix) is not None:
            stats["metadata"] += 1

        process_event_start_wall_ns = time.time_ns()
        entry = online_cache.process_event(event)
        process_event_end_wall_ns = time.time_ns()

        if entry is not None:
            stats["matched"] += 1

        if entry is not None and online_cache.flow_cache.is_ready(entry):
            stats["ready"] += 1
            if args.print_ready:
                key = entry.key
                print(
                    "[ready] src=%s:%s dst=%s:%s flow_id=%s direction=%s packets=%s cumulative_payload_bytes=%s"
                    % (
                        key.src_ip,
                        key.src_port,
                        key.dst_ip,
                        key.dst_port,
                        key.flow_id,
                        entry.direction,
                        len(entry.packets),
                        entry.cumulative_payload_bytes,
                    )
                )

            if stream_producer is not None and should_publish(entry):
                try:
                    online_cache.mark_pending(entry)
                    ready_detected_wall_ns = time.time_ns()
                    request = build_online_flow_request(
                        entry,
                        packet_count=args.feature_packet_count,
                        run_id=args.run_id,
                        capture_mode="tshark",
                        feature_ready_wall_ns=ready_detected_wall_ns,
                    )
                    request_built_wall_ns = time.time_ns()
                    request["producer_metrics"].update(
                        {
                            "event_received_wall_ns": event_received_wall_ns,
                            "process_event_start_wall_ns": process_event_start_wall_ns,
                            "process_event_end_wall_ns": process_event_end_wall_ns,
                            "ready_detected_wall_ns": ready_detected_wall_ns,
                            "request_built_wall_ns": request_built_wall_ns,
                        }
                    )
                    request["producer_metrics"]["publish_enqueue_start_wall_ns"] = time.time_ns()
                    request["producer_metrics"]["publish_enqueued_wall_ns"] = time.time_ns()
                    publish_queue.put((request, entry))
                    stats["publish_queued"] += 1
                except Exception as exc:
                    stats["publish_errors"] += 1
                    online_cache.flow_cache.set_status(entry, "default")
                    print(f"[redis] stream enqueue failed: {exc}", file=sys.stderr)
            elif entry.status == "default":
                online_cache.mark_pending(entry)

    if stream_producer is not None:
        stream_producer.connect()
        publisher_thread = threading.Thread(
            target=publisher_loop,
            name="redis-stream-publisher",
            daemon=True,
        )
        publisher_thread.start()
        result_subscriber = RedisResultSubscriber(
            redis_url=args.redis_url,
            channel_name=args.redis_response_channel,
            on_result=on_classifier_result,
        )
        result_subscriber.start_background()

    cmd = build_tshark_command(args.interface, args.capture_filter)
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    print("[*] tshark TrafficGenerator capture attached to %s" % args.interface, flush=True)
    if stream_producer is not None:
        print(
            "[*] Redis Stream publishing enabled (stream=%s, direction=%s, response_channel=%s)"
            % (args.redis_stream, args.publish_direction, args.redis_response_channel),
            flush=True,
        )

    fallback_frame_number = 0
    try:
        assert process.stdout is not None
        for line in process.stdout:
            fallback_frame_number += 1
            event_received_wall_ns = time.time_ns()
            try:
                event = parse_tshark_line(line, args.interface, fallback_frame_number)
            except Exception as exc:
                stats["parse_errors"] += 1
                print(f"[tshark] parse failed: {exc}", file=sys.stderr)
                continue
            if event is None:
                continue
            stats["events"] += 1
            process_event(event, event_received_wall_ns)
    except KeyboardInterrupt:
        pass
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if stream_producer is not None:
            publish_queue.put(publisher_stop)
            if publisher_thread is not None:
                publisher_thread.join(timeout=2)
            stream_producer.close()
        if result_subscriber is not None:
            result_subscriber.close()
        print(
            "[summary] events=%s metadata=%s matched=%s ready=%s published=%s classified=%s "
            "parse_errors=%s publish_errors=%s "
            "directions(src_to_dst=%s,dst_to_src=%s,unknown=%s)"
            % (
                stats["events"],
                stats["metadata"],
                stats["matched"],
                stats["ready"],
                stats["published"],
                stats["classified"],
                stats["parse_errors"],
                stats["publish_errors"],
                direction_counts["src_to_dst"],
                direction_counts["dst_to_src"],
                direction_counts["unknown"],
            ),
            flush=True,
        )
        print("[*] tshark capture stopped", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
