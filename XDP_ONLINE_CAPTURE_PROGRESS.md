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
    -> feature_pipeline에서 pcap + metadata를 읽어 dataset/feature 생성
```

기존 feature 추출의 핵심 파일은 다음과 같다.

- `feature_pipeline/packet_loader.py`
  - `tshark -Y tcp`로 TCP 패킷을 읽는다.
  - `tcp.len > 0` 필터가 없으므로 ACK-only 패킷도 포함된다.
- `feature_pipeline/flow_cache.py`
  - `(src_index, flow_id, direction)`을 key로 flow를 구분한다.
  - flow별 초기 `feature_packet_count`개 패킷을 저장한다.
- `feature_pipeline/dataset_builder.py`
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

tshark로도 원리적으로 같은 일을 할 수는 있다. 다만 현재 `feature_pipeline/packet_loader.py`는 `tcp.payload`를 읽지 않고 header/length/flag 계열 field만 읽는다. 따라서 기존 tshark 코드가 payload metadata를 파싱한 것은 아니며, tshark에서 이 방식을 쓰려면 `tcp.payload` 또는 raw packet bytes를 추가로 읽고, connection별 state를 유지하는 별도 로직이 필요하다.

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

- `packet_captuer.py`
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

- `feature_pipeline/online_tg_flow_cache.py`

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
--capture-mode {tshark,xdp,none}
--xdp-mode {skb,native,hw}
--feature-packet-count N
```

기본값은 기존 호환을 위해 `tshark`다.

XDP smoke 실행 예시:

```bash
sudo python3 main.py 1 --run-id xdp_smoke --capture-mode xdp --feature-packet-count 2
```

패킷 10개 기준 검증 실행 예시:

```bash
sudo python3 main.py 100 --run-id xdp_loss_seq10 --capture-mode xdp --feature-packet-count 10
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
env PYTHONPYCACHEPREFIX=/tmp/capstone_pycache python3 -m py_compile main.py packet_captuer.py xdp/tg_xdp_capture.py feature_pipeline/online_tg_flow_cache.py
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

- `feature_pipeline/online_request.py`
  - ready `FlowEntry`를 모델 학습 데이터와 같은 `feature_names`, `x`, `seq_len` 형태의 온라인 요청 payload로 변환한다.
- `feature_pipeline/redis_transport.py`
  - Redis Stream producer를 제공한다.
  - `XADD flow_features payload=...` 형태로 요청을 적재한다.
- `xdp/tg_xdp_capture.py`
  - `--redis-url`이 주어지면 ready flow를 Redis Stream에 전송한다.
  - 기본 전송 방향은 `dst_to_src`다.
- `model/gru_stream_worker.py`
  - Redis Stream consumer group으로 요청을 읽는다.
  - GRU checkpoint와 scaler로 추론한다.
  - 결과를 Redis Pub/Sub channel로 publish한다.
- `feature_pipeline/redis_result_subscriber.py`
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
4. tshark 동시 캡처와 XDP 캡처를 비교해 외부 기준의 유실 검증 추가하기

현재까지의 결과는 “XDP로 online FlowCache 입력을 만들 수 있다”는 단계까지 도달한 상태다. 다음 작업부터는 이 ready entry를 모델 추론 파이프라인에 연결하면 된다.
