# UNI1 Trace Dataset Analysis

## 목적

`univ1_trace.tgz`에서 추출한 UNI1 packet trace를 프로젝트의 기존 feature format에 맞춰 가공하고, 기존 TrafficGenerator 기반 `dataset.jsonl`과 비교했다.

비교의 핵심 질문은 다음과 같다.

- UNI1 trace에서 만든 dataset이 기존 synthetic dataset과 어떤 차이를 갖는가?
- UNI1 flow-size CDF가 기존 `DCTCP_CDF.txt`와 얼마나 다른가?
- 모델 학습/검증 관점에서 어떤 주의점이 있는가?

## 입력 데이터

### UNI1 원본

- 원본: IMC 2010 데이터센터 trace의 `UNI1`
- 로컬 압축 파일: `univ1_trace.tgz`
- 압축 해제 위치: `data/univ1_trace/`
- 사용한 pcap: `univ1_pt1.pcap` ~ `univ1_pt20.pcap`
- 제외한 파일: `univ1_pt1_old.pcap`

`pt1`~`pt20`은 시간 순서로 이어지는 조각이다.

```text
pt1  : 2009-12-18 01:26:04 ~ 01:31:49
...
pt20 : 2009-12-18 02:30:25 ~ 02:31:19
```

전체적으로 약 65분 구간이며, 동일 시간 간격이 아니라 패킷 수나 파일 크기 기준으로 나뉜 조각에 가깝다.

## 생성된 산출물

| 파일 | 설명 |
|---|---|
| `dataset_univ1.jsonl` | UNI1 기반 학습용 JSONL dataset |
| `analyze/univ1_flow_stats.csv` | TCP stream별 flow 통계 |
| `analyze/univ1_summary.json` | UNI1 전체 요약 통계 |
| `TrafficGenerator/conf/UNI1_CDF.txt` | TrafficGenerator에서 사용할 수 있는 UNI1-derived request size CDF |
| `analyze/cdf_comparison.svg` | `DCTCP_CDF.txt`와 `UNI1_CDF.txt` 비교 그래프 |
| `analyze/compare_cdfs.py` | CDF 비교 SVG 생성 스크립트 |
| `model/EDA.ipynb` | 기존 dataset과 UNI1 dataset 비교 EDA 노트북 |

## UNI1 Dataset 생성 방식

기존 TrafficGenerator 데이터는 `results/flows_*_meta.csv`의 request metadata를 기준으로 flow를 식별한다. 반면 UNI1은 외부 packet trace이므로 프로젝트의 request metadata가 없다.

따라서 UNI1은 다음 방식으로 flow를 재구성했다.

- 대상 패킷: TCP packet only
- flow grouping 기준: `source_file + tcp.stream`
- sample 방향: 양방향 중 TCP payload byte가 더 큰 dominant direction
- flow size 기준: `max(src_to_dst_tcp_payload_bytes, dst_to_src_tcp_payload_bytes)`
- feature vector: 기존 dataset과 동일한 11개 feature
- packet count: 첫 8개 packet

사용한 feature는 다음과 같다.

```text
frame_len
ip_len
ip_ttl
tcp_payload_bytes
tcp_flags
tcp_window_size
iat_us
retransmission
out_of_order
duplicate_ack
fast_retransmission
```

## UNI1 전체 통계

`analyze/univ1_summary.json` 기준:

| 항목 | 값 |
|---|---:|
| TCP stream 수 | 183,519 |
| payload가 있는 flow 수 | 147,774 |
| dataset 후보 flow 수 | 85,657 |
| dataset elephant threshold | 31,877 bytes |
| dataset elephant 수 | 17,133 |
| 전체 flow p50 | 4,267 bytes |
| 전체 flow p80 | 13,176 bytes |
| 전체 flow p90 | 38,568 bytes |
| 전체 flow p95 | 95,767 bytes |
| 전체 flow p99 | 429,641 bytes |
| 전체 flow max | 682,769,160 bytes |
| dataset p50 | 11,314 bytes |
| dataset p80 | 31,877 bytes |
| dataset p90 | 82,422 bytes |
| dataset p95 | 159,066 bytes |
| dataset p99 | 839,978 bytes |

Elephant 기준은 dataset 후보 flow의 상위 20%로 잡았다.

```text
label 1 = flow_size_bytes >= 31,877
label 0 = flow_size_bytes < 31,877
```

## 기존 Dataset과 UNI1 Dataset 비교

### Label 분포

| dataset | label 0 | label 1 |
|---|---:|---:|
| TrafficGenerator | 983 | 126 |
| UNI1 | 68,524 | 17,133 |

UNI1은 의도대로 약 80:20 비율이다. 현재 브랜치의 기존 `dataset.jsonl`도 label 1이 일부 존재한다.

### Flow Size 통계

| dataset | label | count | mean bytes | median bytes | min | max |
|---|---:|---:|---:|---:|---:|---:|
| TrafficGenerator | 0 | 983 | 150,636 | 20 | 0 | 1,863,206 |
| TrafficGenerator | 1 | 126 | 7,054,253 | 5,660,984 | 2,273,254 | 19,655,248 |
| UNI1 | 0 | 68,524 | 10,565 | 11,131 | 1 | 31,869 |
| UNI1 | 1 | 17,133 | 540,011 | 82,422 | 31,877 | 682,769,160 |

해석:

- TrafficGenerator의 elephant는 MB 단위로 매우 크다.
- UNI1의 elephant는 상위 20% 기준이지만 median은 약 82KB 수준이다.
- UNI1에는 극단적으로 큰 outlier가 존재한다.
- 기존 synthetic dataset은 elephant와 non-elephant의 차이가 매우 뚜렷하고, UNI1은 더 넓고 불규칙한 실제 trace 분포를 갖는다.

### Direction 분포

| dataset | direction | label 0 | label 1 |
|---|---|---:|---:|
| TrafficGenerator | dst_to_src | 428 | 126 |
| TrafficGenerator | src_to_dst | 555 | 0 |
| UNI1 | dst_to_src | 59,731 | 15,854 |
| UNI1 | src_to_dst | 8,793 | 1,279 |

해석:

- TrafficGenerator의 elephant는 전부 `dst_to_src` 방향이다.
- 이는 generator 구조상 server response가 큰 payload를 보내기 때문이다.
- UNI1도 dominant direction 대부분은 `dst_to_src`이지만, `src_to_dst` elephant도 존재한다.
- 실제 trace는 synthetic traffic보다 방향성이 더 다양하다.

## Feature 평균 비교

| feature mean | TG label 0 | TG label 1 | UNI1 label 0 | UNI1 label 1 |
|---|---:|---:|---:|---:|
| frame_len | 624.29 | 1,354.76 | 715.74 | 904.12 |
| ip_len | 610.29 | 1,340.76 | 696.79 | 885.38 |
| ip_ttl | 64.00 | 64.00 | 69.96 | 78.07 |
| tcp_payload_bytes | 557.26 | 1,288.76 | 653.74 | 842.33 |
| tcp_flags | 17.31 | 17.74 | 18.85 | 18.68 |
| tcp_window_size | 1,270.21 | 85.00 | 60,813.79 | 49,987.41 |
| iat_us | 8,022.03 | 163.45 | 424,557.16 | 233,410.86 |
| retransmission | 0.0016 | 0.0000 | 0.0055 | 0.0025 |
| out_of_order | 0.0000 | 0.0000 | 0.0004 | 0.0007 |
| duplicate_ack | 0.0322 | 0.0000 | 0.0069 | 0.0014 |
| fast_retransmission | 0.0000 | 0.0000 | 0.0002 | 0.0004 |

주요 관찰:

- 두 dataset 모두 label 1에서 `frame_len`, `ip_len`, `tcp_payload_bytes`가 커지는 경향이 있다.
- TrafficGenerator label 1은 평균 payload가 1,288 bytes로 MTU에 가까운 큰 packet이 빠르게 이어진다.
- UNI1 label 1도 label 0보다 payload가 크지만 차이가 덜 극단적이다.
- `iat_us`는 TrafficGenerator가 훨씬 작다. 특히 label 1 평균 IAT가 약 163us로, 매우 촘촘한 전송 패턴이다.
- UNI1은 실제 trace라 IAT가 훨씬 크고 불규칙하다.
- TCP window size는 두 dataset 사이 scale이 크게 다르다. 이 feature는 OS, TCP stack, capture 위치, trace anonymization 환경의 영향을 받을 수 있어 직접 비교에 주의가 필요하다.

## CDF 비교

### Raw CDF 값

| percentile | DCTCP_CDF | UNI1_CDF |
|---|---:|---:|
| p50 | 80,000 이하 구간 | 4,267 |
| p80 | 2,000,000 | 13,176 |
| p90 | 5,000,000 | 38,568 |
| p95 | 10,000,000 이하 구간 | 95,767 |
| p99 | 30,000,000 | 429,641 |
| max | 30,000,000 | 682,769,160 |

정확한 CDF point는 다음 파일에 있다.

- `TrafficGenerator/conf/DCTCP_CDF.txt`
- `TrafficGenerator/conf/UNI1_CDF.txt`

비교 그래프:

- `analyze/cdf_comparison.svg`

### 해석

두 분포는 모두 heavy-tail 특성을 갖는다.

```text
작은 flow가 많고, 큰 flow는 적지만 tail에 존재한다.
```

하지만 scale과 skew가 크게 다르다.

- DCTCP CDF는 large-flow-heavy synthetic workload에 가깝다.
- UNI1 CDF는 small-flow-heavy real trace에 가깝다.
- DCTCP p80은 2MB이지만 UNI1 p80은 13KB 수준이다.
- UNI1은 대부분 작은 flow에 몰려 있고, 아주 드문 outlier가 긴 tail을 만든다.

정규화 관점에서도 max normalization은 UNI1의 극단적 outlier 때문에 해석력이 떨어진다. UNI1처럼 heavy-tail이 강한 trace는 p99 기준 정규화와 log x-axis가 더 적절하다.

## 기존 Dataset과 UNI1의 의미 차이

### TrafficGenerator Dataset

장점:

- 실험 조건을 통제할 수 있다.
- elephant와 non-elephant의 차이가 명확하다.
- 모델이 패턴을 빠르게 학습하기 쉽다.
- DCTCP-like 또는 synthetic benchmark 성격의 실험에 적합하다.

한계:

- 실제 trace보다 flow size 차이가 극단적이다.
- label 1이 주로 `dst_to_src`에만 존재한다.
- packet timing이 매우 규칙적이고 촘촘하다.
- 실제 환경 generalization을 보장하기 어렵다.

### UNI1 Dataset

장점:

- 실제 university data center trace 기반이다.
- small-flow-heavy + heavy-tail 구조를 반영한다.
- 방향, packet size, timing이 synthetic보다 다양하다.
- real-trace-derived validation dataset으로 적합하다.

한계:

- request metadata가 없으므로 `tcp.stream` 기준으로 flow를 재구성했다.
- capture 중간에 시작된 TCP stream이 포함될 수 있다.
- payload는 제거되어 application content 분석은 불가능하다.
- IP는 anonymized 되어 topology 의미를 복원할 수 없다.
- 2009년 trace이므로 현대 DCN traffic을 완전히 대표한다고 보긴 어렵다.

## 모델 학습 관점의 결론

기존 TrafficGenerator 데이터로만 학습하면 모델은 다음 규칙을 강하게 배울 가능성이 높다.

```text
큰 TCP payload가 매우 짧은 IAT로 연속 등장하면 elephant
```

이 규칙은 synthetic dataset에서는 잘 맞지만, UNI1에서는 더 약하게 나타난다. UNI1 elephant도 packet size가 큰 경향은 있지만, TrafficGenerator처럼 극단적으로 크거나 규칙적이지 않다.

따라서 실험은 다음처럼 분리해서 보는 것이 좋다.

| 목적 | 추천 데이터 |
|---|---|
| controlled synthetic benchmark | TrafficGenerator dataset |
| real-trace-derived validation | UNI1 dataset |
| generator workload 재현 | `UNI1_CDF.txt` |
| DCTCP baseline 비교 | `DCTCP_CDF.txt` vs `UNI1_CDF.txt` |

최종적으로는 다음 세 가지 실험이 필요하다.

1. TrafficGenerator dataset으로 학습하고 TrafficGenerator test set에서 평가
2. UNI1 dataset으로 학습하고 UNI1 split에서 평가
3. TrafficGenerator 학습 모델을 UNI1에 적용해 cross-domain 성능 확인

3번 결과가 낮다면, synthetic workload가 실제 trace를 충분히 대표하지 못한다는 근거가 된다.

## 재현 방법

UNI1 dataset 재생성:

```bash
python3 -m feature_pipeline.univ1_dataset_builder
```

CDF 비교 그래프 재생성:

```bash
python3 analyze/compare_cdfs.py
```

EDA 노트북 실행:

```bash
source ~/miniconda3/etc/profile.d/conda.sh
conda activate eda
cd /home/leesungwon/capstone
jupyter notebook
```

노트북 파일:

```text
model/EDA.ipynb
```

