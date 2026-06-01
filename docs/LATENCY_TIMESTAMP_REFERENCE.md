# Latency Timestamp Reference

이 문서는 온라인 분류 파이프라인에서 latency 측정을 위해 사용하는 timestamp 필드를 정리한다.
대상 경로는 XDP/tshark capture -> FlowCache -> Redis Stream -> step_GRU worker -> Redis Pub/Sub -> FlowCache update이다.

## 핵심 원칙

- 대부분의 latency 계산은 `time.time_ns()`로 찍은 epoch wall-clock nanosecond 값을 사용한다.
- duration 필드는 `(end_ns - start_ns) / 1_000_000`으로 millisecond 단위로 저장한다.
- Redis Stream ID는 millisecond 정밀도이므로 sub-ms 분석의 기준값으로는 부정확하다.
- XDP의 `bpf_ktime_get_ns()`는 monotonic timestamp라서 epoch 기준 분석에 바로 쓸 수 없다. user-space에서 `time.time_ns() - time.monotonic_ns()` offset을 더해 `epoch_ts_us`로 변환한다.
- tshark의 `frame.time_epoch`는 epoch timestamp라서 그대로 `epoch_ts_us`로 변환한다.
- 패킷 기준 latency는 모델 feature에 실제 사용된 payload packet만 대상으로 한다. ACK-only packet은 제외한다.

## 전체 흐름

```text
packet observed
  -> feature ready detected
  -> request built
  -> request enqueued to local publish queue
  -> publisher thread dequeued
  -> Redis Stream XADD start/end
  -> worker received Stream entry
  -> worker inference start/end
  -> worker Pub/Sub publish start/done
  -> subscriber received result
  -> FlowCache apply start/done
```

## 파일별 역할

| 파일 | 역할 |
| --- | --- |
| `xdp/tg_xdp_capture.py` | XDP packet timestamp를 만들고, producer 및 E2E latency field를 기록한다. |
| `pipeline/realtime/tg_tshark_capture.py` | tshark packet timestamp를 만들고, XDP와 같은 producer/E2E latency field를 기록한다. |
| `pipeline/realtime/online_request.py` | feature request payload의 `producer_metrics`를 만든다. |
| `pipeline/realtime/online_tg_flow_cache.py` | `OnlinePacketEvent`를 `PacketRecord`로 변환하면서 `epoch_ts_us`를 보존한다. |
| `pipeline/redis/transport.py` | Redis Stream XADD 시작/완료 timestamp와 latency 보조 Stream을 기록한다. |
| `model/step_GRU/stream_worker.py` | worker 수신/추론/결과 publish timestamp와 worker latency를 기록한다. |
| `pipeline/redis/result_subscriber.py` | Pub/Sub 결과 수신 timestamp를 기록한다. |
| `analyze/redis_stream_latency.py` | Redis Stream과 `flow_features:latency`를 읽어 producer -> Redis 지연을 요약한다. |
| `analyze/online_e2e_latency.py` | `online_classification_latency.jsonl`의 E2E latency field를 요약한다. |

## Packet Timestamp

| 필드 | 단위 | 생성 위치 | 의미 |
| --- | --- | --- | --- |
| `BpfPacketEvent.ts_ns` | ns | `xdp/tg_xdp_capture.py` BPF program | `bpf_ktime_get_ns()`로 찍은 kernel monotonic packet timestamp |
| `OnlinePacketEvent.ts_us` | us | `xdp/tg_xdp_capture.py::make_event()` | XDP `ts_ns // 1000`; monotonic 기준 |
| `OnlinePacketEvent.epoch_ts_us` | us | `xdp/tg_xdp_capture.py::make_event()` | XDP monotonic timestamp에 epoch offset을 더한 packet epoch timestamp |
| `OnlinePacketEvent.ts_us` | us | `pipeline/realtime/tg_tshark_capture.py::parse_tshark_line()` | tshark `frame.time_epoch`를 us로 변환한 값 |
| `OnlinePacketEvent.epoch_ts_us` | us | `pipeline/realtime/tg_tshark_capture.py::parse_tshark_line()` | tshark `frame.time_epoch`를 us로 변환한 값 |
| `PacketRecord.epoch_ts_us` | us | `pipeline/realtime/online_tg_flow_cache.py::_event_to_packet_record()` | latency 분석을 위해 동적으로 붙이는 packet epoch timestamp |
| `first_packet_ts_us` | us | `pipeline/realtime/online_request.py::build_online_flow_request()` | feature에 사용된 첫 payload packet timestamp |
| `last_packet_ts_us` | us | `pipeline/realtime/online_request.py::build_online_flow_request()` | feature에 사용된 마지막 payload packet timestamp |

`first_packet_ts_us`, `last_packet_ts_us`는 `producer_metrics`에 저장된다. `epoch_ts_us`가 있으면 epoch 기준 값을 우선 사용하고, 없으면 `PacketRecord.ts_us`를 사용한다.

## Producer Metrics

`producer_metrics`는 Redis Stream payload 안에 들어가는 latency 전용 메타데이터다.

| 필드 | 단위 | 생성 위치 | 의미 |
| --- | --- | --- | --- |
| `feature_ready_wall_ns` | ns | `build_online_flow_request()` | flow가 모델 입력 가능 상태가 된 시각. 호출자가 넘긴 값이 있으면 사용하고 없으면 `time.time_ns()` |
| `capture_mode` | string | `build_online_flow_request()` 호출부 | `xdp` 또는 `tshark` |
| `first_packet_ts_us` | us | `build_online_flow_request()` | feature 첫 payload packet timestamp |
| `last_packet_ts_us` | us | `build_online_flow_request()` | feature 마지막 payload packet timestamp |
| `feature_packet_count_observed` | count | `build_online_flow_request()` | feature에 실제 사용된 payload packet 수 |
| `event_received_wall_ns` | ns | XDP/tshark capture | capture callback/loop가 packet event를 받은 시각 |
| `process_event_start_wall_ns` | ns | XDP/tshark capture | `online_cache.process_event()` 시작 시각 |
| `process_event_end_wall_ns` | ns | XDP/tshark capture | `online_cache.process_event()` 종료 시각 |
| `ready_detected_wall_ns` | ns | XDP/tshark capture | `online_cache.flow_cache.is_ready(entry)` 이후 pending 처리 직전 시각 |
| `request_built_wall_ns` | ns | XDP/tshark capture | `build_online_flow_request()` 완료 직후 시각 |
| `publish_enqueue_start_wall_ns` | ns | XDP/tshark capture | local publish queue enqueue 직전 시각 |
| `publish_enqueued_wall_ns` | ns | XDP/tshark capture | local publish queue enqueue 시점으로 기록하는 시각 |
| `publisher_dequeued_wall_ns` | ns | XDP/tshark publisher thread | publisher thread가 queue에서 request를 꺼낸 시각 |

주의: 현재 코드에서 `publish_enqueue_start_wall_ns`와 `publish_enqueued_wall_ns`는 모두 `publish_queue.put()` 직전에 연속으로 찍힌다. 따라서 이 값은 queue put 비용보다는 callback 안에서 enqueue 직전까지 도달했음을 표시하는 marker에 가깝다.

## Redis Stream Timestamp

| 필드 | 단위 | 저장 위치 | 생성 위치 | 의미 |
| --- | --- | --- | --- | --- |
| `publish_start_wall_ns` | ns | `flow_features` field, payload 외부 | `RedisStreamProducer.publish()` | Redis Stream XADD 호출 직전 |
| `publish_end_wall_ns` | ns | `flow_features:latency` field | `RedisStreamProducer.publish()` | Redis Stream XADD 완료 직후 |
| `source_stream_id` | string | `flow_features:latency` field | `RedisStreamProducer.publish()` | 보조 latency Stream row가 가리키는 원본 `flow_features` Stream ID |
| Redis Stream ID | ms | `flow_features` entry ID | Redis | Redis가 부여한 entry ID. `<epoch_ms>-<seq>` 형식 |

`pipeline/redis/transport.py`는 요청 본문을 `flow_features`에 넣고, latency 분석 전용 필드를 `flow_features:latency`에 한 번 더 기록한다. 분석할 때는 `source_stream_id`로 두 Stream을 join한다.

## Worker Timestamp

| 필드 | 단위 | 생성 위치 | 의미 |
| --- | --- | --- | --- |
| `worker_received_wall_ns` | ns | `StepGruStreamWorker.run()` | worker가 Redis Stream entry를 받은 직후 |
| `worker_infer_start_wall_ns` | ns | `StepGruStreamWorker.infer()` | 모델 추론 시작 직전 |
| `worker_infer_end_wall_ns` | ns | `StepGruStreamWorker.infer()` | 모델 추론 종료 직후 |
| `inference_ms` | ms | `StepGruStreamWorker.infer()` | `time.perf_counter()` 기준 추론 시간. CUDA는 전후 `torch.cuda.synchronize()` 포함 |
| `result_publish_wall_ns` | ns | `StepGruStreamWorker.run()` | Redis Pub/Sub publish 호출 직전 |
| `result_publish_done_wall_ns` | ns | `StepGruStreamWorker.run()` | Redis Pub/Sub publish 완료 직후 |
| `stream_xack_wall_ns` | ns | `StepGruStreamWorker.run()` | Redis Stream entry ACK 직후 |
| `logged_at_wall_ns` | ns | `StepGruStreamWorker._append_result_log()` | worker local result log에 쓴 시각 |

worker는 request payload의 `producer_metrics`를 `_compact_metrics()`로 줄여 response에 보존한다. 이 response가 Pub/Sub으로 전달되고, capture 프로세스의 subscriber가 받아 `online_classification_latency.jsonl`에 기록한다.

## Subscriber 및 FlowCache Timestamp

| 필드 | 단위 | 생성 위치 | 의미 |
| --- | --- | --- | --- |
| `subscriber_received_wall_ns` | ns | `RedisResultSubscriber.listen_forever()` | capture 프로세스가 Pub/Sub 결과를 받은 시각 |
| `cache_apply_start_wall_ns` | ns | XDP/tshark `on_classifier_result()` | classification result를 FlowCache에 적용하기 직전 |
| `cache_updated_wall_ns` | ns | XDP/tshark `on_classifier_result()` | FlowCache update 완료 직후 |

`cache_apply_start_wall_ns`와 `cache_updated_wall_ns`는 `add_e2e_latency_fields()`에서 result row에 추가된다.

## E2E Latency Field

아래 필드는 `xdp/tg_xdp_capture.py::add_e2e_latency_fields()`와 `pipeline/realtime/tg_tshark_capture.py::add_e2e_latency_fields()`에서 만들어지고, `online_classification_latency.jsonl`에 저장된다.

| latency field | 계산식 |
| --- | --- |
| `cache_apply_duration_ms` | `cache_apply_start_wall_ns -> cache_updated_wall_ns` |
| `ready_to_cache_updated_ms` | `feature_ready_wall_ns -> cache_updated_wall_ns` |
| `ready_to_request_built_ms` | `feature_ready_wall_ns -> request_built_wall_ns` |
| `request_built_to_worker_received_ms` | `request_built_wall_ns -> worker_received_wall_ns` |
| `request_built_to_publish_enqueued_ms` | `request_built_wall_ns -> publish_enqueued_wall_ns` |
| `publish_enqueue_duration_ms` | `publish_enqueue_start_wall_ns -> publish_enqueued_wall_ns` |
| `publish_enqueued_to_publisher_dequeued_ms` | `publish_enqueued_wall_ns -> publisher_dequeued_wall_ns` |
| `publisher_dequeued_to_worker_received_ms` | `publisher_dequeued_wall_ns -> worker_received_wall_ns` |
| `request_built_to_redis_publish_start_ms` | `request_built_wall_ns -> redis_stream_publish_start_wall_ns` |
| `redis_publish_start_to_worker_received_ms` | `redis_stream_publish_start_wall_ns -> worker_received_wall_ns` |
| `process_event_duration_ms` | `process_event_start_wall_ns -> process_event_end_wall_ns` |
| `ready_to_worker_received_ms` | `feature_ready_wall_ns -> worker_received_wall_ns` |
| `worker_received_to_done_ms` | `worker_received_wall_ns -> worker_infer_end_wall_ns` |
| `worker_done_to_publish_ms` | `worker_infer_end_wall_ns -> result_publish_wall_ns` |
| `pubsub_publish_to_subscriber_ms` | `result_publish_wall_ns -> subscriber_received_wall_ns` |
| `subscriber_to_cache_updated_ms` | `subscriber_received_wall_ns -> cache_updated_wall_ns` |

worker에서 먼저 만드는 latency field도 있다.

| latency field | 계산식 |
| --- | --- |
| `worker_queue_wait_ms` | `worker_received_wall_ns -> worker_infer_start_wall_ns` |
| `worker_total_ms` | `worker_received_wall_ns -> worker_infer_end_wall_ns` |
| `ready_to_worker_received_ms` | `feature_ready_wall_ns -> worker_received_wall_ns` |
| `ready_to_worker_done_ms` | `feature_ready_wall_ns -> worker_infer_end_wall_ns` |
| `stream_publish_to_worker_received_ms` | `redis_stream_publish_start_wall_ns -> worker_received_wall_ns` |
| `result_publish_duration_ms` | `result_publish_wall_ns -> result_publish_done_wall_ns` |

## Redis Stream 분석 Field

`analyze/redis_stream_latency.py`는 `flow_features`와 `flow_features:latency`를 읽어 다음 지표를 만든다.

| 분석 field | 계산식 | 해석 |
| --- | --- | --- |
| `ready_to_xadd_done_ms` | `feature_ready_wall_ns -> publish_end_wall_ns` | feature ready 이후 Redis XADD 완료까지 |
| `xadd_duration_ms` | `publish_start_wall_ns -> publish_end_wall_ns` | Redis XADD 호출 자체 비용 |
| `ready_to_redis_id_ms` | `feature_ready_wall_ns -> Redis Stream ID epoch ms` | Redis ID 기준 참고값 |
| `publish_to_redis_id_ms` | `publish_start_wall_ns -> Redis Stream ID epoch ms` | Redis ID 기준 참고값 |
| `first_packet_to_xadd_done_ms` | `first_packet_ts_us -> publish_end_wall_ns` | feature 첫 payload packet 관측 이후 XADD 완료까지 |
| `last_packet_to_xadd_done_ms` | `last_packet_ts_us -> publish_end_wall_ns` | feature 마지막 payload packet 관측 이후 XADD 완료까지 |
| `first_packet_to_redis_ms` | `first_packet_ts_us -> Redis Stream ID epoch ms` | Redis ID 기준 참고값 |
| `last_packet_to_redis_ms` | `last_packet_ts_us -> Redis Stream ID epoch ms` | Redis ID 기준 참고값 |

capture backend 비교에서는 보통 `last_packet_to_xadd_done_ms`가 가장 직접적이다. flow duration이나 packet inter-arrival time의 영향을 줄이고, 모델 입력에 필요한 마지막 packet이 관측된 뒤 request가 Redis로 나가기까지를 본다.

## 로그와 분석 명령

온라인 classification E2E 로그:

```text
runs/mininet_<run_id>/results/online_classification_latency.jsonl
```

요약:

```bash
python3 analyze/online_e2e_latency.py \
  --classification-log runs/mininet_<run_id>/results/online_classification_latency.jsonl \
  --run-id <run_id> \
  --json-out runs/mininet_<run_id>/results/online_e2e_latency_summary.json
```

Redis Stream producer latency 요약:

```bash
python3 analyze/redis_stream_latency.py \
  --redis-url redis://127.0.0.1:6379/0 \
  --stream flow_features \
  --run-id <run_id> \
  --capture-mode xdp \
  --csv-out runs/mininet_<run_id>/results/redis_stream_latency.csv
```

## 해석 시 주의사항

- 서로 다른 프로세스 사이 timestamp를 빼는 구조라 같은 host clock을 전제로 한다.
- `time.time_ns()`는 wall-clock이므로 NTP 보정 등으로 시스템 시간이 크게 조정되면 long-running 실험의 duration에 영향이 생길 수 있다.
- `inference_ms`는 `perf_counter()` 기준이고, 나머지 E2E field는 대부분 `time.time_ns()` 기준이다. 추론 시간 자체를 볼 때는 `inference_ms`, 전체 경로를 볼 때는 `worker_received_to_done_ms` 또는 `ready_to_cache_updated_ms`를 본다.
- `ready_to_cache_updated_ms`는 classifier 결과가 FlowCache에 반영될 때까지의 전체 online loop latency다.
- `Redis Stream ID` 기반 field는 Redis가 부여한 millisecond timestamp를 ns로 환산한 참고 지표다. sub-ms 병목 판단에는 `publish_start_wall_ns`, `publish_end_wall_ns` 기반 field를 우선 사용한다.
- 기존 로그에는 일부 field가 없을 수 있다. 분석 스크립트는 field가 없는 row를 해당 지표의 count에서 제외한다.
