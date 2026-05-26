# XDP 기반 온라인 패킷 캡처 진행 정리

이 문서는 기존 `tshark` 기반 캡처/분석 흐름을 XDP 기반 온라인 캡처 흐름으로 바꾸기 위해 지금까지 확인하고 구현한 내용을 정리한다.

## 1. 목표

최종 목표는 Mininet에서 흐르는 TrafficGenerator TCP 트래픽을 실시간으로 관측하고, flow별 초기 패킷 feature가 일정 개수 모이면 모델 추론으로 넘기는 온라인 환경을 만드는 것이다.

큰 흐름은 다음과 같다.

```text
Mininet live traffic
    -> XDP/eBPF packet capture
    -> TrafficGenerator metadata 기반 flow 식별
    -> FlowCache에 초기 N개 패킷 저장
    -> Redis를 통해 model worker로 비동기 추론 요청
    -> 추론 결과를 flow state에 반영
```

현재까지는 Redis/model worker 연결 전 단계인 XDP 캡처, flow 식별, `FlowCache` ready 생성, 유실 검증까지 진행했다.

## 2. 기존 구조

기존 실행 흐름은 `main.py` 기준으로 다음과 같다.

```text
Ryu controller 시작
    -> Fat-tree Mininet topology 생성
    -> edge switch host-facing interface에서 tshark 실행
    -> TrafficGenerator 트래픽 생성
    -> pcap 저장
    -> pipeline에서 pcap + metadata를 읽어 dataset/feature 생성
```

기존 feature 추출의 핵심 파일은 다음과 같다.

- `pipeline/packet_loader.py`
  - `tshark -Y tcp`로 TCP 패킷을 읽는다.
  - `tcp.len > 0` 필터가 없으므로 ACK-only 패킷도 포함된다.
- `pipeline/flow_cache.py`
  - `(src_index, flow_id, direction)`을 key로 flow를 구분한다.
  - flow별 초기 `feature_packet_count`개 패킷을 저장한다.
  - flow 상태는 `default`, `pending`, `elephant`, `mice`로 관리한다.
  - 10개 미만이면 `default`를 유지하고, ready가 되어 Redis/model 요청을 보낸 뒤 `pending`으로 전환한다.
- `pipeline/dataset_builder.py`
  - 모델 입력 feature 18개를 구성한다.

모델 입력 feature는 다음 18개다.

```text
direction
frame_len
ip_len
ip_hdr_len
ip_ttl
ip_dscp
ip_ecn
tcp_len
tcp_hdr_len
tcp_syn
tcp_ack
tcp_psh
tcp_fin
tcp_rst
tcp_window_size
iat_us
elapsed_us
cum_payload_bytes
```

## 3. 5-tuple만으로 flow를 구분할 수 없는 이유

TrafficGenerator는 persistent TCP connection을 사용한다.

즉 하나의 TCP 연결에서 여러 request/flow가 이어서 전송될 수 있다. 이 경우 다음 5-tuple은 그대로 유지된다.

```text
src_ip, src_port, dst_ip, dst_port, protocol
```

따라서 5-tuple만 key로 쓰면 서로 다른 TrafficGenerator flow가 하나의 flow로 합쳐질 수 있다.

기존 tshark 기반 오프라인 파이프라인은 이 문제를 `flows_*_meta.csv`로 해결했다.

```text
pcap packet
    -> 5-tuple으로 meta.csv 후보 검색
    -> packet timestamp가 start_time_us ~ stop_time_us 범위에 들어가는 request 선택
    -> (src_index, flow_id, direction) 복원
```

즉 기존 tshark 파이프라인은 pcap payload 안의 metadata를 직접 읽은 것이 아니라, TrafficGenerator client가 별도로 저장한 meta.csv를 기준으로 packet을 request에 매칭했다.

반면 XDP 온라인 파이프라인에서는 아직 flow가 끝나지 않았고 meta.csv도 없기 때문에, TrafficGenerator payload 앞에 들어가는 metadata를 직접 읽는 방식을 사용한다.

TrafficGenerator metadata는 payload prefix에 들어 있으며, 현재 구조는 unsigned int 5개다.

```text
id, size, tos, rate, direction
```

서버는 request metadata를 읽고, 응답 방향에서는 `direction`을 `dst_to_src`로 바꿔 다시 payload 앞에 붙인다. 그래서 meta.csv 없이도 온라인 패킷에서 `(src_index, flow_id, direction)`을 복원할 수 있다.

tshark로도 원리적으로 같은 일을 할 수는 있다. 다만 현재 `pipeline/packet_loader.py`는 `tcp.payload`를 읽지 않고 header/length/flag 계열 field만 읽는다. 따라서 기존 tshark 코드가 payload metadata를 파싱한 것은 아니며, tshark에서 이 방식을 쓰려면 `tcp.payload` 또는 raw packet bytes를 추가로 읽고, connection별 state를 유지하는 별도 로직이 필요하다.

## 4. XDP 캡처 방식

새로 추가한 XDP 캡처는 BCC 기반이다.

구현 파일:

- `xdp/tg_xdp_capture.py`
  - XDP program을 interface에 attach한다.
  - Ethernet/IP/TCP header를 파싱한다.
  - TCP payload prefix를 userspace로 보낸다.
  - ACK-only 패킷도 event로 보낸다.
  - 종료 시 summary와 kernel counter를 출력한다.

현재 출력 예시는 다음과 같다.

```text
[summary] events=... metadata=... matched=... ready=... key_errors=...
[kernel] hook=... eth_ip=... ip_tcp=... tcp_header=... tcp_payload=... submit=... tcp_ack_only=...
```

counter 의미:

- `hook`: XDP hook에 들어온 패킷 수
- `eth_ip`: Ethernet + IPv4까지 확인된 패킷 수
- `ip_tcp`: TCP로 확인된 패킷 수
- `tcp_header`: TCP header 파싱이 완료된 패킷 수
- `tcp_payload`: TCP payload가 있는 패킷 수
- `submit`: userspace perf event로 제출한 패킷 수
- `tcp_ack_only`: `tcp_len == 0`인 TCP 패킷 수

## 5. 캡처 위치 변경

처음에는 edge switch의 host-facing OVS interface에 XDP를 attach했다.

결과:

```text
events=0
```

즉 attach는 되었지만 XDP hook에서 실제 트래픽을 보지 못했다. OVS datapath/interface 특성상 이 위치는 XDP 실험 지점으로 적합하지 않았다.

이후 캡처 지점을 각 Mininet host namespace의 `h*-eth0`로 변경했다.

구현 파일:

- `network/packet_capture.py`
  - `NodeXdpPacketCapturer` 추가
  - Mininet host별로 `node.popen()`을 사용해 각 namespace 안에서 XDP 캡처 프로세스를 실행한다.
  - 로그는 host별 파일로 저장한다.

예시 로그 위치:

```text
runs/mininet_<run_id>/captured_packet/logs/h1.xdp.stdout.txt
runs/mininet_<run_id>/captured_packet/logs/h1.xdp.stderr.txt
```

현재 `main.py`에서는 `--capture-mode xdp`일 때 host namespace 방식이 사용된다.

## 6. FlowCache 연결

XDP event를 기존 `FlowCache` 형태로 연결하기 위해 새 파일을 추가했다.

구현 파일:

- `pipeline/online_tg_flow_cache.py`

주요 역할:

- XDP event를 Python object로 변환한다.
- payload prefix에서 TrafficGenerator metadata를 파싱한다.
- persistent connection 안에서 flow boundary를 추적한다.
- `(src_index, flow_id, direction)` key로 기존 `FlowCache`에 packet feature를 넣는다.
- flow별 패킷 수가 `feature_packet_count`에 도달하면 ready 상태를 만든다.

이 방식 덕분에 dataset에서 쓰던 flow 식별 구조와 온라인 캡처 구조가 같은 key를 공유한다.

```text
src_index + flow_id + direction
```

## 7. ACK-only 패킷 처리 결정

기존 학습 데이터는 `tshark -Y tcp` 기반이라 ACK-only 패킷을 포함한다.

확인 결과:

- `packet_loader.py`에는 `tcp.len > 0` 필터가 없다.
- dataset에도 `tcp_len == 0` 행이 존재한다.
- 일부 seq10 샘플에서는 첫 패킷이 ACK-only인 경우도 있다.

따라서 온라인 XDP 캡처도 학습 데이터와 맞추기 위해 ACK-only 패킷을 포함하기로 결정했다.

정책:

- metadata를 아직 만나지 못한 ACK-only 패킷은 flow를 알 수 없으므로 버린다.
- metadata를 만나 flow가 active 상태가 된 뒤의 ACK-only 패킷은 해당 flow에 붙인다.
- `tcp_len == 0`이므로 `cum_payload_bytes`는 증가하지 않는다.

추론 시점에 ACK-only를 제외하는 방법도 검토했지만, 학습/추론 feature 분포가 달라지는 문제가 있어 현재는 포함 유지로 정했다.

## 8. main.py 실행 옵션

`main.py`에 다음 옵션을 추가했다.

```text
--capture-mode {tshark,xdp,xdp-verify,none}
--xdp-mode {skb,native,hw}
--feature-packet-count N
```

기본값은 기존 호환을 위해 `tshark`다.

외부 기준 유실 검증을 위해 `--capture-mode xdp-verify`도 추가했다. 이 모드는 각 Mininet host namespace의 `h*-eth0`에서 XDP와 `tshark`를 동시에 실행한다. XDP는 ingress hook만 보므로, 검증용 `tshark`는 같은 host interface에서 `tcp and dst host <host_ip>` 필터로 ingress TCP만 pcap에 저장한다.

XDP smoke 실행 예시:

```bash
sudo python3 main.py 1 --run-id xdp_smoke --capture-mode xdp --feature-packet-count 2
```

패킷 10개 기준 검증 실행 예시:

```bash
sudo python3 main.py 100 --run-id xdp_loss_seq10 --capture-mode xdp --feature-packet-count 10
```

XDP와 tshark 동시 검증 실행 예시:

```bash
sudo python3 main.py 100 --run-id xdp_verify_seq10 --capture-mode xdp-verify --feature-packet-count 10
python3 analyze/verify_xdp_tshark_capture.py runs/mininet_xdp_verify_seq10
```

로그 확인 명령:

```bash
find runs/mininet_xdp_loss_seq10/captured_packet/logs -name '*.xdp.stdout.txt' -exec grep -H '\[summary\]\|\[kernel\]\|\[ready\]' {} \;
```

## 9. 구현 중 해결한 문제

XDP/BPF verifier 관련 문제:

- packet memory 직접 접근에서 verifier 오류가 발생했다.
- `bpf_xdp_load_bytes()` helper를 사용하도록 수정했다.
- BCC가 제공하는 helper를 중복 선언하면 컴파일 오류가 나서 제거했다.

TCP header parsing 문제:

- `sizeof(*tcp)` 기준 접근에서 verifier가 안전성을 인정하지 못했다.
- raw byte 기반으로 IP/TCP header length를 계산하도록 수정했다.
- TCP header length는 20~60 byte 범위만 허용한다.

캡처 지점 문제:

- edge switch OVS port에서는 `events=0`이었다.
- host namespace의 `h*-eth0`에서는 XDP event가 정상 발생했다.

ACK-only 누락 문제:

- 처음에는 payload가 있는 패킷 위주로 처리되어 기존 tshark 데이터와 달라질 수 있었다.
- ACK-only도 event로 올리고 active flow에 붙이도록 수정했다.

## 10. 검증 결과

### 단위 테스트

다음 검증을 통과했다.

```bash
env PYTHONPYCACHEPREFIX=/tmp/capstone_pycache python3 -m py_compile main.py network/packet_capture.py xdp/tg_xdp_capture.py pipeline/online_tg_flow_cache.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

테스트 내용:

- TrafficGenerator metadata 파싱
- persistent connection에서 flow_id 1, 2가 분리되는지 확인
- `src_to_dst` metadata packet이 flow entry를 만드는지 확인
- ACK-only packet이 active flow에 붙고 payload byte는 늘리지 않는지 확인

### smoke run

`feature_packet_count=2` 기준으로 host namespace XDP 캡처가 동작했다.

예시:

```text
[ready] src_index=0 flow_id=1 direction=dst_to_src packets=2 payload_bytes=1468
[summary] events=83 metadata=1 matched=3 ready=1 key_errors=0 directions(src_to_dst=0,dst_to_src=83,unknown=0)
[kernel] hook=210 eth_ip=209 ip_tcp=208 tcp_header=208 tcp_payload=83 submit=83
```

즉 XDP hook, userspace event 전달, metadata 파싱, FlowCache ready 생성이 모두 이어졌다.

### 100-flow, seq10 기준 유실 검증

실행:

```bash
sudo python3 main.py 100 --run-id xdp_loss_seq10 --capture-mode xdp --feature-packet-count 10
```

집계 결과:

```text
events   = 710,311
metadata = 1,600
matched  = 706,832
ready    = 549
key_errors = 0

directions:
  src_to_dst = 240,845
  dst_to_src = 469,466
  unknown    = 0

kernel:
  hook         = 710,428
  eth_ip       = 710,313
  ip_tcp       = 710,311
  tcp_header   = 710,311
  tcp_payload  = 469,621
  submit       = 710,311
  tcp_ack_only = 240,690
```

중요 확인:

- `submit == events`
  - kernel에서 userspace로 제출한 event 수와 Python에서 받은 event 수가 같다.
  - 현재 관측 기준으로 perf event 전달 유실은 없다.
- `metadata == 1,600`
  - 8개 source host * 100 flows = 800 parent flows
  - 각 flow는 `src_to_dst`, `dst_to_src` metadata가 있으므로 기대값은 1,600이다.
  - 기대값과 일치한다.
- `key_errors == 0`
  - flow key 생성/metadata 매칭 오류는 관측되지 않았다.
- `unknown == 0`
  - 방향을 모르는 packet event는 관측되지 않았다.

`dst_to_src` ready 검증:

```text
size_bytes >= 11,580 인 flow 수 = 249
dst_to_src ready flow 수       = 249
missing eligible flow          = 0
extra ready below threshold    = 0
```

즉 `feature_packet_count=10` 기준으로 10개 패킷을 만들 수 있는 `dst_to_src` flow는 모두 ready가 되었다.

현재 검증 기준에서는 XDP 캡처에서 모델 입력 대상 flow를 만드는 과정의 패킷 유실은 관측되지 않았다.

## 11. 현재 해석

현재 상태에서 말할 수 있는 것은 다음과 같다.

- XDP attach 위치는 edge switch OVS port가 아니라 host namespace `h*-eth0`가 맞다.
- TrafficGenerator persistent connection 때문에 5-tuple만으로는 flow 식별이 부족하다.
- payload metadata를 이용하면 meta.csv 없이 온라인에서 flow_id와 direction을 복원할 수 있다.
- 기존 학습 데이터와 맞추려면 ACK-only 패킷은 포함하는 것이 현재는 안전하다.
- `feature_packet_count=10` 기준으로 `dst_to_src`에서 10개 패킷 이상 만들 수 있는 flow는 모두 ready가 되었다.
- 현재 관측에서는 XDP kernel submit과 userspace events 수가 같아 event 전달 유실은 보이지 않는다.

다만 `src_to_dst`에서도 ACK-only가 누적되어 ready가 생길 수 있다. 현재 모델/데이터셋 목표가 `dst_to_src` 중심이면 Redis/model로 보내는 단계에서 `direction == "dst_to_src"`만 필터링하는 것이 좋다.

## 12. 남은 작업

온라인 추론 연결은 다음 구조로 진행한다.

```text
Client / XDP capture
    -> Redis Stream: flow_features
    -> Classifier worker consumer group
    -> Redis Pub/Sub: flow_results
    -> Client / controller-side result subscriber
```

요청 방향은 Redis Stream을 사용한다. Stream은 메시지가 Redis에 남고 consumer group으로 나눠 읽을 수 있어 classifier scale-out과 장애 복구에 유리하다.

응답 방향은 Redis Publish/Subscribe를 사용한다. 추론 결과는 최신성이 중요하고 client가 빠르게 반응해야 하므로, Stream보다 가벼운 Pub/Sub channel로 전달한다.

현재 추가된 구현:

- `pipeline/online_request.py`
  - ready `FlowEntry`를 모델 학습 데이터와 같은 `feature_names`, `x`, `seq_len` 형태의 온라인 요청 payload로 변환한다.
- `pipeline/redis_transport.py`
  - Redis Stream producer를 제공한다.
  - `XADD flow_features payload=...` 형태로 요청을 적재한다.
- `pipeline/flow_cache.py`
  - `default -> pending -> elephant/mice` 상태 모델을 사용한다.
  - `is_ready()`는 `status == "default"`이고 초기 패킷 수가 `feature_packet_count` 이상일 때만 true다.
  - pending 이후에는 같은 entry에 추가 패킷을 누적하지 않는다.
- `xdp/tg_xdp_capture.py`
  - `--redis-url`이 주어지면 ready flow를 Redis Stream에 전송한다.
  - Redis 전송 후 해당 `FlowEntry`를 `pending`으로 전환한다.
  - 기본 전송 방향은 `dst_to_src`다.
- `model/gru_stream_worker.py`
  - Redis Stream consumer group으로 요청을 읽는다.
  - GRU checkpoint와 scaler로 추론한다.
  - 결과를 Redis Pub/Sub channel로 publish한다.
- `pipeline/redis_result_subscriber.py`
  - client/controller 쪽에서 `flow_results` channel을 구독해 추론 결과를 받을 때 사용할 수 있는 subscriber 유틸이다.

실행 예시:

```bash
python3 model/gru_stream_worker.py \
  --redis-url redis://127.0.0.1:6379/0 \
  --stream flow_features \
  --group gru_classifiers \
  --consumer classifier-1 \
  --response-channel flow_results \
  --model-path runs/gru/mininet_pretrain/best.pt \
  --scaler-path runs/gru/mininet_pretrain/scaler.json
```

```bash
sudo python3 main.py 100 \
  --run-id xdp_stream_seq10 \
  --capture-mode xdp \
  --feature-packet-count 10 \
  --redis-url redis://127.0.0.1:6379/0 \
  --redis-stream flow_features \
  --publish-direction dst_to_src
```

우선순위:

1. Redis 서버를 띄운 뒤 Stream 적재/worker 소비/PubSub 응답 end-to-end 확인
2. client/controller 쪽 Pub/Sub subscriber를 flow state 갱신 로직에 연결
3. worker consumer group pending message 재처리 정책 정리
4. `xdp-verify` 모드로 tshark 동시 캡처와 XDP 캡처를 비교해 외부 기준의 유실 검증 실행하기

현재까지의 결과는 “XDP로 online FlowCache 입력을 만들 수 있다”는 단계까지 도달한 상태다. 다음 작업부터는 이 ready entry를 모델 추론 파이프라인에 연결하면 된다.

## 13. Redis 도착 latency 계측 추가

XDP와 `tshark` 기반 실시간 캡처의 지연 오버헤드를 비교하기 위해 Redis Stream 적재 지점에 latency 계측을 추가했다.

계측 대상은 `flow_features` Stream이다. 실제 feature request는 기존처럼 `flow_features`에 `XADD`하고, `XADD` 호출 완료 시각 등 세부 latency 정보는 별도 보조 Stream인 `flow_features:latency`에 기록한다.

추가된 주요 파일:

- `pipeline/online_request.py`
  - ready `FlowEntry`를 Redis payload로 변환한다.
  - `producer_metrics`에 `feature_ready_wall_ns`, `capture_mode`, `first_packet_ts_us`, `last_packet_ts_us`, `feature_packet_count_observed`를 넣는다.
- `pipeline/redis_transport.py`
  - `XADD flow_features` 전후의 wall-clock timestamp를 측정한다.
  - 보조 Stream `flow_features:latency`에 `publish_start_wall_ns`, `publish_end_wall_ns`, `feature_ready_wall_ns`, `source_stream_id`를 기록한다.
- `analyze/redis_stream_latency.py`
  - `flow_features`와 `flow_features:latency`를 함께 읽어 p50, p95, p99, max, mean을 계산한다.

Redis Stream ID는 millisecond 정밀도라 sub-ms latency 분석에는 부정확하다. 실제로 `ready_to_redis_id`, `publish_to_redis_id`는 음수가 나올 수 있다. 따라서 비교에는 `publish_end_wall_ns` 기준 지표를 사용한다.

주요 지표 의미:

- `ready_to_xadd_done`
  - feature ready 시점부터 Redis `XADD` 완료까지의 시간이다.
  - Redis request emission 단계의 producer-side latency를 비교하는 주 지표다.
- `xadd_duration`
  - Redis `XADD` 호출 시작부터 완료까지의 시간이다.
  - Redis write 자체와 publisher thread scheduling 영향이 포함된다.
- `first_packet_to_xadd_done`
  - feature에 포함된 첫 패킷 timestamp부터 Redis `XADD` 완료까지의 시간이다.
  - flow 관측 시작 기준 end-to-end 지연을 본다.
- `last_packet_to_xadd_done`
  - feature를 만들기 위한 마지막 패킷 timestamp부터 Redis `XADD` 완료까지의 시간이다.
  - 캡처/파싱/큐 처리/backlog를 포함한 online pipeline 지연 비교에 가장 직접적인 지표다.
- `ready_to_redis_id`, `publish_to_redis_id`
  - Redis Stream ID timestamp 기준 참고 지표다.
  - Redis ID가 ms 단위라 sub-ms 비교에는 사용하지 않는다.

## 14. Redis UDS 사용 이유와 TCP 주의점

Mininet host namespace 안에서 실행되는 XDP publisher는 `127.0.0.1:6379`를 host OS의 Redis가 아니라 각 Mininet host namespace 자신의 loopback으로 본다. 따라서 TCP URL `redis://127.0.0.1:6379/0`를 넘기면 다음 오류가 발생한다.

```text
redis.exceptions.ConnectionError: Error 111 connecting to 127.0.0.1:6379. Connection refused.
```

이를 피하기 위해 현재 비교 실험은 Unix Domain Socket(UDS)을 사용한다.

Redis UDS 실행:

```bash
redis-server \
  --port 0 \
  --unixsocket /tmp/capstone-redis.sock \
  --unixsocketperm 777 \
  --daemonize yes
```

Stream 초기화:

```bash
redis-cli -s /tmp/capstone-redis.sock DEL flow_features flow_features:latency
```

Redis가 떠 있는지 확인:

```bash
redis-cli -s /tmp/capstone-redis.sock PING
```

정상이라면 다음처럼 출력된다.

```text
PONG
```

실험/분석에서 사용하는 URL:

```text
unix:///tmp/capstone-redis.sock?db=0
```

`redis+unix://`는 현재 `redis-py`에서 지원하지 않는 scheme이므로 사용하지 않는다.

### Redis Stream 적재 여부를 눈으로 확인하는 명령

실험을 시작하기 전에 Stream을 비운다.

```bash
redis-cli -s /tmp/capstone-redis.sock DEL flow_features flow_features:latency
```

다른 터미널에서 Stream 길이를 1초마다 확인한다.

```bash
watch -n 1 'redis-cli -s /tmp/capstone-redis.sock XLEN flow_features; redis-cli -s /tmp/capstone-redis.sock XLEN flow_features:latency'
```

의미:

- `flow_features`: 모델 worker가 읽어 갈 실제 online feature request Stream
- `flow_features:latency`: `XADD` 시작/완료 시각 등을 담은 latency 분석용 보조 Stream

실험 중 위 숫자가 증가하면 Redis Stream에 request가 정상적으로 올라가고 있다는 뜻이다.

최근에 올라온 request 3개를 확인한다.

```bash
redis-cli -s /tmp/capstone-redis.sock XREVRANGE flow_features + - COUNT 3
```

최근 latency entry 3개를 확인한다.

```bash
redis-cli -s /tmp/capstone-redis.sock XREVRANGE flow_features:latency + - COUNT 3
```

payload를 문자열로 조금 더 읽기 쉽게 보고 싶으면 `--raw`를 붙인다.

```bash
redis-cli -s /tmp/capstone-redis.sock --raw XREVRANGE flow_features + - COUNT 1
```

Stream 전체 개수만 빠르게 확인할 때는 다음 명령을 쓴다.

```bash
redis-cli -s /tmp/capstone-redis.sock XLEN flow_features
redis-cli -s /tmp/capstone-redis.sock XLEN flow_features:latency
```

실험 후 latency 요약은 분석 스크립트로 확인한다.

```bash
python3 analyze/redis_stream_latency.py \
  --redis-url 'unix:///tmp/capstone-redis.sock?db=0' \
  --stream flow_features \
  --run-id <RUN_ID> \
  --capture-mode <xdp|tshark>
```

## 15. XDP latency 계측 보강

초기 XDP 구현은 packet timestamp로 `bpf_ktime_get_ns()`를 사용했다. 이 값은 epoch 시간이 아니라 kernel monotonic 계열 timestamp이므로 Redis wall-clock timestamp와 직접 비교할 수 없었다.

이를 해결하기 위해 XDP userspace 프로그램 시작 시점에 다음 offset을 계산한다.

```python
monotonic_to_epoch_offset_ns = time.time_ns() - time.monotonic_ns()
```

그리고 각 XDP event의 kernel timestamp를 epoch timestamp로 변환한다.

```python
epoch_ts_ns = raw.ts_ns + monotonic_to_epoch_offset_ns
epoch_ts_us = epoch_ts_ns // 1000
```

구현 변경:

- `pipeline/online_tg_flow_cache.py`
  - `XdpPacketEvent`에 `epoch_ts_us` 필드를 추가했다.
  - `PacketRecord`에는 기존 `ts_us`는 그대로 두고, 동적으로 `epoch_ts_us` 속성을 붙인다.
  - 모델 feature 계산은 기존 monotonic `ts_us`를 사용하므로 `iat_us`, `elapsed_us` 계산은 깨지지 않는다.
- `pipeline/online_request.py`
  - `PacketRecord.epoch_ts_us`가 있으면 Redis payload의 `first_packet_ts_us`, `last_packet_ts_us`에 epoch 값을 사용한다.
  - 없으면 기존 `ts_us`를 사용한다. 이는 tshark 쪽 `frame.time_epoch`와 호환된다.
- `xdp/tg_xdp_capture.py`
  - `make_event()`에서 XDP kernel timestamp를 epoch timestamp로 변환해 `XdpPacketEvent`에 넣는다.

이제 XDP와 tshark 모두 `first_packet_to_xadd_done`, `last_packet_to_xadd_done`를 같은 epoch 기준으로 계산할 수 있다.

## 16. tshark 기반 live-traffic 비교 환경

`tshark` 기반 실시간 코드는 `live-traffic` 브랜치에 있으므로 현재 `feat/xdp` 작업트리와 섞지 않기 위해 별도 worktree를 만들었다.

```text
/tmp/capstone-live-traffic
```

추가된 변경:

- `main.py`
  - `--run-id`, `--feature-packet-count`, `--realtime`, `--redis-url`, `--redis-stream`, `--redis-stream-maxlen` 옵션을 추가했다.
- `realtime_pipeline.py`
  - `RealtimeFlowTable.process_packet()`에서 ready entry가 반환되면 Redis Stream에 publish한다.
  - `capture_mode="tshark"`로 기록한다.
- `pipeline/online_request.py`
  - live-traffic의 `FlowEntry` 형식에 맞춰 Redis payload를 생성한다.
- `pipeline/redis_transport.py`
  - XDP 쪽과 같은 `flow_features`, `flow_features:latency` 계측 Stream을 사용한다.
- `analyze/redis_stream_latency.py`
  - XDP 쪽과 같은 분석 스크립트를 복사했다.

tshark 비교 실행:

```bash
cd /tmp/capstone-live-traffic

redis-cli -s /tmp/capstone-redis.sock DEL flow_features flow_features:latency

sudo python3 main.py 100 \
  --run-id tshark_uds_100_dctcp \
  --realtime \
  --redis-url 'unix:///tmp/capstone-redis.sock?db=0' \
  --redis-stream flow_features
```

분석:

```bash
python3 analyze/redis_stream_latency.py \
  --redis-url 'unix:///tmp/capstone-redis.sock?db=0' \
  --stream flow_features \
  --run-id tshark_uds_100_dctcp \
  --capture-mode tshark
```

## 17. 현재 latency 측정 결과

DCTCP 조건에서 UDS Redis Stream으로 비교한 결과는 다음과 같다.

### XDP UDS DCTCP

초기 XDP 계측 결과:

```text
stream=flow_features samples=673 run_id=xdp_uds_100 capture_mode=xdp
ready_to_xadd_done       count=   673 min=0.084 p50=0.113 p95=0.152 p99=0.194 max=0.220 mean=0.118 ms
xadd_duration            count=   673 min=0.075 p50=0.101 p95=0.135 p99=0.163 max=0.196 mean=0.105 ms
```

이 결과는 feature ready 이후 Redis 적재 구간에서 XDP가 sub-ms tail latency를 유지함을 보여준다.

이 실험 당시에는 XDP packet timestamp가 epoch로 변환되지 않아 `first_packet_to_xadd_done`, `last_packet_to_xadd_done`는 아직 계산할 수 없었다. 이후 XDP epoch timestamp 변환을 추가했으므로, XDP는 다시 실행해야 end-to-end packet 기준 지표가 나온다.

XDP epoch timestamp 변환을 추가한 뒤 다시 실행한 결과:

```text
stream=flow_features samples=672 run_id=xdp_uds_100_dctcp_e2e capture_mode=xdp
ready_to_xadd_done        count=   672 min=0.071 p50=0.116 p95=0.162 p99=0.696 max=0.771 mean=0.128 ms
xadd_duration             count=   672 min=0.063 p50=0.103 p95=0.149 p99=0.684 max=0.759 mean=0.115 ms
first_packet_to_xadd_done count=   672 min=2.113 p50=4.777 p95=630.761 p99=1288.912 max=67675.909 mean=185.789 ms
last_packet_to_xadd_done  count=   672 min=0.124 p50=0.628 p95=1.148 p99=1.228 max=1.697 mean=0.651 ms
```

`first_packet_to_xadd_done`는 flow의 첫 feature packet 기준이므로 flow duration과 packet inter-arrival의 영향을 크게 받는다. 따라서 캡처 오버헤드 비교에는 `last_packet_to_xadd_done`가 더 직접적이다.

### tshark UDS DCTCP

live-traffic worktree에서 tshark realtime pipeline을 Redis에 연결해 측정한 결과:

```text
stream=flow_features samples=1380 run_id=tshark_uds_100_dctcp capture_mode=tshark
ready_to_xadd_done       count=  1380 min=0.166 p50=0.600 p95=3.335 p99=5.102 max=16.985 mean=0.998 ms
xadd_duration            count=  1380 min=0.113 p50=0.535 p95=3.275 p99=5.045 max=16.920 mean=0.932 ms
first_packet_to_redis    count=  1380 min=41.295 p50=354.341 p95=775.972 p99=947.774 max=2185.696 mean=389.969 ms
last_packet_to_redis     count=  1380 min=40.065 p50=334.808 p95=707.267 p99=898.423 max=1837.650 mean=360.758 ms
```

tshark의 `ready_to_xadd_done` p99는 5.102 ms로, 초기 XDP 결과의 0.194 ms보다 약 26.3배 크다.

```text
ready_to_xadd_done p99:
  XDP    = 0.194 ms
  tshark = 5.102 ms
```

다만 `first_packet_to_redis`, `last_packet_to_redis`는 당시 XDP에는 같은 epoch packet timestamp가 없어 직접 비교하지 않았다. tshark에서는 `frame.time_epoch`가 epoch timestamp이므로 계산 가능했고, 이 값은 tshark stdout buffering, tshark field extraction, Python readline/split/parse, queue 처리, flow table 처리, Redis publish 지연이 모두 합쳐진 end-to-end 지연 신호로 해석한다.

XDP와 같은 분석 스크립트 기준으로 tshark도 다시 실행한 결과:

```text
stream=flow_features samples=1349 run_id=tshark_uds_100_dctcp_e2e capture_mode=tshark
ready_to_xadd_done        count=  1349 min=0.166 p50=0.564 p95=5.251 p99=9.945 max=20.528 mean=1.174 ms
xadd_duration             count=  1349 min=0.115 p50=0.504 p95=5.184 p99=9.884 max=20.470 mean=1.111 ms
first_packet_to_xadd_done count=  1349 min=64.560 p50=344.011 p95=722.996 p99=839.196 max=1045.607 mean=365.996 ms
last_packet_to_xadd_done  count=  1349 min=63.331 p50=336.444 p95=646.592 p99=780.808 max=1045.607 mean=348.860 ms
```

같은 epoch timestamp와 같은 Redis `XADD` 완료 기준으로 비교할 수 있게 되었다.

## 18. 현재 해석과 주장 범위

현재 결과로 확실히 말할 수 있는 주장:

```text
동일한 Redis UDS Stream 환경에서 마지막 feature packet 관측 이후 Redis request emission 완료까지의 지연을 비교한 결과,
XDP는 p99 1.228 ms, tshark는 p99 780.808 ms로 측정되었다.
따라서 캡처 이후 online request emission까지의 end-to-end tail latency는 XDP가 tshark보다 약 636배 낮다.
```

주의: 이 비교는 완전히 동일한 물리적 캡처 위치에서 capture backend만 바꾼 microbenchmark가 아니다. 현재 구현상 XDP는 각 Mininet host namespace의 `h*-eth0` ingress에 attach되고, tshark는 edge switch의 host-facing interface에서 실행된다. 따라서 결론은 “동일 traffic/Redis/publish 방향 조건에서 각 online capture pipeline의 end-to-end emission 지연 비교”로 표현해야 한다.

XDP를 쓰는 이유는 커널 네트워크 스택의 이른 지점에서 필요한 TCP/IP header와 payload prefix만 추출해, `tshark` 기반 user-space capture/parsing 경로를 줄이는 것이다.

tshark 경로:

```text
interface
  -> kernel packet capture path
  -> libpcap/tshark process
  -> protocol field extraction
  -> stdout text serialization
  -> Python readline
  -> string split/parse
  -> FlowTable
  -> Redis
```

XDP 경로:

```text
interface ingress
  -> XDP hook
  -> 필요한 header/payload prefix만 추출
  -> perf buffer
  -> Python event handler
  -> FlowCache
  -> Redis
```

최종 비교 표:

```text
capture   samples   ready_p50   ready_p95   ready_p99   last_pkt_p50   last_pkt_p95   last_pkt_p99   last_pkt_max
XDP       672       0.116 ms    0.162 ms    0.696 ms    0.628 ms      1.148 ms      1.228 ms      1.697 ms
tshark    1349      0.564 ms    5.251 ms    9.945 ms    336.444 ms    646.592 ms    780.808 ms    1045.607 ms
```

비율:

```text
ready_to_xadd_done:
  p50: 0.564 / 0.116  = 약 4.9배
  p95: 5.251 / 0.162  = 약 32.4배
  p99: 9.945 / 0.696  = 약 14.3배

last_packet_to_xadd_done:
  p50: 336.444 / 0.628 = 약 536배
  p95: 646.592 / 1.148 = 약 563배
  p99: 780.808 / 1.228 = 약 636배
```

`last_packet_to_xadd_done`는 feature에 필요한 마지막 패킷이 관측된 시점부터 Redis `XADD` 완료까지의 시간이다. 따라서 tshark와 XDP의 캡처/파싱/큐 처리/backlog 차이를 가장 직접적으로 보여주는 지표다.

## 19. tshark 지연 발생 위치와 XDP의 감소 메커니즘

tshark 기반 realtime pipeline의 지연은 특정 한 함수 하나에서만 생기는 것이 아니라, 다음 단계들이 누적되며 발생한다.

```text
1. kernel packet capture path
2. libpcap/AF_PACKET 경로를 통한 packet copy
3. tshark protocol dissection
4. -T fields 출력 생성을 위한 field extraction
5. stdout text serialization
6. Python readline()
7. tab-separated string split/parse
8. Python queue 대기
9. RealtimeFlowTable 상태 갱신
10. Redis XADD
```

이 중 특히 tshark realtime 경로에서 큰 지연을 만들 수 있는 부분은 다음이다.

- `tshark` protocol dissection
  - tshark는 범용 packet analyzer라 TCP/IP header 일부만 필요한 현재 목적에 비해 많은 분석 경로를 지난다.
- text serialization
  - packet field를 binary 구조가 아니라 tab-separated text로 stdout에 쓴다.
  - 이후 Python이 다시 문자열을 split하고 숫자로 변환해야 한다.
- stdout/readline buffering
  - `-l` 옵션을 사용해도 tshark process, pipe, Python thread scheduling에 의해 backlog가 생길 수 있다.
- Python parsing 및 queue 처리
  - 여러 tshark reader thread가 packet을 queue에 넣고, processor thread가 이를 처리한다.
  - traffic burst가 오면 queue가 밀리고 `last_packet_to_xadd_done`가 커진다.

측정 결과는 이 해석과 맞다.

```text
tshark last_packet_to_xadd_done:
  p50 = 336.444 ms
  p95 = 646.592 ms
  p99 = 780.808 ms
```

마지막 feature packet timestamp 이후에도 Redis request emission까지 수백 ms가 걸린다는 것은, packet 자체가 이미 관측된 뒤에도 tshark stdout/파싱/큐 처리 경로에 backlog가 남아 있음을 의미한다.

반면 XDP는 다음 방식으로 지연을 줄인다.

```text
interface ingress
  -> XDP hook
  -> 필요한 TCP/IP header와 TrafficGenerator payload prefix만 추출
  -> perf buffer로 고정 구조체 전달
  -> Python event handler
  -> FlowCache
  -> Redis XADD
```

XDP는 tshark 경로의 다음 비용을 제거하거나 크게 줄인다.

- libpcap/tshark user-space capture path
- 범용 protocol dissection
- stdout text serialization
- Python text parsing
- tshark process scheduling 및 pipe buffering

그 결과 XDP의 `last_packet_to_xadd_done`는 다음 수준으로 유지된다.

```text
XDP last_packet_to_xadd_done:
  p50 = 0.628 ms
  p95 = 1.148 ms
  p99 = 1.228 ms
```

따라서 현재 실험에서 XDP의 이점은 단순히 Redis `XADD`가 빠르다는 것이 아니라, 마지막 feature packet이 들어온 뒤 Redis request를 만들기까지의 전체 online emission 경로에서 tshark 기반 capture/parsing backlog를 제거했다는 점이다.

## 20. 다음 실행 순서

추가 반복 실험 시 XDP 실행 명령:

```bash
cd ~/capstone

redis-cli -s /tmp/capstone-redis.sock DEL flow_features flow_features:latency

sudo python3 main.py 100 \
  --run-id xdp_uds_100_dctcp_e2e \
  --capture-mode xdp \
  --redis-url 'unix:///tmp/capstone-redis.sock?db=0' \
  --redis-stream flow_features
```

분석:

```bash
python3 analyze/redis_stream_latency.py \
  --redis-url 'unix:///tmp/capstone-redis.sock?db=0' \
  --stream flow_features \
  --run-id xdp_uds_100_dctcp_e2e \
  --capture-mode xdp
```

확인해야 할 것:

- `first_packet_to_xadd_done count > 0`
- `last_packet_to_xadd_done count > 0`
- `ready_to_xadd_done`가 기존 XDP 결과와 비슷한 sub-ms 수준인지 확인
- tshark 결과와 `last_packet_to_xadd_done` p95/p99 비교

## 21. tshark 조건 정렬: 방향 필터와 서버쪽 캡처 지점

live-traffic 브랜치의 기존 tshark realtime 경로를 확인한 결과, 캡처 조건이 XDP 비교에 불리하게 넓었다.

- 캡처 지점: 모든 edge host-facing 인터페이스 16개
- BPF filter: 각 지점에서 `tcp and src host <host_ip>`
- 결과: pod0/1 클라이언트가 보내는 `src_to_dst`와 pod2/3 서버가 보내는 `dst_to_src`가 모두 tshark stdout, Python 파싱, queue, flow table 처리를 통과

현재 XDP 비교의 기본 publish 방향은 `dst_to_src`다. 따라서 tshark가 `src_to_dst`까지 함께 처리하면, 실제 비교 대상이 아닌 request 방향 패킷까지 tshark dissection/텍스트 직렬화/파싱/큐 비용에 포함되어 `last_packet_to_xadd_done`가 더 커질 수 있다.

이를 분리해서 검증하기 위해 `/tmp/capstone-live-traffic`에 다음 옵션을 추가했다.

```text
--publish-direction {dst_to_src,src_to_dst,all}
  Redis Stream으로 publish할 ready flow 방향을 제한한다.
  XDP 비교 기본 조건은 dst_to_src.

--capture-scope {all-hosts,server-dst-to-src}
  all-hosts: 기존처럼 모든 host-facing edge 포트를 캡처한다.
  server-dst-to-src: TrafficGenerator 목적지 pod인 pod2/3의 host-facing edge 포트만 캡처한다.
```

`server-dst-to-src`는 캡처 지점을 서버쪽으로 줄이고, 기존 `src host <server_ip>` BPF filter를 그대로 사용한다. TrafficGenerator 구조에서 서버 응답은 `src_port == 5001`이고 realtime flow table에서는 이를 `dst_to_src`로 판정한다. 즉 tshark 프로세스가 애초에 처리하는 입력량을 서버 응답 방향 중심으로 줄이는 조건이다.

다음 비교 실행 명령:

```bash
cd /tmp/capstone-live-traffic

redis-cli -s /tmp/capstone-redis.sock DEL flow_features flow_features:latency

sudo python3 main.py 100 \
  --run-id tshark_server_dst_uds_100_dctcp_e2e \
  --realtime \
  --capture-scope server-dst-to-src \
  --publish-direction dst_to_src \
  --redis-url 'unix:///tmp/capstone-redis.sock?db=0' \
  --redis-stream flow_features
```

분석:

```bash
python3 analyze/redis_stream_latency.py \
  --redis-url 'unix:///tmp/capstone-redis.sock?db=0' \
  --stream flow_features \
  --run-id tshark_server_dst_uds_100_dctcp_e2e \
  --capture-mode tshark
```

이 결과를 기존 `tshark_uds_100_dctcp_e2e`와 비교하면 “양방향/all-host 캡처 때문에 증가한 tshark overhead”를 분리해 볼 수 있다. 그래도 `last_packet_to_xadd_done`가 XDP보다 충분히 크다면, 주된 병목은 방향 수 자체보다 tshark의 dissection/stdout text/Python parsing/queue 경로라고 더 강하게 말할 수 있다.

주의: 이것은 tshark를 XDP와 더 공정하게 맞춘 비교 조건이지만, 완전히 동일한 물리적 캡처 조건은 아니다. 현재 XDP는 host namespace의 인터페이스들에서 캡처하고 publish 방향을 `dst_to_src`로 제한한다. tshark `server-dst-to-src`는 캡처 입력 자체를 서버쪽으로 줄인 조건이다. 따라서 보고서에는 “기존 tshark all-host 결과”와 “서버쪽 dst_to_src 제한 tshark 결과”를 둘 다 제시하는 것이 좋다.
