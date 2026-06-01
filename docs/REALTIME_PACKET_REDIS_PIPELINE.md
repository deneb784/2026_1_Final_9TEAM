# 실시간 XDP / Redis / step_GRU 파이프라인

이 문서는 현재 코드 기준의 XDP/Redis 실시간 분류 경로를 구조 참고용으로 정리한다. 환경 준비, 실행 명령, 결과 확인 절차는 `README.md`를 기준으로 확인하고, 전체 설계 흐름은 `docs/OVERVIEW.md`를 참고한다.

## 1. 현재 구조

실시간 경로는 `tshark`가 아니라 **XDP 기반**이다.

```text
main.py
  -> Mininet fat-tree + TrafficGenerator 실행
  -> 각 host namespace의 h*-eth0에 XDP capture attach

xdp/tg_xdp_capture.py
  -> TCP/IP header + TrafficGenerator payload prefix 추출
  -> TrafficGeneratorOnlineFlowCache로 logical flow 구성
  -> feature_packet_count개 payload packet이 모이면 Redis Stream publish

Redis Stream: flow_features
  -> model.step_GRU.stream_worker가 consumer group으로 읽음
  -> weights.pt 기반 elephant/mice 추론
  -> JSONL result log 저장
  -> Redis Pub/Sub: flow_results publish

xdp/tg_xdp_capture.py
  -> flow_results 구독
  -> RealtimeFlowCache entry 상태를 elephant/mice로 갱신
```

`pcap` 모드는 tshark로 pcap을 저장하는 오프라인 데이터셋 생성용이다. Redis로 바로 전송하는 실시간 경로는 `--capture-mode xdp`, `--capture-mode tshark`, 또는 검증용 `--capture-mode xdp-verify`에서 동작한다.

## 2. 관련 파일

| 파일 | 역할 |
|---|---|
| `main.py` | Mininet, TrafficGenerator, XDP capturer 실행 |
| `network/packet_capture.py` | host namespace별 `tg_xdp_capture.py` 실행 |
| `xdp/tg_xdp_capture.py` | XDP attach, packet event 처리, Redis publish/result subscribe |
| `pipeline/realtime/online_tg_flow_cache.py` | TG metadata 기반 logical flow 구성 |
| `pipeline/realtime/online_flow_cache.py` | 실시간 flow 상태 및 classifier 결과 반영 |
| `pipeline/realtime/online_request.py` | OnlineFlowEntry를 모델 request JSON으로 변환 |
| `pipeline/redis/transport.py` | Redis Stream `flow_features` / latency stream 적재 |
| `pipeline/redis/result_subscriber.py` | Redis Pub/Sub `flow_results` 구독 |
| `model/step_GRU/stream_worker.py` | Redis Stream consumer + step_GRU 추론 worker |
| `model/step_GRU/inference.py` | `weights.pt` 로드 및 `FlowClassifier` |

## 3. 캡처 위치

XDP와 tshark 실시간 캡처는 각 host가 edge switch로 내보내는 패킷을 edge switch의 host-facing port ingress에서 관측한다.

`main.py`는 실시간 캡처에서 모든 edge host-facing 포트에 대해 다음 형태의 capture point를 만든다.

```python
CapturePoint(
    node=edge_node,
    interface="%s-eth%d" % (edge_name, i),
    src_ips=(host_ip,),
)
```

그 다음 `NodeXdpPacketCapturer` 또는 `NodeTsharkOnlinePacketCapturer`가 edge switch namespace 안에서 실시간 캡처 스크립트를 실행한다.

## 4. XDP event 필드

XDP/BPF 프로그램은 TCP 패킷만 대상으로 다음 값을 user-space perf event로 보낸다.

```text
ts_ns
ifindex
frame_len
saddr
daddr
sport
dport
ip_len
ip_hdr_len
ttl
dscp
ecn
tcp_len
tcp_hdr_len
seq
ack_seq
window
tcp_flags
payload_prefix_len
payload_prefix[32]
```

payload 전체는 복사하지 않는다. TrafficGenerator metadata 식별에 필요한 payload 앞부분만 최대 32B 복사한다.

## 5. TrafficGenerator metadata

TrafficGenerator는 logical request/response payload 앞에 20B metadata를 붙인다.

```text
flow_id
size_bytes
tos
rate_mbps
direction_value
```

Python에서는 little-endian unsigned int 5개로 해석한다.

```python
flow_id, size_bytes, tos, rate_mbps, direction_value = struct.unpack(
    "<IIIII",
    payload_prefix[:20],
)
```

방향 값:

```text
0 -> src_to_dst
1 -> dst_to_src
```

`flow_id == 0`은 connection 종료 신호로 보고 logical flow 시작으로 사용하지 않는다.

## 6. Logical flow 매칭

`TrafficGeneratorOnlineFlowCache`는 TCP 5-tuple만으로 flow를 나누지 않는다. TrafficGenerator가 persistent TCP connection을 재사용하기 때문에, 같은 connection 안에서도 여러 logical flow가 이어질 수 있다.

처리 순서:

```text
1. server_port=5001 기준 방향 추론
   - dst_port == 5001 -> src_to_dst
   - src_port == 5001 -> dst_to_src

2. payload_prefix에서 TG metadata 파싱

3. metadata direction과 실제 port 방향이 일치하면 새 logical flow 시작

4. connection state key 생성
   - (client_ip, client_port, server_ip, server_port, direction)

5. 실시간 FlowCache key 생성
   - (client_ip, client_port, server_ip, server_port, flow_id, direction)

6. tcp_len <= 0인 ACK-only packet은 online model feature에 넣지 않음

7. payload packet이 feature_packet_count개 모이면 ready
```

현재 온라인 모델 입력은 payload packet 중심이다.

## 7. FlowCache 상태

실시간 FlowCache key:

```text
(client_ip, client_port, server_ip, server_port, flow_id, direction)
```

상태 전이:

```text
default  -> 아직 Redis로 보내지 않음
pending  -> Redis Stream으로 보냈고 결과 대기 중
elephant -> classifier 결과 반영 완료
mice     -> classifier 결과 반영 완료
```

ready entry를 Redis Stream으로 보낼 때 `pending`으로 바꿔 중복 publish를 막는다. worker가 `flow_results`로 결과를 publish하면 `RedisResultSubscriber`가 이를 받아 같은 FlowCache entry에 `elephant` 또는 `mice`를 반영한다.

## 8. Feature 구조

Redis request의 핵심은 `x`와 `seq_len`이다.

```text
x shape = [feature_packet_count, feature_dim]
feature_dim = 18
```

컬럼 순서:

```text
[
  direction,
  frame_len,
  ip_len,
  ip_hdr_len,
  ip_ttl,
  ip_dscp,
  ip_ecn,
  tcp_len,
  tcp_hdr_len,
  tcp_syn,
  tcp_ack,
  tcp_psh,
  tcp_fin,
  tcp_rst,
  tcp_window_size,
  iat_us,
  elapsed_us,
  cum_payload_bytes
]
```

`build_online_flow_request()`는 `OnlineFlowEntry`를 학습/추론 feature와 같은 형식으로 변환한다.

```text
1. OnlineFlowEntry.packets에서 tcp_len > 0인 packet만 사용
2. 앞 feature_packet_count개 packet 선택
3. packet 수가 부족하면 padding
4. x, seq_len, feature_names 생성
```

`seq_len`은 실제 관측된 유효 payload packet 수이다.

## 9. Redis request payload

`flow_features` Stream의 `payload` field에는 다음 형태의 JSON 문자열이 들어간다.

```json
{
  "online_flow_key": {
    "client_ip": "10.0.0.1",
    "client_port": 40000,
    "server_ip": "10.2.0.1",
    "server_port": 5001,
    "flow_id": 123,
    "direction": "dst_to_src"
  },
  "logical_flow_id": "10.0.0.1:40000->10.2.0.1:5001:123:dst_to_src",
  "feature_names": ["direction", "frame_len", "..."],
  "x": [
    [1, 1514, 1500, 20, 64, 0, 0, 1448, 32, 0, 1, 1, 0, 0, 1152, 0, 0, 1448],
    [1, 1514, 1500, 20, 64, 0, 0, 1448, 32, 0, 1, 1, 0, 0, 1152, 34, 34, 2896]
  ],
  "seq_len": 10,
  "max_packet_count": 10,
  "observed_directional_payload_bytes": 14480,
  "producer_metrics": {
    "feature_ready_wall_ns": 123456789,
    "capture_mode": "xdp",
    "first_packet_ts_us": 123,
    "last_packet_ts_us": 456,
    "feature_packet_count_observed": 10
  },
  "run_id": "example_run"
}
```

worker는 `payload` 안의 `x`, `feature_names`, `seq_len`을 추론에 사용하고, 방향 정보는 `online_flow_key`에서 읽는다.

## 10. Redis Stream field

`RedisStreamProducer.publish()`는 request 전체를 `payload`로 넣고, 조회/분석에 자주 쓰는 값은 별도 field로 복사한다.

`flow_features` field:

```text
online_flow_key
logical_flow_id
payload
publish_start_wall_ns
run_id
capture_mode
feature_ready_wall_ns
first_packet_ts_us
last_packet_ts_us
```

latency 보조 Stream:

```text
flow_features:latency
```

`flow_features:latency` field:

```text
source_stream_id
publish_end_wall_ns
publish_start_wall_ns
feature_ready_wall_ns
run_id
capture_mode
logical_flow_id
```

Redis Stream ID는 millisecond 정밀도라 sub-ms latency 분석에는 `publish_start_wall_ns`, `publish_end_wall_ns`를 사용한다.

## 11. Worker 결과 구조

`model.step_GRU.stream_worker`는 `flow_features`를 읽고 다음 결과를 만든다.

```json
{
  "online_flow_key": {
    "client_ip": "10.0.1.2",
    "client_port": 43122,
    "server_ip": "10.2.0.1",
    "server_port": 5001,
    "flow_id": 1,
    "direction": "dst_to_src"
  },
  "logical_flow_id": "10.0.1.2:43122->10.2.0.1:5001:1:dst_to_src",
  "run_id": "vl2_cuda_50",
  "inference_ms": 0.474,
  "score": 0.9993,
  "predicted_label": "elephant",
  "threshold": 0.5,
  "exit_step": 2,
  "stream_id": "1779971288013-0"
}
```

이 결과는 두 곳으로 나간다.

```text
1. Redis Pub/Sub channel: flow_results
2. --result-log JSONL 파일
```

`tg_xdp_capture.py`는 `flow_results`를 백그라운드로 구독하고, `online_flow_key`가 일치하는 실시간 FlowCache entry를 `elephant` 또는 `mice`로 갱신한다.

## 12. 운영 기준

실행 명령, Redis 실행/초기화, worker 실행, `main.py` 실행, 결과 확인 명령은 `README.md`를 기준으로 관리한다. 이 문서에는 실시간 경로의 payload 구조, 상태 전이, latency 필드 의미만 남긴다.

XDP 로그 summary의 주요 값:

해석:

| 값 | 의미 |
|---|---|
| `metadata` | TG metadata를 발견한 packet 수 |
| `matched` | logical flow에 매칭된 packet 수 |
| `ready` | feature_packet_count를 채운 flow 수 |
| `published` | Redis Stream으로 보낸 request 수 |
| `classified` | Pub/Sub 결과를 받아 FlowCache에 반영한 수 |

## 13. End-to-end 실시간 동작 증명

Redis 온라인 경로에서 `main.py`를 사용하면 기본적으로 다음 파일이 생성된다.

```text
runs/mininet_<run_id>/results/online_classification_latency.jsonl
```

이 파일은 `tg_xdp_capture.py`가 Pub/Sub 결과를 받아 `RealtimeFlowCache.apply_classification_result()`까지 성공한 뒤에만 append한다. 따라서 각 JSONL row는 다음 경로가 실제로 완료되었다는 증거다.

```text
XDP feature ready
  -> Redis Stream flow_features
  -> step_GRU worker
  -> Redis Pub/Sub flow_results
  -> RedisResultSubscriber
  -> RealtimeFlowCache status/model_score/classification_result 반영
```

주요 latency 필드:

| 필드 | 의미 |
| --- | --- |
| `ready_to_cache_updated_ms` | feature ready부터 FlowCache 반영 완료까지 |
| `ready_to_worker_received_ms` | feature ready부터 worker가 Redis Stream 항목을 받은 시점까지 |
| `worker_received_to_done_ms` | worker 수신부터 모델 추론 완료까지 |
| `worker_done_to_publish_ms` | worker 추론 완료부터 Pub/Sub publish 시작까지 |
| `pubsub_publish_to_subscriber_ms` | worker publish 시작부터 subscriber 수신까지 |
| `subscriber_to_cache_updated_ms` | subscriber 수신부터 FlowCache 반영 완료까지 |

요약 실행 명령은 `README.md`의 End-to-end 실시간 동작 증명 섹션을 따른다.

## 14. 주의점

- 기본적으로 worker는 상시 실행 프로세스다. 실험용 자동 종료가 필요하면 `--idle-timeout-sec 10`처럼 지정한다. 이 옵션은 최소 한 건 처리한 뒤 새 메시지가 없는 시간이 지정값을 넘으면 worker를 종료한다.
- CUDA 첫 추론은 context 초기화 때문에 `inference_ms`가 크게 튈 수 있다.
- steady-state 추론 시간은 첫 결과를 제외한 p50/p95로 해석한다.
- `main.py N`의 `N`은 각 source client가 생성하는 요청 수이다.
- 결과 수는 전체 요청 수가 아니라 `publish_direction`과 `feature_packet_count` 조건을 만족한 ready flow 수이다.
- FB + seq10은 ready flow가 0개일 수 있다. FB는 seq3로 먼저 확인한다.
- VL2는 큰 flow tail이 있어 seq10 실험이 가능하지만 실행 시간이 길어질 수 있다.
