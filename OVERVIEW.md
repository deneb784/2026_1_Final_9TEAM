# Fat-Tree 네트워크 시뮬레이션 전체 동작 흐름

---

## 사전 준비

### 필수 설치
```bash
# Mininet
sudo apt install -y mininet

# tshark (패킷 캡처)
sudo apt install -y tshark

# Docker
sudo apt install -y docker.io
```

### Ryu 컨트롤러 Docker 이미지 빌드 (최초 1회)
```bash
sudo docker build -f ryu_Dockerfile -t ryu-py3 .
```

### 폴더 권한 설정 (실행 후 pcap/결과 파일 접근 시)
```bash
sudo chown -R $USER:$USER captured_packet/
sudo chown -R $USER:$USER results/
```

### 예시 실행 (필요 인자 : 플로우 수) 
```bash
sudo python3 main.py 20
```

---

## 1. 전체 흐름 요약

```
main.py
  │
  ├─ 1. Ryu 컨트롤러 Docker 컨테이너로 백그라운드 실행
  │       └─ time.sleep(3): Ryu가 port 6653에서 listening 상태가 될 때까지 대기
  │
  ├─ 2. fatTreeBuilder(k) → Mininet 네트워크 생성 및 start()
  │
  ├─ 3. time.sleep(3): 스위치 룰 설치 완료까지 대기
  │       └─ 각 스위치가 Ryu에 연결되면 switch_features_handler 호출
  │           └─ rule_ecmp.install_rules() → ECMP 룰 설치
  │
  ├─ 4. CapturePoint 생성 (모든 edge 스위치의 host-facing 인터페이스)
  │       └─ 각 인터페이스에 연결된 호스트 IP를 src_ips로 지정
  │
  ├─ 5. PacketCapturer.start() → 각 인터페이스에서 tshark 실행
  │
  ├─ 6. flowGenerator() → 트래픽 생성
  │       ├─ dst 호스트 (pod2,3): server 데몬 실행
  │       ├─ ping 테스트로 라우팅 정상 확인
  │       └─ src 호스트 (pod0,1): client 병렬 실행 → 모든 dst로 TCP 플로우 전송
  │
  ├─ 7. PacketCapturer.stop() → tshark 종료
  │       └─ captured_packet/{interface}.pcap 저장 완료
  │
  └─ 8. 결과 분석 (TrafficGenerator/src/script/result.py)
```

---

## 2. Topology 생성 (`topoBuilder.py` / `fatTreeBuilder`)

k = 4 고정

### Output
| 반환값 | 타입 | 설명 |
|---|---|---|
| `network` | `Mininet` | 생성된 Mininet 네트워크 객체 |

### 동작 과정

**① Core 스위치 생성**
- 개수: `(k/2)²`
- 이름: `core1` ~ `core{(k/2)²}`
- DPID: `1` ~ `(k/2)²`

**② Pod 생성 (k개 반복)**

각 Pod 내에서:
- **Aggregation 스위치** 생성 (`k/2`개)
  - 이름: `agg_p{pod}_a{idx}`
  - DPID: `100 + pod*(k/2) + idx + 1`
- **Edge 스위치** 생성 (`k/2`개)
  - 이름: `edge_p{pod}_e{idx}`
  - DPID: `200 + pod*(k/2) + idx + 1`
- **Host** 생성 (`k/2`개 per Edge)
  - 이름: `h{N}` (전역 순번)
  - IP: `10.{pod}.{edge_idx}.{host+1}`
  - MAC: `00:00:{pod:02x}:{edge:02x}:00:{host+1:02x}`

**③ 링크 연결 (순서가 포트 번호를 결정)**

| 순서 | 연결 | Edge 포트 | Agg 포트 | Core 포트 |
|---|---|---|---|---|
| 1 | Host ↔ Edge | eth1 ~ eth{k/2} | - | - |
| 2 | Edge ↔ Agg | eth{k/2+1} ~ eth{k} | eth1 ~ eth{k/2} | - |
| 3 | Agg ↔ Core | - | eth{k/2+1} ~ eth{k} | eth1 ~ eth{k} |

**④ Mininet 네트워크 구성**
- Controller: `RemoteController('c0', ip='127.0.0.1', port=6653)`
- Link: `TCLink`
- Switch: `OVSSwitch`
- `autoStaticArp=True`: ARP를 Mininet이 자동 처리

### 링크 대역폭
| 구간 | 대역폭 |
|---|---|
| Host ↔ Edge | 100 Mbps |
| Edge ↔ Agg | 100 Mbps |
| Agg ↔ Core | 100 Mbps |

---

## 3. Ryu Controller 및 Rule 적용

### 전체 구성

```
main.py
  └─ docker run ryu-py3 ryu-manager main_controller.py
        └─ ModularFatTreeController
              └─ switch_features_handler (스위치 연결 시 이벤트)
                    └─ rule_ecmp.install_rules() → ECMP 라우팅 룰
```

### `rule_ecmp.install_rules(datapath, logger, k)`

DPID 범위로 스위치 타입 판별 후 OpenFlow 룰 설치 |

### DPID로 스위치 타입 판별
| 스위치 타입 | DPID 범위 |
|---|---|
| Core | `1` ~ `(k/2)²` |
| Aggregation | `101` ~ `100 + k*(k/2)` |
| Edge | `201` ~ `200 + k*(k/2)` |

### 스위치별 룰

**Core 스위치**
| 매칭 | 액션 | Priority |
|---|---|---|
| 목적지 `10.{pod}.0.0/16` | pod 방향 포트(`pod+1`)로 전달 | 100 |

**Aggregation 스위치**
| 매칭 | 액션 | Priority |
|---|---|---|
| 목적지 `10.{pod}.{e}.0/24` (같은 pod) | edge 다운링크 포트(`e+1`)로 전달 | 200 |
| 그 외 IP 트래픽 | ECMP 그룹 (업링크 포트 `k/2+1`~`k`) | 100 |

**Edge 스위치**
| 매칭 | 액션 | Priority |
|---|---|---|
| 목적지 `10.{pod}.{edge}.{h+1}` (로컬 호스트) | host 포트(`h+1`)로 전달 | 200 |
| 그 외 IP 트래픽 | ECMP 그룹 (업링크 포트 `k/2+1`~`k`) | 100 |

> **Priority 역할**: 하나의 패킷이 여러 룰에 매칭될 때 높은 priority가 우선 적용됨.
> 구체적인 룰(200)이 기본 룰(100)보다 먼저 적용되어 로컬 트래픽이 업링크로 나가지 않도록 함.

---

## 4. k값에 따른 스위치·호스트 수 공식

| 항목 | 공식 | k=4 |
|---|---|---|
| Core 스위치 | `(k/2)²` | 4 |
| Aggregation 스위치 | `k × (k/2)` | 8 |
| Edge 스위치 | `k × (k/2)` | 8 |
| **전체 스위치** | `(k/2)² + k²` | **20** |
| Pod 수 | `k` | 4 |
| Edge당 호스트 수 | `k/2` | 2 |
| **전체 호스트** | `k³ / 4` | **16** |

---

## 5. k=4 토폴로지 상세 정보

### 구조 개요
```
               [core1][core2][core3][core4]
                  |  \  / |   | \  / |
            [agg_p0_a0][agg_p0_a1] ... [agg_p3_a1]
               |    |              |    |
         [edge_p0_e0][edge_p0_e1] ... [edge_p3_e1]
           |  |          |  |              |  |
          h1  h2        h3  h4  ...      h15 h16
```

### Core 스위치 (4개)
| 이름 | DPID (dec) | eth1 | eth2 | eth3 | eth4 |
|---|---|---|---|---|---|
| core1 | 1 | pod0 | pod1 | pod2 | pod3 |
| core2 | 2 | pod0 | pod1 | pod2 | pod3 |
| core3 | 3 | pod0 | pod1 | pod2 | pod3 |
| core4 | 4 | pod0 | pod1 | pod2 | pod3 |

### Aggregation 스위치 (8개)
| 이름 | DPID (dec) | eth1 | eth2 | eth3 | eth4 |
|---|---|---|---|---|---|
| agg_p0_a0 | 101 | edge_p0_e0 | edge_p0_e1 | core1 | core2 |
| agg_p0_a1 | 102 | edge_p0_e0 | edge_p0_e1 | core3 | core4 |
| agg_p1_a0 | 103 | edge_p1_e0 | edge_p1_e1 | core1 | core2 |
| agg_p1_a1 | 104 | edge_p1_e0 | edge_p1_e1 | core3 | core4 |
| agg_p2_a0 | 105 | edge_p2_e0 | edge_p2_e1 | core1 | core2 |
| agg_p2_a1 | 106 | edge_p2_e0 | edge_p2_e1 | core3 | core4 |
| agg_p3_a0 | 107 | edge_p3_e0 | edge_p3_e1 | core1 | core2 |
| agg_p3_a1 | 108 | edge_p3_e0 | edge_p3_e1 | core3 | core4 |

### Edge 스위치 (8개)
| 이름 | DPID (dec) | eth1 | eth2 | eth3 | eth4 |
|---|---|---|---|---|---|
| edge_p0_e0 | 201 | h1 (10.0.0.1) | h2 (10.0.0.2) | agg_p0_a0 | agg_p0_a1 |
| edge_p0_e1 | 202 | h3 (10.0.1.1) | h4 (10.0.1.2) | agg_p0_a0 | agg_p0_a1 |
| edge_p1_e0 | 203 | h5 (10.1.0.1) | h6 (10.1.0.2) | agg_p1_a0 | agg_p1_a1 |
| edge_p1_e1 | 204 | h7 (10.1.1.1) | h8 (10.1.1.2) | agg_p1_a0 | agg_p1_a1 |
| edge_p2_e0 | 205 | h9 (10.2.0.1) | h10 (10.2.0.2) | agg_p2_a0 | agg_p2_a1 |
| edge_p2_e1 | 206 | h11 (10.2.1.1) | h12 (10.2.1.2) | agg_p2_a0 | agg_p2_a1 |
| edge_p3_e0 | 207 | h13 (10.3.0.1) | h14 (10.3.0.2) | agg_p3_a0 | agg_p3_a1 |
| edge_p3_e1 | 208 | h15 (10.3.1.1) | h16 (10.3.1.2) | agg_p3_a0 | agg_p3_a1 |

### 호스트 (16개)
| 이름 | IP | MAC | 연결된 Edge |
|---|---|---|---|
| h1 | 10.0.0.1 | 00:00:00:00:00:01 | edge_p0_e0 |
| h2 | 10.0.0.2 | 00:00:00:00:00:02 | edge_p0_e0 |
| h3 | 10.0.1.1 | 00:00:00:01:00:01 | edge_p0_e1 |
| h4 | 10.0.1.2 | 00:00:00:01:00:02 | edge_p0_e1 |
| h5 | 10.1.0.1 | 00:00:01:00:00:01 | edge_p1_e0 |
| h6 | 10.1.0.2 | 00:00:01:00:00:02 | edge_p1_e0 |
| h7 | 10.1.1.1 | 00:00:01:01:00:01 | edge_p1_e1 |
| h8 | 10.1.1.2 | 00:00:01:01:00:02 | edge_p1_e1 |
| h9 | 10.2.0.1 | 00:00:02:00:00:01 | edge_p2_e0 |
| h10 | 10.2.0.2 | 00:00:02:00:00:02 | edge_p2_e0 |
| h11 | 10.2.1.1 | 00:00:02:01:00:01 | edge_p2_e1 |
| h12 | 10.2.1.2 | 00:00:02:01:00:02 | edge_p2_e1 |
| h13 | 10.3.0.1 | 00:00:03:00:00:01 | edge_p3_e0 |
| h14 | 10.3.0.2 | 00:00:03:00:00:02 | edge_p3_e0 |
| h15 | 10.3.1.1 | 00:00:03:01:00:01 | edge_p3_e1 |
| h16 | 10.3.1.2 | 00:00:03:01:00:02 | edge_p3_e1 |

---

## 6. 트래픽 생성 (`flowGenerator.py`)

### Input
| 파라미터 | 타입 | 설명 |
|---|---|---|
| `network` | `Mininet` | Mininet 네트워크 객체 |
| `port` | `int` | TCP 포트 번호 (dst쪽 포트 번호) |
| `flowsPerPair` | `int` | src-dst 쌍 당 전송할 플로우 수 |

### Output
| 결과물 | 경로 | 설명 |
|---|---|---|
| 플로우 결과 | `results/flows_{srcIndex}.txt` | 각 src 호스트의 플로우 완료 로그 |
| 플로우 메타데이터 | `results/flows_{srcIndex}_meta.csv` | 요청 단위 flow 식별용 메타데이터 |
| 실행 로그 | `results/log_{srcIndex}.txt` | client 실행 로그 |

### 동작 과정

**① src / dst 호스트 분류**
- `10.0.*.*`, `10.1.*.*` (pod 0, 1) → **src** 호스트 (8개)
- `10.2.*.*`, `10.3.*.*` (pod 2, 3) → **dst** 호스트 (8개)

**② dst 호스트: server 데몬 실행**
```
TrafficGenerator/bin/server -p {port} &
```

**③ src별 config 파일 생성 (모든 dst 등록)**
```
# TrafficGenerator/conf/src{N}_config.txt
server 10.2.0.1 {port}
server 10.2.0.2 {port}
server 10.2.1.1 {port}
...                       ← 모든 dst 호스트 (8개)
req_size_dist TrafficGenerator/conf/DCTCP_CDF.txt
```
- 각 src는 모든 dst 호스트 중 랜덤하게 선택해서 플로우 전송 → 다양한 destination

**④ ping 테스트**
- `srcHosts[0]` → `dstHosts[0]` ping으로 라우팅 정상 여부 확인

**⑤ src 호스트: client 병렬 실행**
```
TrafficGenerator/bin/client
  -b 80                           # 목표 대역폭 (Mbps)
  -c conf/src{N}_config.txt       # 설정 파일
  -n {flowsPerPair}               # 전송할 플로우 수
  -l results/flows_{N}.txt        # 결과 저장 파일
  -s 1                            # 랜덤 시드
  -x {srcIndex}                   # 송신 host 인덱스
  -v                              # verbose
```
- subprocess 사용

### 트래픽 패턴
- **DCTCP_CDF.txt**: 데이터센터 트래픽 패턴 (DCTCP 논문 기반 CDF)
- 소규모 플로우(마우스 플로우)와 대규모 플로우(엘리펀트 플로우) 혼합

---

## 7. 패킷 캡처 (`packet_captuer.py` / `PacketCapturer`)

### `CapturePoint`
| 필드 | 타입 | 설명 |
|---|---|---|
| `node` | Mininet node | 캡처를 실행할 노드 (edge 스위치) |
| `interface` | `str` | 캡처할 인터페이스 이름 (예: `edge_p0_e0-eth1`) |
| `src_ips` | `tuple[str, ...]` | host→switch 방향 필터링에 사용할 host IP |

### `PacketCapturer.__init__`
| Input | 타입 | 설명 |
|---|---|---|
| `capture_points` | `list[CapturePoint]` | 캡처 지점 목록 |
| `output_dir` | `str` | pcap 파일 저장 디렉터리 (기본값: `captured_packet`) |

### `PacketCapturer.start()`
**실행되는 tshark 명령**
```bash
tshark -i {interface} -w {output_dir}/{interface}.pcap \
  -f "tcp and (src host {host_ip})"
```
- `tcp`: TrafficGenerator는 TCP만 사용, ICMP(ping) 등 제외
- `src host {host_ip}`: 해당 host가 스위치로 내보내는 방향(host→switch) 패킷만 캡처

### 방향 해석
- 캡처 지점은 모든 host-facing 인터페이스에 존재하지만, 각 인터페이스에서는 "그 host가 송신한 패킷"만 저장한다.
- 따라서 src host 포트에서 관측되는 패킷은 주로 `src -> dst` 정방향 요청 트래픽이다.
- 반대로 dst host 포트에서 관측되는 패킷은 주로 `dst -> src` 역방향 응답 트래픽이다.
- 즉 현재 구조는 하나의 포트에서 양방향을 동시에 잡는 방식이 아니라, 각 host 포트의 송신 방향만 모아서 전체적으로 정방향/역방향을 분리해 관측하는 방식이다.

### `PacketCapturer.stop()`
- tshark subprocess에 SIGINT 전송 → 2초 대기 → 미종료 시 kill

### 캡처 지점 구성 (k=4 기준)
- 대상: 모든 edge 스위치의 host-facing 인터페이스 (`eth1`, `eth2`)
- 총 캡처 지점: 8 스위치 × 2 인터페이스 = **16개**

### 생성되는 pcap 파일 (k=4)
```
captured_packet/
├── edge_p0_e0-eth1.pcap   # h1 (10.0.0.1) 송신 패킷
├── edge_p0_e0-eth2.pcap   # h2 (10.0.0.2) 송신 패킷
├── edge_p0_e1-eth1.pcap   # h3 (10.0.1.1) 송신 패킷
├── edge_p0_e1-eth2.pcap   # h4 (10.0.1.2) 송신 패킷
├── ...
└── edge_p3_e1-eth2.pcap   # h16 (10.3.1.2) 송신 패킷 (총 16개)
```

### 요청/응답과의 대응 관계
- `pod0`, `pod1`의 src host 포트 pcap:
  - client가 외부로 내보내는 요청 패킷
  - 보통 `logical_flow_id = (src_index, flow_id, src_to_dst)`에 대응
- `pod2`, `pod3`의 dst host 포트 pcap:
  - server가 외부로 내보내는 응답 패킷
  - 보통 `logical_flow_id = (src_index, flow_id, dst_to_src)`에 대응
- 방향 판별은 캡처 위치와 함께 packet ToS의 최하위 1비트(`tos & 1`)를 이용해 확인할 수 있다.

---

## 8. 요청 및 방향별 Logical Flow 식별

TrafficGenerator는 persistent connection을 재사용하므로, 서로 다른 요청이 같은 TCP 연결을 공유할 수 있다.
따라서 `src_port` 또는 `5-tuple` 기준으로는 실제 요청 단위를 안정적으로 구분할 수 없다.

이를 해결하기 위해 식별 기준을 **TCP 연결**이 아니라 **요청(Request)** 으로 변경했다.
또한 로드밸런싱 실험에서 `src -> dst`, `dst -> src`를 서로 다른 방향 플로우로 다루기 위해,
하나의 요청 아래에 두 개의 **direction-aware logical flow**를 정의했다.

### 변경 파일
| 파일 | 변경 내용 |
|---|---|
| [`TrafficGenerator/src/common/common.h`](/home/leesungwon/capstone/TrafficGenerator/src/common/common.h:8) | `flow_metadata.direction` 추가, ToS direction bit 헬퍼 정의 |
| [`TrafficGenerator/src/common/common.c`](/home/leesungwon/capstone/TrafficGenerator/src/common/common.c:128) | socket metadata 직렬화/역직렬화에 `direction` 포함 |
| [`TrafficGenerator/src/client/client.c`](/home/leesungwon/capstone/TrafficGenerator/src/client/client.c:725) | 각 요청에 `flow.id = req_id + 1` 부여, 요청을 `src_to_dst`로 태깅 |
| [`TrafficGenerator/src/client/client.c`](/home/leesungwon/capstone/TrafficGenerator/src/client/client.c:894) | `flows_{srcIndex}_meta.csv` 생성, 방향별 logical flow ID 기록 |
| [`TrafficGenerator/src/server/server.c`](/home/leesungwon/capstone/TrafficGenerator/src/server/server.c:140) | 응답 전송 시 요청 metadata를 `dst_to_src` 방향으로 변환 |
| [`flowGenerator.py`](/home/leesungwon/capstone/flowGenerator.py:105) | client 실행 시 `-x <srcIndex>` 전달 |

### 식별 기준
- 각 요청은 client 내부에서 `flow.id = req_id + 1` 값을 가진다.
- 각 src host마다 `flow.id`는 다시 1부터 시작하므로, 실험 전체 요청 고유 식별자는 `(src_index, flow_id)` 조합으로 정의한다.
- 하나의 요청은 두 개의 방향별 logical flow를 가진다.
  - `src_to_dst`: client가 서버로 보내는 정방향 요청 플로우
  - `dst_to_src`: server가 client로 보내는 역방향 응답 플로우
- 따라서 현재 구조에서 `request_id`와 `logical_flow_id`는 다음처럼 구분한다.
  - `request_id = (src_index, flow_id)`
  - `logical_flow_id = (src_index, flow_id, direction)`

### ToS와 방향 태깅
- 기존 TrafficGenerator의 DSCP 필드는 유지한다.
- 대신 실험 편의를 위해 ToS의 최하위 1비트를 direction tag로 사용한다.
  - `tos & 1 == 0` → `src_to_dst`
  - `tos & 1 == 1` → `dst_to_src`
- 현재 설정에서는 `dscp=0`이므로 실제 패킷에서는 보통 다음처럼 관찰된다.
  - `src_to_dst_tos = 0`
  - `dst_to_src_tos = 1`

### 메타데이터 필드

| 필드 | 설명 |
|---|---|
| `src_index` | 송신(src) host 인덱스 |
| `flow_id` | 해당 src host 내부 요청 ID (= request local ID) |
| `server_id` | 선택된 destination server 인덱스 |
| `connection_id` | 요청 전송에 사용된 persistent connection ID |
| `src_ip` | 요청 시작 시점의 로컬 출발지 IP |
| `src_port` | 요청 시작 시점의 로컬 출발지 TCP 포트 |
| `dst_ip` | 목적지 서버 IP |
| `dst_port` | 목적지 서버 포트 |
| `size_bytes` | 요청 크기 |
| `dscp` | DSCP 값 |
| `rate_mbps` | 송신 rate |
| `start_time_us` | 요청 시작 시각 |
| `stop_time_us` | 요청 종료 시각 |
| `fct_us` | Flow Completion Time |
| `src_to_dst_flow_id` | 정방향 logical flow 식별자 (`src_index:flow_id:src_to_dst`) |
| `dst_to_src_flow_id` | 역방향 logical flow 식별자 (`src_index:flow_id:dst_to_src`) |
| `src_to_dst_tos` | 정방향 패킷에 부여되는 ToS 값 |
| `dst_to_src_tos` | 역방향 패킷에 부여되는 ToS 값 |

### 해석 예시
예를 들어 다음 메타데이터 row:

```csv
0,1,6,0,10.0.0.1,46228,10.3.1.1,5001,3205631,0,0,...,0:1:src_to_dst,0:1:dst_to_src,0,1
```

- `request_id = (0, 1)`
- `10.0.0.1:46228 -> 10.3.1.1:5001` 요청 1개
- 정방향 패킷은 `logical_flow_id = 0:1:src_to_dst`, `ToS = 0`
- 역방향 패킷은 `logical_flow_id = 0:1:dst_to_src`, `ToS = 1`

### 활용 목적
후속 피처 추출은 더 이상 `5-tuple` 기준이 아니라, `flows_*_meta.csv`의 `(src_index, flow_id)`를 상위 요청 키로 사용해야 한다.
그리고 실제 패킷 수집/분석에서는 이를 방향별 logical flow로 확장해서 해석해야 한다.
- 요청 기준: `(src_index, flow_id)`
- 방향별 플로우 기준: `(src_index, flow_id, direction)`

---

## 9. CSV 변환 (`analyze/pcap_to_csv.py`)

16개 pcap 파일을 **pcap별 개별 CSV** 및 **전체 통합 CSV** 두 가지 형태로 변환한다.

```bash
sudo python3 analyze/pcap_to_csv.py
```

### 출력 파일

**① pcap별 개별 CSV** (`convert()`)
```
csv_results/
├── edge_p0_e0-eth1.csv   # h1 (10.0.0.1) 패킷
├── edge_p0_e0-eth2.csv   # h2 (10.0.0.2) 패킷
├── ...
└── edge_p3_e1-eth2.csv   # h16 (10.3.1.2) 패킷 (총 16개)
```
- 헤더: FIELDS 그대로 (source_file 컬럼 없음)
- `tcp.stream`이 파일 단위로 부여되므로 파일별로 분리하면 번호 충돌 없음
```
### CSV 필드

| 필드 | 설명 |
|---|---|
| `frame.number` | pcap 파일 내 패킷 일련번호 (tshark 부여, 1부터 시작) |
| `frame.time_epoch` | 캡처 타임스탬프 (Unix epoch, ��대 시각) |
| `frame.len` | 패킷 전체 크기 (bytes) |
| `ip.src` | 출발지 IP |
| `ip.dst` | 목적지 IP |
| `ip.len` | IP 페이로드 크기 (bytes) |
| `ip.ttl` | TTL |
| `tcp.srcport` | 출발지 포트 |
| `tcp.dstport` | 목적지 포트 |
| `tcp.stream` | TCP 연결 식별 번호 (tshark 부여, pcap 파일 단위로 0부터 시작) |
| `tcp.len` | TCP 페이로드 크기 (bytes) |
| `tcp.seq` | 시퀀스 번호 (내가 보내는 데이터의 시작 바이트 위치) |
| `tcp.ack` | ACK 번호 (상대방 데이터를 몇 바이트까지 수신했는지) |
| `tcp.flags` | TCP 플래그 (SYN/ACK/FIN 등) |
| `tcp.window_size` | 윈도우 크기 |
| `tcp.time_relative` | 해당 TCP 연결 첫 패킷 기준 상대 시간 (초) |
| `tcp.time_delta` | 같은 TCP 연결 내 직전 패킷과의 시간 차 (초) |
| `tcp.analysis.retransmission` | 재전송 여부 |
| `tcp.analysis.out_of_order` | 순서 어긋남 여부 |
| `tcp.analysis.duplicate_ack` | 중복 ACK 여부 |
| `tcp.analysis.fast_retransmission` | 빠른 재전송 여부 |

### 주의
`tcp.stream`은 pcap 파일 단위 TCP 스트림 번호이므로, persistent connection 재사용 환경에서는 실제 요청 단위 flow 식별자로 사용할 수 없다.
따라서 요청 단위 분석은 `source_file + tcp.stream`이 아니라 `flows_*_meta.csv`의 `(src_index, flow_id)`를 기준으로 해석해야 한다.
