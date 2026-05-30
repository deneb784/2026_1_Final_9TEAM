# Project File Guide

이 문서는 capstone workspace에서 각 폴더와 주요 파일이 어떤 역할을 하는지 빠르게 파악하기 위한 안내서다.

## 전체 흐름

현재 프로젝트의 큰 흐름은 다음과 같다.

```text
Mininet + TrafficGenerator
  -> 패킷 생성
  -> XDP 또는 tshark로 캡처
  -> flow feature 생성
  -> step_GRU 모델 추론
  -> FlowCache에 분류 결과 반영
  -> latency / FCT-before 분석
```

실시간 온라인 경로는 다음 흐름이다.

```text
xdp/tg_xdp_capture.py
  -> pipeline/realtime/*
  -> pipeline/redis/transport.py
  -> model/step_GRU/stream_worker.py
  -> pipeline/redis/result_subscriber.py
  -> online_classification_latency.jsonl
```

오프라인 데이터셋 생성 경로는 다음 흐름이다.

```text
runs/*/results/flows_*_meta.csv
runs/*/captured_packet/*.pcap
  -> pipeline/dataset/*
  -> dataset/*.jsonl
  -> model/step_GRU/*
```

## 최상위 파일

| 경로 | 역할 |
|---|---|
| `README.md` | 실험 실행 방법, Redis 실행, worker 실행, XDP 실시간 파이프라인 실행 명령 정리 |
| `main.py` | Mininet topology 구성, TrafficGenerator 실행, capture 모드 실행을 묶는 메인 엔트리포인트 |
| `requirements_redis.txt` | Redis 기반 온라인 파이프라인 실행에 필요한 Python 패키지 |
| `environment_capstone_gru.yml` | GRU 모델 학습/추론용 conda 환경 정의 |
| `pytest.ini` | 테스트 실행 설정 |
| `dump.rdb` | Redis가 만든 로컬 DB 스냅샷 파일. 코드 파일이 아니므로 보통 커밋 대상에서 제외하는 것이 좋음 |

## `xdp/`

XDP 기반 온라인 패킷 캡처와 feature-ready trigger를 담당한다.

| 경로 | 역할 |
|---|---|
| `xdp/tg_xdp_capture.py` | XDP 프로그램을 BCC로 로드하고, perf event callback에서 TrafficGenerator packet을 해석한다. flow가 ready 상태가 되면 온라인 추론 request를 만들고 Redis Stream으로 전달한다. 현재는 `queue.Queue` + publisher thread 구조로 Redis publish를 callback 밖에서 처리한다. |

주요 책임:

- XDP C 프로그램 정의
- TCP/IP header와 payload prefix 추출
- TrafficGenerator metadata 해석
- online flow cache 업데이트
- feature-ready 시 request 생성
- Redis result subscriber를 통해 classifier 결과를 FlowCache에 반영
- `online_classification_latency.jsonl`에 end-to-end latency 기록

## `pipeline/realtime/`

실시간 캡처 경로에서 flow 상태를 관리하고 모델 request를 만든다.

| 경로 | 역할 |
|---|---|
| `pipeline/realtime/online_tg_flow_cache.py` | TrafficGenerator payload metadata를 이용해 실시간 flow를 식별한다. online packet event를 flow별로 모으고, feature packet count에 도달했는지 판단한다. |
| `pipeline/realtime/online_flow_cache.py` | 실시간 FlowCache의 기본 자료구조. classification 결과를 기존 flow entry에 되붙이는 역할을 한다. |
| `pipeline/realtime/online_request.py` | ready flow entry를 step_GRU worker가 읽을 수 있는 request dict로 변환한다. `x`, `seq_len`, `feature_names`, `producer_metrics`를 구성한다. |

## `pipeline/redis/`

Redis를 이용해 capture process와 model worker를 비동기적으로 분리한다.

| 경로 | 역할 |
|---|---|
| `pipeline/redis/transport.py` | `RedisStreamProducer`. 모델 추론 request를 Redis Stream에 `XADD`로 넣는다. latency 분석용 보조 stream도 기록한다. |
| `pipeline/redis/result_subscriber.py` | worker가 Pub/Sub으로 발행한 분류 결과를 subscribe하고 capture 쪽 callback으로 전달한다. |

Redis 사용 목적:

- XDP capture와 model inference를 서로 막히지 않게 분리
- 모델 worker를 별도 프로세스로 실행
- request를 stream에 쌓아 실험 중 유실 가능성을 줄임
- latency 분석용 field를 함께 기록

## `model/step_GRU/`

GRU 모델 정의, 학습/추론, Redis Stream worker를 포함한다.

| 경로 | 역할 |
|---|---|
| `model/step_GRU/models.py` | step_GRU 모델 구조 정의 |
| `model/step_GRU/data.py` | 학습/평가 데이터 로딩 관련 코드 |
| `model/step_GRU/inference.py` | 저장된 weights를 로드하고 단일 flow sequence를 분류하는 `FlowClassifier` 구현 |
| `model/step_GRU/stream_worker.py` | Redis Stream에서 온라인 request를 읽고 모델 추론을 수행한다. 결과는 Redis Pub/Sub으로 publish한다. `--read-count`, `--quiet-results`, warmup 등 latency 실험용 옵션이 있다. |
| `model/step_GRU/dataset_catalog.py` | dataset type, sequence length, weights path를 찾기 위한 catalog/helper |
| `model/step_GRU/metrics.py` | 모델 평가 metric 관련 코드 |
| `model/step_GRU/*.ipynb` | 학습/실험용 notebook |

온라인 실행에서 가장 중요한 파일:

```text
model/step_GRU/stream_worker.py
```

이 worker가 하는 일:

```text
Redis Stream에서 request 수신
  -> JSON payload 복원
  -> feature schema를 모델 입력에 맞춤
  -> GRU inference
  -> 결과를 Redis Pub/Sub으로 publish
  -> Stream entry ack
```

## `pipeline/dataset/`

pcap과 flow metadata를 이용해 오프라인 학습/평가용 JSONL dataset을 만든다.

| 경로 | 역할 |
|---|---|
| `pipeline/dataset/meta_loader.py` | `flows_*_meta.csv`를 읽어 request metadata로 변환 |
| `pipeline/dataset/packet_loader.py` | pcap/csv에서 packet row를 읽는 코드 |
| `pipeline/dataset/matcher.py` | packet을 `flows_*_meta.csv`의 request 구간과 매칭 |
| `pipeline/dataset/feature_extractor.py` | packet sequence에서 모델 입력 feature를 추출 |
| `pipeline/dataset/dataset_builder.py` | Mininet run 결과와 pcap을 합쳐 학습용 JSONL dataset 생성 |
| `pipeline/dataset/merge_mininet_runs.py` | 여러 Mininet run에서 만든 dataset을 병합 |
| `pipeline/dataset/relabel_dataset.py` | threshold 기준으로 elephant/mice label 재부여 |
| `pipeline/dataset/univ1_dataset_builder.py` | Univ1 trace용 dataset 생성 |
| `pipeline/dataset/pipeline.py` | dataset 생성 흐름을 묶는 보조 코드 |

## `pipeline/`

공통 모델과 offline flow cache가 있다.

| 경로 | 역할 |
|---|---|
| `pipeline/models.py` | request metadata, packet, flow entry 등 공통 dataclass 정의 |
| `pipeline/dataset/flow_cache.py` | 오프라인 dataset 생성용 FlowCache. `(src_index, flow_id, direction)` 기준으로 packet을 누적 |
| `pipeline/README.md` | pipeline 모듈 설명 |

주의:

- `pipeline/dataset/flow_cache.py`는 주로 오프라인 dataset 생성용이다.
- `pipeline/realtime/online_flow_cache.py`와 `online_tg_flow_cache.py`는 실시간 온라인 캡처 경로용이다.

## `TrafficGenerator/`

Mininet 안에서 request/response traffic을 생성하는 C 기반 traffic generator다.

| 경로 | 역할 |
|---|---|
| `TrafficGenerator/src/client/client.c` | client request 생성, FCT 측정, `flows_*.txt`, `flows_*_meta.csv` 기록 |
| `TrafficGenerator/src/server/*.c` 또는 관련 source | server 쪽 traffic 처리 |
| `TrafficGenerator/bin/client` | 빌드된 client 실행 파일 |
| `TrafficGenerator/bin/server` | 빌드된 server 실행 파일 |
| `TrafficGenerator/bin/result.py` | FCT 결과 후처리 스크립트 |
| `TrafficGenerator/conf/FB_CDF.txt` | FB workload flow size 분포 |
| `TrafficGenerator/conf/VL2_CDF.txt` | VL2 workload flow size 분포 |
| `TrafficGenerator/conf/DCTCP_CDF.txt` | DCTCP workload flow size 분포 |
| `TrafficGenerator/conf/src0_config.txt` ~ `src7_config.txt` | source host별 TrafficGenerator 설정 |

중요한 결과 파일:

```text
flows_*.txt
flows_*_meta.csv
```

`flows_*.txt`:

```text
size_bytes fct_us dscp rate_mbps goodput_mbps
```

`flows_*_meta.csv`:

```text
src_index, flow_id, src_ip, src_port, dst_ip, dst_port,
start_time_us, stop_time_us, fct_us, ...
```

FCT-before 분석에는 `flows_*_meta.csv`의 `stop_time_us`를 사용한다.

## `network/`

Mininet topology, traffic generator 실행, packet capture 실행을 감싸는 Python 코드다.

| 경로 | 역할 |
|---|---|
| `network/topology.py` | Mininet topology 구성 |
| `network/controller.py` | controller 실행/연동 관련 코드 |
| `network/rule_ecmp.py` | ECMP rule 설정 관련 코드 |
| `network/traffic_generator.py` | TrafficGenerator client/server 실행 관리 |
| `network/packet_capture.py` | tshark/pcap capture 실행 관리 |

`main.py`가 이 모듈들을 호출해 실험을 실행한다.

## `analyze/`

실험 결과를 분석하는 스크립트 모음이다.

| 경로 | 역할 |
|---|---|
| `analyze/online_e2e_latency.py` | `online_classification_latency.jsonl`을 읽어 online classification latency를 요약한다. p50/p95/p99/max를 출력한다. |
| `analyze/online_fct_deadline.py` | `online_classification_latency.jsonl`과 `flows_*_meta.csv`를 join해서 FCT 전에 cache update가 끝났는지 분석한다. |
| `analyze/redis_stream_latency.py` | Redis Stream과 latency 보조 stream을 직접 읽어 Redis publish 관련 latency를 분석한다. |
| `analyze/verify_xdp_tshark_capture.py` | XDP capture 결과와 tshark/pcap 기반 결과를 비교 검증한다. |
| `analyze/pcap_to_csv.py` | pcap을 CSV 형태로 변환 |
| `analyze/compare_cdfs.py` | workload CDF 비교/시각화 |
| `analyze/ecmp_verification.py` | ECMP 설정 검증 |
| `analyze/cdf_comparison.svg` | CDF 비교 결과 그림 |

자주 쓰는 분석 명령:

```bash
python3 analyze/online_e2e_latency.py \
  --classification-log runs/mininet_vl2_cuda_50/results/online_classification_latency.jsonl \
  --run-id vl2_cuda_50
```

```bash
python3 analyze/online_fct_deadline.py \
  --classification-log runs/mininet_vl2_cuda_50/results/online_classification_latency.jsonl \
  --flow-meta 'runs/mininet_vl2_cuda_50/results/flows_*_meta.csv' \
  --run-id vl2_cuda_50
```

## `docs/`

프로젝트 설명 문서가 들어 있다.

| 경로 | 역할 |
|---|---|
| `docs/OVERVIEW.md` | 전체 시스템 구조와 데이터셋 생성 흐름 설명 |
| `docs/REALTIME_PACKET_REDIS_PIPELINE.md` | 실시간 packet -> Redis -> model worker 파이프라인 상세 설명 |
| `docs/XDP_ONLINE_PIPELINE_LATENCY_SUMMARY.md` | XDP 온라인 파이프라인 병목 분석과 개선 후 latency 결과 정리 |
| `docs/PROJECT_FILE_GUIDE.md` | 현재 문서. 폴더/파일별 역할 요약 |

## `runs/`

실험 실행 결과가 저장되는 폴더다.

예시:

```text
runs/mininet_vl2_cuda_50/
  results/
    flows_0.txt
    flows_0_meta.csv
    log_0.txt
    online_classification_latency.jsonl
  captured_packet/
    ...
```

주요 파일:

| 파일 | 역할 |
|---|---|
| `runs/*/results/log_*.txt` | TrafficGenerator 실행 로그 |
| `runs/*/results/flows_*.txt` | source별 FCT 결과. 헤더 없음 |
| `runs/*/results/flows_*_meta.csv` | source별 flow metadata. FCT-before 분석에 필요 |
| `runs/*/results/online_classification_latency.jsonl` | XDP 온라인 분류 결과와 end-to-end latency 기록 |
| `runs/*/captured_packet/` | tshark 또는 검증 모드에서 저장한 pcap 파일 |
| `runs/step_gru/*.jsonl` | model worker가 남긴 결과 로그. latency 핵심 분석은 보통 `online_classification_latency.jsonl`을 사용 |

주의:

- `runs/` 아래 파일은 실험 산출물이다.
- 같은 `run-id`로 여러 번 실행하면 로그가 섞일 수 있으므로, latency/FCT 분석 전에는 기존 결과를 정리하는 것이 좋다.

## `dataset/`

학습된 모델 weights와 dataset 관련 파일이 들어 있다.

예시:

```text
dataset/elephant_dst_to_src/vl2/seq10/weights.pt
dataset/elephant_dst_to_src/fb/seq3/weights.pt
```

역할:

- step_GRU worker 실행 시 `--model-path`로 weights를 지정
- dataset builder가 만든 JSONL 학습 데이터 저장

## `data/`

원본 trace 또는 압축된 실험 데이터가 들어 있다.

예시:

```text
data/runs_fb_payload_only_4loads.tgz
data/runs_vl2_payload_only_4loads.tgz
data/runs_dctcp_payload_only_4loads.tgz
```

## `tests/`

단위 테스트가 들어 있다.

| 경로 | 역할 |
|---|---|
| `tests/test_online_tg_flow_cache.py` | 실시간 TG flow cache 동작 검증 |
| `tests/test_online_request.py` | ready flow를 online request로 변환하는 코드 검증 |
| `tests/test_redis_transport.py` | Redis Stream producer field 구성 검증 |
| `tests/test_online_e2e_latency.py` | online latency summary 계산 검증 |
| `tests/test_step_gru_runtime.py` | step_GRU runtime 관련 검증 |
| `tests/test_step_gru_dataset_catalog.py` | dataset catalog path resolution 검증 |

자주 쓰는 테스트:

```bash
python3 -m unittest tests.test_redis_transport tests.test_online_request
```

## 생성물 / 캐시 폴더

| 경로 | 역할 |
|---|---|
| `__pycache__/`, `*/__pycache__/` | Python bytecode cache |
| `.pytest_cache/` | pytest cache |
| `captured_packet/` | 예전 또는 기본 capture output |
| `results/` | 예전 또는 기본 TrafficGenerator output |
| `splits/` | dataset split 결과 |

이 폴더들은 보통 코드 이해에는 중요하지 않고, 실험 산출물 또는 캐시로 보면 된다.

## 파일을 찾는 기준

온라인 실험 실행을 보고 싶을 때:

```text
README.md
main.py
xdp/tg_xdp_capture.py
model/step_GRU/stream_worker.py
pipeline/redis/*
pipeline/realtime/*
```

모델 추론을 보고 싶을 때:

```text
model/step_GRU/inference.py
model/step_GRU/stream_worker.py
model/step_GRU/models.py
```

데이터셋 생성을 보고 싶을 때:

```text
pipeline/dataset/*
pipeline/models.py
pipeline/dataset/flow_cache.py
```

FCT와 flow metadata를 보고 싶을 때:

```text
TrafficGenerator/src/client/client.c
runs/*/results/flows_*_meta.csv
analyze/online_fct_deadline.py
```

latency 결과를 보고 싶을 때:

```text
runs/*/results/online_classification_latency.jsonl
analyze/online_e2e_latency.py
docs/XDP_ONLINE_PIPELINE_LATENCY_SUMMARY.md
```

## 발표 준비용 핵심 파일

발표 직전에 주로 보면 되는 파일:

```text
docs/XDP_ONLINE_PIPELINE_LATENCY_SUMMARY.md
docs/PROJECT_FILE_GUIDE.md
README.md
analyze/online_e2e_latency.py
analyze/online_fct_deadline.py
runs/mininet_vl2_cuda_50/results/online_classification_latency.jsonl
runs/mininet_vl2_cuda_50/results/flows_*_meta.csv
```
