# XDP Online Pipeline Latency Summary

## 핵심 주장

본 구현은 XDP를 이용해 패킷 도착 시점에 가까운 위치에서 flow feature를 갱신하고, feature-ready 조건이 만족되면 즉시 모델 추론 요청을 생성하는 온라인 플로우 분류 파이프라인이다.

현재 구현은 hard real-time 보장을 목표로 한 시스템은 아니지만, VL2와 같은 long-tailed workload에서는 feature-ready 이후 수 ms 이내에 FlowCache까지 결과를 반영하는 near-real-time prototype으로 동작한다.

## 기존 구조의 병목

초기 구조에서는 XDP perf callback 안에서 ready flow를 감지한 뒤 Redis publish까지 직렬로 수행했다.

```text
on_event()
  -> online_cache.process_event()
  -> build_online_flow_request()
  -> RedisStreamProducer.publish()
  -> Redis XADD
  -> worker xreadgroup()
```

초기 측정에서는 request 생성과 모델 추론은 매우 빨랐지만, request를 만든 뒤 worker가 실제로 수신하기까지의 시간이 컸다.

FB 기준 초기 측정:

```text
ready_to_request_built_ms              p50 ~= 0.011 ms
process_event_duration_ms              p50 ~= 0.006 ms
inference_ms                           p50 ~= 0.336 ms
request_built_to_worker_received_ms    p50 ~= 95.8 ms
ready_to_cache_updated_ms              p50 ~= 96 ms
```

따라서 병목은 XDP packet 처리나 모델 추론이 아니라, 다음 구간에 있었다.

```text
request 생성 이후 -> Redis publish -> worker 수신
```

## 개선한 구조

Redis publish를 XDP callback에서 분리했다.

```text
on_event()
  -> online_cache.process_event()
  -> build_online_flow_request()
  -> Queue에 enqueue
  -> callback 반환

publisher thread
  -> Queue에서 request dequeue
  -> RedisStreamProducer.publish()
  -> Redis XADD
```

추가로 worker에는 작은 튜닝을 적용했다.

```text
--read-count 8
--quiet-results
worker warmup
```

의미:

- XDP callback이 Redis I/O에 막히지 않음
- Redis publish를 별도 publisher thread로 분리
- worker가 한 번 깨어났을 때 여러 stream entry를 읽을 수 있음
- per-result `print(..., flush=True)` 비용 제거
- 첫 추론 warmup으로 초기 inference 튐 완화

## 개선 후 FB 결과

Queue 기반 publisher thread 적용 후 FB에서는 전체 loop 지연이 크게 줄었다.

```text
ready_to_cache_updated_ms              p50 ~= 22.4 ms
request_built_to_worker_received_ms    p50 ~= 21.7 ms
request_built_to_publish_enqueued_ms   p50 ~= 0.001 ms
publish_enqueue_duration_ms            p50 ~= 0.000 ms
inference_ms                           p50 ~= 0.469 ms
```

초기 대비:

```text
request_built_to_worker_received_ms
95.8 ms -> 21.7 ms
약 77% 감소
```

해석:

- callback에서 queue에 넣는 비용은 사실상 0에 가까움
- XDP callback 내부의 Redis publish blocking은 제거됨
- 남은 지연은 주로 Redis-to-worker 전달 및 Python worker scheduling 구간으로 이동함

FB workload는 short flow가 많기 때문에 same-flow FCT-before 조건은 매우 빡세다. 따라서 FB 결과는 파이프라인의 한계 분석에는 유용하지만, elephant/long-tailed flow 조기 분류 목적을 보여주는 대표 결과로는 VL2가 더 적합하다.

### FB 재실험 해석

최종 worker 조건(`--read-count 8`, `--quiet-results`, idle timeout 없음)으로 FB를 다시 실행해도 전체 지연은 여전히 VL2보다 크게 나타났다.

최신 FB 재실험 예:

```text
ready_to_cache_updated_ms                 p50 ~= 17.5 ms
request_built_to_worker_received_ms       p50 ~= 16.8 ms
redis_publish_start_to_worker_received_ms p50 ~= 16.4 ms
inference_ms                              p50 ~= 0.44 ms
```

반면 XDP producer 쪽은 여전히 매우 작았다.

```text
ready_to_request_built_ms              p50 ~= 0.011 ms
request_built_to_publish_enqueued_ms   p50 ~= 0.001 ms
process_event_duration_ms              p50 ~= 0.006 ms
```

따라서 FB에서 남는 지연은 XDP 처리나 모델 추론 때문이 아니라, Redis Stream에 넣은 request를 Python worker가 실제로 받아오기까지의 전달 구간에서 발생한 것으로 해석할 수 있다.

FB에서 이 구간이 크게 나오는 가장 가능성 높은 이유는 short-flow burst 특성이다.

```text
작은 flow가 짧은 시간에 많이 완료됨
  -> feature-ready request가 한꺼번에 몰림
  -> worker는 Redis Stream에서 request를 순차적으로 받아 처리
  -> 뒤쪽 request는 worker가 받아오기 전까지 대기
  -> redis_publish_start_to_worker_received_ms 증가
```

즉 FB는 flow 자체가 빨리 끝나고 request가 burst로 몰리기 쉬워, 현재 Python+Redis 기반 전달 계층의 지연이 flow completion 전에 들어가기 어렵다. 이는 XDP packet 처리의 한계라기보다 prototype transport 계층의 한계로 보는 것이 맞다.

## 개선 후 VL2 결과

VL2 50-flow 실험에서는 전체 온라인 loop가 ms 단위로 동작했다.

```text
classified_rows = 84
unique_logical_flows = 84
```

주요 latency:

```text
Metric                                      p50      p95      p99      max
ready_to_cache_updated_ms                  1.106    1.600    4.683    18.368
ready_to_request_built_ms                  0.018    0.030    0.082    0.087
request_built_to_worker_received_ms        0.245    0.349    0.377    0.398
redis_publish_start_to_worker_received_ms  0.217    0.310    0.346    0.356
inference_ms                               0.586    1.005    4.028    17.527
```

해석:

- XDP producer path는 p50 기준 수십 us 수준이다.
- Redis-to-worker 전달 지연도 VL2 실험에서는 p50 0.217 ms, p99 0.346 ms로 낮았다.
- 전체 FlowCache 반영은 p50 1.106 ms, p99 4.683 ms였다.
- tail latency는 주로 inference 단계의 일시적인 지연에서 발생했다.

## FCT-before 기준

FCT 전에 모델 결과가 FlowCache에 반영됐는지는 다음 기준으로 평가했다.

```text
cache_updated_wall_ns / 1000 <= flows_*_meta.csv의 stop_time_us
```

즉:

```text
FlowCache update 시각 <= flow completion 시각
```

VL2 결과:

```text
flow_meta_rows = 400
classified_unique_flows = 84
cache_before_fct = 84/84 (100%)
classified_coverage = 84/400 (21%)
cache_update_margin_ms p50 = 989.646 ms
```

분류된 VL2 flow 84개는 모두 FCT 전에 FlowCache update까지 완료됐다.

## 발표용 결론

기존 구조에서는 XDP callback 안에서 Redis publish까지 직렬로 수행하면서 request 생성 이후 worker 수신까지 p50 약 96 ms의 병목이 발생했다.

이를 Queue 기반 publisher thread 구조로 분리한 결과, callback enqueue 비용은 p50 0.001 ms 수준으로 줄었고, FB 기준 전체 지연은 p50 약 22 ms까지 감소했다.

VL2 workload에서는 worker 튜닝까지 적용한 뒤 feature-ready부터 FlowCache update까지 p50 1.1 ms, p99 4.7 ms를 달성했으며, 분류된 flow 84개 모두 FCT 전에 cache에 반영되었다.

따라서 본 시스템은 XDP 기반으로 packet-arrival 시점에서 feature-ready trigger를 만들고, long-tailed workload에서 near-real-time online flow classification을 수행할 수 있음을 확인했다.

## 발표에서 조심할 표현

사용 가능한 표현:

```text
XDP 기반 실시간 온라인 플로우 분류 파이프라인
near-real-time online flow classification prototype
feature-ready 이후 p50 1.1 ms 내 FlowCache update
VL2 기준 분류된 flow 100% FCT-before update
```

피하는 것이 좋은 표현:

```text
모든 workload에서 FCT 전에 항상 분류 완료
hard real-time 보장
line-rate 제어 완성
FB short-flow까지 모두 same-flow control 가능
```

## 한 줄 요약

XDP 자체와 request 생성은 병목이 아니었고, 기존 병목은 Redis publish와 worker 수신 구간이었다. Redis publish를 callback 밖으로 분리하고 worker를 가볍게 튜닝한 결과, VL2에서는 feature-ready 이후 p50 1.1 ms, p99 4.7 ms 안에 FlowCache update를 완료했고, 분류된 flow 전부가 FCT 전에 반영됐다.
