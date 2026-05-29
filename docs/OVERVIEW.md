# Fat-Tree 네트워크 시뮬레이션 전체 동작 흐름

---

이 문서는 프로젝트 내부 구조와 데이터 흐름을 설명한다.
환경 준비, 실행 명령, Redis/Conda 설정은 `README.md`를 기준으로 확인한다.
XDP/Redis 실시간 추론 경로와 payload 구조는 `docs/REALTIME_PACKET_REDIS_PIPELINE.md`를 기준으로 확인한다.

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

## 2. Topology 생성 (`network/topology.py` / `fatTreeBuilder`)

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
  └─ docker run ryu-py3 ryu-manager network/controller.py
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

## 6. 트래픽 생성 (`network/traffic_generator.py`)

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

## 7. 패킷 캡처 (`network/packet_capture.py` / `PacketCapturer`)

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

## 8. ECMP 검증 로직 (`analyze/ecmp_verification.py`)

현재 실험 구조는 다음 두 단계로 동작한다.

- `Ryu`가 스위치 계층별 base rule과 ECMP group을 설치
- 이후 `main.py`가 `ovs-ofctl -O OpenFlow15 mod-group`를 사용해 edge/aggregation 스위치의 `group_id=1`에 `selection_method=hash`를 다시 적용

즉 현재 ECMP 검증은 **Ryu base rule + OVS hash 기반 group selection 보정** 상태에서 수행한다.  
추가된 `ecmp_verification` 로직은 이 제어 결과를 바꾸는 코드가 아니라, 실험 종료 후 실제 uplink 사용 결과를 확인하기 위한 **후처리/검증 코드**이다.

### 제어 구조 보정
- Ryu controller는 OpenFlow 1.3으로 base rule과 base ECMP group을 설치한다.
- OVS 스위치는 `OpenFlow13,OpenFlow15`를 모두 허용한다.
- `main.py`의 `configure_ecmp_group_selection()`가 edge/aggregation 계층의 `group_id=1`을 hash 기반 selection으로 다시 설정한다.
- edge와 aggregation에는 서로 다른 `selection_method_param(hash basis)`를 적용해 상위 계층에서도 동일한 hash 결과가 반복되는 현상(polarization)을 줄인다.

### 무엇을 추가로 했는가
- edge 스위치의 uplink 포트(`eth3`, `eth4`)도 함께 캡처
- aggregation 스위치의 uplink 포트(`eth3`, `eth4`)도 함께 캡처
- uplink pcap을 요약한 report 생성

### 생성 파일
| 파일 | 설명 |
|---|---|
| `results/ecmp_uplink_summary.csv` | edge/aggregation uplink pcap 기반 패킷/바이트 집계 |
| `results/ecmp_verification_report.txt` | 사람이 바로 읽을 수 있는 ECMP 요약 리포트 |

### 이름 규칙 해석
- `p*` : pod 번호
- `e*` : edge switch 인덱스
- `a*` : aggregation switch 인덱스
- `eth*` 또는 `port *` : 해당 스위치 인터페이스 번호

예시:
- `edge_p2_e1`
  - `pod 2`의 `edge switch 1`
- `agg_p3_a0`
  - `pod 3`의 `aggregation switch 0`

### 포트 번호 의미 (`k=4`)
- Edge switch
  - `port 1`, `port 2` : host-facing downlink
  - `port 3`, `port 4` : aggregation uplink
- Aggregation switch
  - `port 1`, `port 2` : edge downlink
  - `port 3`, `port 4` : core uplink

### 리포트 해석
`results/ecmp_verification_report.txt`는 uplink pcap을 직접 집계한 결과이다.

- edge uplink 포트(`eth3`, `eth4`)와 aggregation uplink 포트(`eth3`, `eth4`)를 대상으로 집계한다
- `packets`, `bytes`는 해당 uplink 포트로 관측된 패킷 수와 바이트 수
- 퍼센트는 같은 스위치의 두 uplink 중 어느 쪽이 얼마나 많이 쓰였는지 의미

예시:
```text
edge_p0_e0
  port 3: packets=33231 (54.94%), bytes=32972224 (54.96%)
  port 4: packets=27260 (45.06%), bytes=27017894 (45.04%)
```
- `edge_p0_e0`의 uplink 두 개가 모두 사용되었고,
- 패킷/바이트 기준으로 약 `55:45` 수준으로 분산되었다는 뜻이다.

예시:
```text
agg_p0_a0
  port 3: packets=30283 (99.93%), bytes=2003795 (99.87%)
  port 4: packets=21 (0.07%), bytes=2699 (0.13%)
```
- `agg_p0_a0`에서는 core uplink 중 `port 3`이 거의 대부분 사용되었고,
- `port 4`는 거의 사용되지 않았다는 뜻이다.

### 현재 결과를 어떻게 해석하는가
- ECMP rule이 설치된 뒤 **여러 uplink가 실제로 사용되는지** 확인하는 것이 1차 목적이다.
- 따라서 두 uplink가 모두 사용되면, **ECMP가 동작한다는 근거**가 된다.
- 다만 ECMP는 일반적으로 flow hash 기반이므로, 결과가 항상 `50:50`의 완전한 균등 분산으로 나타나는 것은 아니다.
- 기본 Ryu-only select group 상태에서는 aggregation 계층 편향이 크게 나타날 수 있다.
- 현재는 edge/aggregation에 서로 다른 hash basis를 적용한 뒤 uplink pcap을 기준으로 실제 multipath 활용 여부를 확인한다.

즉 현재 report는
- `Ryu base rule + OVS hash selection 보정이 적용되었는지`
- `실제로 여러 경로가 사용되었는지`
- `계층별로 편향이 어느 정도인지`
를 확인하기 위한 보조 검증 자료로 해석하면 된다.

---

## 9. 요청 및 방향별 Logical Flow 식별

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
| [`network/traffic_generator.py`](/home/leesungwon/capstone/network/traffic_generator.py:105) | client 실행 시 `-x <srcIndex>` 전달 |

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

## 10. CSV 변환 (`analyze/pcap_to_csv.py`)

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

---

## 11. Feature Pipeline (`pipeline/`)

`pipeline`는 실험 종료 후 생성된 요청 메타데이터와 패킷 캡처 파일을 입력으로 받아, 요청 단위 및 방향별 logical flow 단위의 packet-level sequence를 추출하는 후처리 파이프라인이다.

### 배경
패킷 기반 정보를 요청 단위로 추출하려면 각 패킷이 어떤 요청에 속하는지 식별할 수 있어야 한다. 본 실험 환경에서는 동일한 src/dst ip:port 조합이 여러 요청에서 반복될 수 있으므로, 요청 메타데이터(`flows_*_meta.csv`)와 시간 구간 정보를 함께 사용하여 요청 단위 매칭을 수행한다.

초기 구현에서는 방향별 flow에 누적된 패킷으로부터 평균, 합계 등의 통계 feature를 계산해 `features` 형태로 출력하였다. 이후 packet별 정보와 순서 정보를 보존하기 위해, 현재는 각 패킷의 feature를 그대로 유지한 `packets` sequence 형태로 출력하도록 구조를 변경하였다.

### 입력
- `results/flows_*_meta.csv`
- `captured_packet/*.pcap`

### 파일 구성
- `pipeline/models.py`
  - `RequestMeta`, `PacketRecord`, `FlowEntry` 등 파이프라인에서 사용하는 데이터 구조를 정의한다.
- `pipeline/dataset/meta_loader.py`
  - `flows_*_meta.csv`를 읽어 `RequestMeta` 객체로 변환하고, 요청 메타 인덱스를 생성한다.
- `pipeline/dataset/packet_loader.py`
  - `captured_packet/*.pcap`를 `tshark`로 읽어 `PacketRecord` 객체로 변환한다.
- `pipeline/dataset/matcher.py`
  - 패킷의 `src/dst ip:port` 및 시간 구간을 이용해 패킷을 `(src_index, flow_id, direction)`에 매칭한다.
- `pipeline/flow_cache.py`
  - 같은 방향의 패킷을 `(src_index, flow_id, direction)` 키로 누적하고, packet sequence 생성 가능 여부를 관리한다.
- `pipeline/dataset/feature_extractor.py`
  - 누적된 패킷 버퍼로부터 packet-level feature sequence를 생성한다.
- `pipeline/dataset/pipeline.py`
  - 전체 파이프라인을 실행하고 최종 JSON payload를 생성한다.
- `pipeline/dataset/dataset_builder.py`
  - 방향별 flow 단위의 학습용 dataset을 생성한다. 전체 패킷을 방향별 flow로 끝까지 누적한 뒤, 초기 n개 패킷의 feature sequence를 입력 `x`로 구성하고, 해당 방향 flow의 전체 `tcp_len` 합을 기준으로 directional_size_bytes와 label을 생성해 `dataset.jsonl`로 저장한다.


### 메타데이터 기반 요청 식별
`flows_*_meta.csv`의 각 row는 요청 1개를 나타낸다. 파이프라인은 다음 식별 체계를 사용한다.

- 요청 기준 식별자: `(src_index, flow_id)`
- 방향별 logical flow 기준 식별자: `(src_index, flow_id, direction)`

여기서 `direction`은 다음 두 값 중 하나이다.
- `src_to_dst`
- `dst_to_src`

### 패킷 매칭 방식
패킷은 `tshark`를 통해 `pcap`으로부터 읽고, 다음 절차로 요청 메타 row에 매칭한다.

1. 패킷의 `src/dst ip:port`를 기준으로 메타 인덱스에서 후보 요청을 찾는다.
2. 정방향 비교:
   - `packet.src == meta.src`
   - `packet.dst == meta.dst`
3. 역방향 비교:
   - `packet.src == meta.dst`
   - `packet.dst == meta.src`
4. 패킷 timestamp가 메타의 `start_time_us ~ stop_time_us` 구간에 포함되는지 확인한다.
5. 최종적으로 `(src_index, flow_id, direction)`를 결정한다.

이때 `start_time_us`와 `stop_time_us`는 요청 전송 시작부터 응답 수신 완료까지의 전체 시간 구간을 의미하므로, `src_to_dst`뿐 아니라 `dst_to_src` 패킷도 같은 요청 메타 row에 매칭할 수 있다.

### 처리 흐름
1. `flows_*_meta.csv`를 읽어 `RequestMeta` 객체로 변환한다.
2. `(src_ip, src_port, dst_ip, dst_port)` 기준으로 메타 인덱스를 생성한다.
3. `captured_packet/*.pcap`를 `tshark`로 읽어 `PacketRecord` 객체로 변환한다.
4. 각 패킷을 메타와 비교하여 `(src_index, flow_id, direction)`에 매칭한다.
5. 같은 방향의 패킷을 `FlowCache`에 누적한다.
6. 지정한 개수(`feature_packet_count`)의 패킷이 모이면 packet sequence를 구성한다.
7. 결과를 JSON payload로 생성한다.

### 학습용 Dataset 생성
`dataset_builder.py`는 pipeline에서 생성한 packet-level sequence 구조를 바탕으로, 모델 학습에 사용할 수 있는 dataset을 구성하는 후처리 모듈이다. 이 모듈은 모든 패킷을 메타데이터와 매칭한 뒤 `(src_index, flow_id, direction)` 기준의 방향별 flow로 누적한다. 이후 각 방향별 flow에 대해 다음 정보를 생성한다.

- flow_key: `(src_index, flow_id, direction)` 형태의 방향별 flow 식별자
- x: 초기 `packet_count`개 패킷의 feature sequence
- directional_size_bytes: 해당 방향 flow 전체의 `tcp_len` 합
- label: directional_size_bytes와 2,000,000 bytes threshold를 비교해 생성한 mice/elephant 라벨
  
즉 `pipeline.dataset.pipeline`이 packet sequence JSON payload를 생성하는 역할이라면, `dataset_builder.py`는 이를 학습용 sample 구조로 변환해 `dataset.jsonl` 형태로 저장하는 역할을 수행한다.

### FlowCache 구조
`FlowCache`는 같은 방향 패킷만 하나의 버퍼에 누적한다. 캐시 키는 다음과 같다.

- `(src_index, flow_id, direction)`

즉 하나의 요청 `(src_index, flow_id)`에 대해:
- `(src_index, flow_id, "src_to_dst")`
- `(src_index, flow_id, "dst_to_src")`

두 개의 방향별 flow entry가 존재할 수 있다.

### Packet Sequence 생성 방식
패킷이 요청 및 방향별 logical flow에 매칭되면, 해당 패킷은 FlowCache에 누적된다. 같은 방향의 패킷이 `feature_packet_count`개 모이면, 그 시점까지의 패킷 버퍼를 기반으로 packet sequence를 생성한다.

현재 구현은 각 패킷에 대해 다음과 같은 packet-level feature를 유지한다.
- `frame_len`
- `ip_len`
- `ip_ttl`
- `tcp_payload_bytes`
- `tcp_flags`
- `tcp_window_size`
- `iat_us`
- `retransmission`
- `out_of_order`
- `duplicate_ack`
- `fast_retransmission`

즉 기존처럼 여러 패킷을 평균, 합계, 최댓값 등의 통계값으로 요약하는 대신, 각 패킷의 feature를 순서대로 보존한 sequence 형태로 출력한다.

### 출력 포맷
파이프라인은 방향별 logical flow에 대해 다음 형태의 JSON payload를 생성한다.

```json
{
  "flow_key": {
    "src_index": 0,
    "flow_id": 19,
    "direction": "src_to_dst"
  },
  "logical_flow_id": "0:19:src_to_dst",
  "meta": {
    "src_ip": "10.0.0.1",
    "src_port": 34294,
    "dst_ip": "10.3.0.1",
    "dst_port": 5001,
    "start_time_us": 1776424613442059,
    "stop_time_us": 1776424622304806,
    "fct_us": 8862747,
    "size_bytes": 9033446,
    "dscp": 0,
    "rate_mbps": 0
  },
  "packets": [
    {
      "packet_index": 0,
      "frame_len": 66,
      "ip_len": 52,
      "ip_ttl": 64,
      "tcp_payload_bytes": 0,
      "tcp_flags": "0x0010",
      "tcp_window_size": 1152,
      "iat_us": 0,
      "retransmission": 0,
      "out_of_order": 0,
      "duplicate_ack": 0,
      "fast_retransmission": 0
    },
    {
      "packet_index": 1,
      "frame_len": 66,
      "ip_len": 52,
      "ip_ttl": 64,
      "tcp_payload_bytes": 0,
      "tcp_flags": "0x0010",
      "tcp_window_size": 1152,
      "iat_us": 2219,
      "retransmission": 0,
      "out_of_order": 0,
      "duplicate_ack": 0,
      "fast_retransmission": 0
    }
  ]
}

```

### 학습용 Dataset 포맷
`dataset_builder.py`가 생성하는 학습용 dataset sample은 다음과 같은 구조를 가진다.

```json
{
  "flow_key": {
    "src_index": 0,
    "flow_id": 19,
    "direction": "src_to_dst"
  },
  "x": [
    [66, 52, 64, 0, 16, 1152, 0, 0, 0, 0, 0],
    [66, 52, 64, 0, 16, 1152, 2219, 0, 0, 0, 0]
  ],
  "directional_size_bytes": 3199399,
  "label": 1
}
```
여기서 `x`는 packet sequence 입력이며, `label`은 방향별 flow 전체 payload 크기를 기준으로 생성된다.

### 실행 방법
데이터셋 생성과 실시간 추론 실행 명령은 `README.md`에 정리한다.
여기서는 내부 처리 구조만 설명한다.

### 현재 상태
현재 파이프라인은 다음 단계까지 구현 및 검증되었다.

- 요청 메타 로딩
- 패킷 로딩 (`pcap` + `tshark`)
- 메타 기반 요청/방향 매칭
- 방향별 `FlowCache` 누적
- 초기 n개 패킷 기반 packet sequence 생성
- JSON payload 생성
- 방향별 flow 기준 학습용 `dataset.jsonl` 생성
- `dataset_builder.py`를 통한 flow_key, x, directional_size_bytes, label 구성
- packet sequence 기반 모델 학습 입력 생성
- XDP 기반 온라인 `RealtimeFlowCache` 생성
- Redis Stream/PubSub 기반 모델 worker 연동
- `step_GRU` 가중치 기반 실시간 elephant/mice 추론
- 추론 결과 JSONL 로그 저장

즉 현재 코드는 오프라인 dataset 생성 경로와 온라인 XDP/Redis/model 추론 경로를 모두 갖고 있다.

### 추가 고려 사항
오프라인 경로는 pcap과 metadata를 사후 매칭하므로 재현성과 디버깅에 유리하다.
온라인 경로는 XDP event에서 TrafficGenerator metadata를 직접 읽어 같은 logical flow key를 구성하고, ready 상태가 되면 Redis Stream으로 모델 요청을 보낸다.

학습/추론 feature 정책은 dataset 종류와 sequence 길이에 맞춰야 한다. 예를 들어 FB traffic은 작은 flow가 많아 seq10보다 seq3가 온라인 ready flow를 만들기 쉽고, VL2는 큰 flow tail이 있어 seq10 실험이 가능하지만 실행 시간이 길어질 수 있다.

---

## 12. 온라인 XDP / Redis / step_GRU 내부 흐름

온라인 경로는 pcap 후처리 없이 live packet event를 바로 모델 추론 요청으로 바꾼다.

```
xdp/tg_xdp_capture.py
  └─ BCC XDP program attach
  └─ TCP/IP header + TrafficGenerator payload prefix 추출
  └─ XdpPacketEvent 생성
      ↓
pipeline/realtime/online_tg_flow_cache.py
  └─ payload metadata에서 flow_id, direction 복원
  └─ client/server IP:port + flow_id + direction으로 RealtimeFlowCache에 packet feature 누적
  └─ feature_packet_count 도달 시 ready entry 생성
      ↓
pipeline/realtime/online_request.py
  └─ 모델 입력 payload(feature_names, x, seq_len) 생성
      ↓
pipeline/redis/transport.py
  └─ Redis Stream flow_features에 XADD
      ↓
model/step_GRU/stream_worker.py
  └─ consumer group으로 request 소비
  └─ weights.pt 로드 후 FlowClassifier 추론
  └─ Redis Pub/Sub flow_results로 결과 publish
      ↓
pipeline/redis/result_subscriber.py
  └─ RealtimeFlowCache 상태를 elephant/mice로 갱신
```

### 온라인 RealtimeFlowCache 상태

온라인 상태는 다음처럼 변한다.

| 상태 | 의미 |
|---|---|
| `default` | 아직 모델 요청을 보내지 않은 flow |
| `pending` | Redis Stream으로 전송했고 결과 대기 중 |
| `elephant` | 모델이 elephant로 분류 |
| `mice` | 모델이 mice로 분류 |

`pending` 이후에는 같은 flow를 다시 publish하지 않아 중복 추론을 막는다.

### Redis 사용 방식

| 방향 | Redis 구조 | 이유 |
|---|---|---|
| pipeline -> model | Stream `flow_features` | 요청 보존, consumer group, scale-out 가능 |
| model -> pipeline | Pub/Sub `flow_results` | 최신 결과를 가볍게 실시간 전달 |
| latency 분석 | Stream `flow_features:latency` | XADD 시작/완료 시각을 별도 기록 |

Mininet host namespace에서 `127.0.0.1`은 host OS가 아니라 각 namespace의 loopback을 가리킬 수 있으므로, 실제 실험에서는 Redis UDS를 사용한다.

### step_GRU 런타임

`model/step_GRU/stream_worker.py`는 `weights.pt`만으로 모델 크기를 추론해 `DynamicPacketGRU`를 복원한다.

주요 구성:

- `model/step_GRU/models.py`: `DynamicPacketGRU`
- `model/step_GRU/inference.py`: `load_model`, `FlowClassifier`
- `model/step_GRU/dataset_catalog.py`: dataset/weights 경로 해석
- `model/step_GRU/metrics.py`: 정확도 지표 로그 유틸

CUDA 사용 시 첫 추론은 context 초기화와 kernel warm-up 때문에 크게 튈 수 있다. steady-state latency는 첫 결과를 제외한 p50/p95로 해석한다.
