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

### 예시 실행
```bash
sudo python3 main.py 5001 20 4
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
  ├─ 7. PacketCapturer.stop() → tshark 종료 후 src IP 기준 후처리 필터링
  │       └─ captured_packet/{interface}.pcap 저장 완료
  │
  └─ 8. 결과 분석 (TrafficGenerator/src/script/result.py)
```

---

## 2. Topology 생성 (`topoBuilder.py` / `fatTreeBuilder`)

### Input
| 파라미터 | 타입 | 설명 |
|---|---|---|
| `k` | `int` | Fat-Tree 파라미터 (기본값 4, 짝수) |

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
| Edge ↔ Agg | 10 Mbps |
| Agg ↔ Core | 10 Mbps |

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

> **Table-miss 룰 불필요**: `autoStaticArp=True`로 ARP가 Mininet에서 자동 처리되고,
> ECMP 룰이 모든 IP 트래픽을 커버하므로 매칭되지 않는 패킷이 발생하지 않음

### `rule_ecmp.install_rules(datapath, logger, k)`

| | 내용 |
|---|---|
| **Input** | `datapath` (연결된 스위치), `logger`, `k` (Fat-Tree 파라미터) |
| **Output** | DPID 범위로 스위치 타입 판별 후 OpenFlow 룰 설치 |

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
| `port` | `int` | TCP 포트 번호 |
| `flowsPerPair` | `int` | src-dst 쌍 당 전송할 플로우 수 |

### Output
| 결과물 | 경로 | 설명 |
|---|---|---|
| 플로우 결과 | `results/flows_{srcIndex}.txt` | 각 src 호스트의 플로우 완료 로그 |
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
  -b 900                          # 목표 대역폭 (Mbps)
  -c conf/src{N}_config.txt       # 설정 파일
  -n {flowsPerPair}               # 전송할 플로우 수
  -l results/flows_{N}.txt        # 결과 저장 파일
  -s 1                            # 랜덤 시드
  -v                              # verbose
```
- 모든 src 호스트가 `multiprocessing.Process`로 동시에 실행
- `process.join()`으로 모든 src 완료 대기

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
| `src_ips` | `tuple[str, ...]` | 캡처할 src IP 목록 (후처리 필터링에 사용) |

### `PacketCapturer.__init__`
| Input | 타입 | 설명 |
|---|---|---|
| `capture_points` | `list[CapturePoint]` | 캡처 지점 목록 |
| `output_dir` | `str` | pcap 파일 저장 디렉터리 (기본값: `captured_packet`) |
| `server_port` | `int \| None` | BPF 필터에 사용할 포트 번호 (None이면 전체 트래픽) |

### `PacketCapturer.start()`
| | 내용 |
|---|---|
| **Input** | 없음 |
| **Output** | 각 캡처 지점마다 tshark subprocess 실행, pcap 파일 생성 시작 |

**실행되는 tshark 명령**
```bash
tshark -i {interface} -w {output_dir}/{interface}.pcap \
  -f "tcp and port {server_port}"
```
> BPF의 `src host` 필터는 OVS 인터페이스에서 동작하지 않을 수 있으므로,
> src IP 필터링은 `stop()` 시점의 후처리로 수행

### `PacketCapturer.stop()`
| | 내용 |
|---|---|
| **Input** | 없음 |
| **Output** | tshark 종료 후 src IP 기준 후처리 필터링, pcap 덮어쓰기 |

**후처리 필터링 과정**
```
① tshark subprocess SIGINT 전송 → 2초 대기 → 미종료 시 kill
② 각 pcap 파일에 대해:
   tshark -r {pcap} -Y "ip.src == {host_ip}" -w {pcap}.tmp
③ tmp 파일로 원본 덮어쓰기
```

### 캡처 지점 구성 (k=4 기준)
- 대상: 모든 edge 스위치의 host-facing 인터페이스 (`eth1`, `eth2`)
- 각 인터페이스에 연결된 호스트 IP를 `src_ips`로 지정
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

### pcap 파일에 포함된 정보
| 필드 | 설명 |
|---|---|
| `frame.time_epoch` | 캡처 타임스탬프 |
| `eth.src / eth.dst` | MAC 주소 |
| `ip.src / ip.dst` | IP 주소 |
| `tcp.srcport / tcp.dstport` | TCP 포트 |
| `frame.len` | 전체 프레임 크기 (bytes) |
| `tcp.len` | TCP payload 크기 (bytes) |
| `tcp.flags` | TCP 플래그 (SYN, ACK 등) |

### pcap 파일 예시 (Wireshark로 조회)
<img width="1536" height="674" alt="image" src="https://github.com/user-attachments/assets/22632fb7-f27c-49d3-b016-95a5ea8fbf5d" />
