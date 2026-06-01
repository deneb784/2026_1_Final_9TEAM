# Capstone Online Flow Classification

Mininet TrafficGenerator 트래픽을 XDP로 실시간 캡처하고, Redis Stream을 통해 `step_GRU` 모델 worker가 flow를 `elephant` / `mice`로 분류하는 프로젝트입니다.

이 문서는 환경 준비, 실행 명령, 결과 확인 절차를 다룹니다. 내부 구조와 설계 흐름은 `docs/OVERVIEW.md`, XDP/Redis 실시간 경로의 세부 구조는 `docs/REALTIME_PACKET_REDIS_PIPELINE.md`를 참고합니다.

## 필수 요구사항

- Linux + sudo 권한
- Mininet
- Docker
- Ryu Docker image: `ryu-py3`
- BCC/XDP 실행 환경
- Redis server / `redis-cli`
- Python 3.12
- PyTorch, redis-py
- TrafficGenerator 로컬 checkout

시스템 패키지 예시:

```bash
sudo apt install -y mininet tshark docker.io redis-server
```

Ryu Docker image는 최초 1회 빌드합니다.

```bash
sudo docker build -f ryu_Dockerfile -t ryu-py3 .
```

Python 패키지:

```bash
pip install -r requirements_redis.txt
```

CUDA를 쓰려면 현재 환경의 GPU/CUDA에 맞는 PyTorch를 설치해야 합니다. CPU용 conda 예시는 `environment_capstone_gru.yml`에 있습니다.

## TrafficGenerator 준비

이 저장소는 외부 TrafficGenerator 코드를 포함하지 않습니다. 원본은 별도로 받아서
repo root의 `TrafficGenerator/` 경로에 배치해야 합니다.

```bash
git clone https://github.com/HKUST-SING/TrafficGenerator.git TrafficGenerator
```

TrafficGenerator는 다음 논문에서 사용된 도구입니다.

```text
Enabling ECN in Multi-Service Multi-Queue Data Centers
Wei Bai, Li Chen, Kai Chen, Haitao Wu
USENIX NSDI 2016
```

이 프로젝트의 온라인 XDP/tshark 파이프라인은 logical flow를 복원하기 위해
TrafficGenerator payload 앞부분의 metadata를 읽습니다. 따라서 원본 TrafficGenerator에
capstone 실험용 local modification을 적용해야 합니다. 필요한 변경 요약은
`docs/TRAFFIC_GENERATOR_MODIFICATIONS.md`를 참고하세요.

수정 후 빌드:

```bash
cd TrafficGenerator
make
cd -
```

`main.py`는 실행 시 `TrafficGenerator/conf/src*_config.txt`를 생성하고
`TrafficGenerator/bin/client`, `TrafficGenerator/bin/server`를 실행합니다.

## Conda 환경

CPU 기준 기본 환경:

```bash
conda env create -f environment_capstone_gru.yml
conda activate capstone-gru
pip install -r requirements_redis.txt
```

CUDA 환경에서는 conda 환경을 만든 뒤, 현재 GPU 드라이버/CUDA 버전에 맞는 PyTorch를 다시 설치합니다. 예시는 CUDA 12.1 기준입니다.

```bash
conda activate capstone-gru
pip uninstall -y torch
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements_redis.txt
```

CUDA 확인:

```bash
python3 - <<'PY'
import torch
print(torch.__version__)
print("cuda_available:", torch.cuda.is_available())
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu")
PY
```

## 데이터셋/가중치 위치

기본 가중치 경로:

```text
dataset/elephant_dst_to_src/{dctcp,fb,vl2,univ1}/seq{3,5,10}/weights.pt
```

예시:

```text
dataset/elephant_dst_to_src/vl2/seq10/weights.pt
dataset/elephant_dst_to_src/fb/seq3/weights.pt
```

FB 트래픽은 작은 flow가 많아서 `seq10`에서는 ready flow가 거의 안 생길 수 있습니다. FB는 먼저 `seq3`로 테스트하는 것을 권장합니다.

## 데이터셋 생성

오프라인 데이터셋 생성은 `results/flows_*_meta.csv`와 `captured_packet/*.pcap`을 매칭해서 JSONL을 만듭니다. 실시간 XDP run은 기본적으로 pcap을 저장하지 않으므로, 데이터셋 생성용 pcap run은 `--capture-mode pcap` 또는 검증용 `--capture-mode xdp-verify`를 사용합니다. 실시간 tshark 비교 run은 `--capture-mode tshark`를 사용합니다.

### 1. Mininet 트래픽 + pcap 생성

예시: FB pcap 생성

```bash
sudo python3 main.py 100 \
  --run-id fb_dataset_100 \
  --cdf-file FB_CDF.txt \
  --capture-mode pcap \
  --feature-packet-count 10
```

생성 위치:

```text
runs/mininet_fb_dataset_100/results/
runs/mininet_fb_dataset_100/captured_packet/
```

### 2. 단일 run에서 JSONL 생성

```bash
python3 -m pipeline.dataset.dataset_builder \
  --results-dir runs/mininet_fb_dataset_100/results \
  --pcap-dir runs/mininet_fb_dataset_100/captured_packet \
  --packet-count 10 \
  --direction-filter dst_to_src \
  --output dataset/fb_seq10_dataset.jsonl
```

주요 옵션:

- `--packet-count`: sample당 초기 packet 개수
- `--direction-filter dst_to_src`: 응답 방향만 사용
- `--sample-mode direction`: 방향별 sample 생성, 기본값
- `--raw-sequences`: padding 없이 관측된 packet만 저장
- `--label-threshold`: elephant 기준 bytes를 직접 지정

### 3. 여러 run 병합

```bash
python3 -m pipeline.dataset.merge_mininet_runs \
  runs/mininet_fb_dataset_100 \
  runs/mininet_fb_dataset_101 \
  --packet-count 10 \
  --direction-filter dst_to_src \
  --output dataset/fb_seq10_merged.jsonl
```

병합 시 전체 sample 기준으로 label threshold가 다시 계산됩니다.

### 4. 라벨 재계산

```bash
python3 -m pipeline.dataset.relabel_dataset \
  dataset/fb_seq10_merged.jsonl \
  --output dataset/fb_seq10_relabel_p90.jsonl \
  --quantile 0.90 \
  --label-size-field directional_size_bytes
```

### 5. UNI1 trace 데이터셋

UNI1 pcap trace는 전용 builder를 사용합니다.

```bash
python3 -m pipeline.dataset.univ1_dataset_builder \
  --pcap-dir data/univ1_trace \
  --packet-count 10 \
  --dataset-out dataset/univ1_seq10_dataset.jsonl \
  --stats-out analyze/univ1_flow_stats.csv \
  --summary-out analyze/univ1_summary.json \
  --cdf-out TrafficGenerator/conf/UNI1_CDF.txt
```

## Redis 실행

Mininet namespace에서 host Redis에 안정적으로 붙기 위해 UDS를 사용합니다.

```bash
redis-server \
  --port 0 \
  --unixsocket /tmp/capstone-redis.sock \
  --unixsocketperm 777 \
  --daemonize yes
```

확인:

```bash
redis-cli -s /tmp/capstone-redis.sock PING
```

Stream 초기화:

```bash
redis-cli -s /tmp/capstone-redis.sock DEL flow_features flow_features:latency
```

Redis 종료:

```bash
redis-cli -s /tmp/capstone-redis.sock SHUTDOWN
```

## 실행 방법

터미널을 최소 2개 사용합니다. Redis를 직접 띄우는 터미널까지 포함하면 3개입니다.

### 1. 모델 worker 실행

VL2 seq10 + CUDA:

```bash
python3 -m model.step_GRU.stream_worker \
  --redis-url 'unix:///tmp/capstone-redis.sock?db=0' \
  --stream flow_features \
  --response-channel flow_results \
  --model-path dataset/elephant_dst_to_src/vl2/seq10/weights.pt \
  --threshold 0.5 \
  --device cuda \
  --read-count 8 \
  --quiet-results \
  --idle-timeout-sec 10 \
  --result-log runs/step_gru/vl2_online_results.jsonl
```

FB seq3 + threshold 0.8:

```bash
python3 -m model.step_GRU.stream_worker \
  --redis-url 'unix:///tmp/capstone-redis.sock?db=0' \
  --stream flow_features \
  --response-channel flow_results \
  --model-path dataset/elephant_dst_to_src/fb/seq3/weights.pt \
  --threshold 0.8 \
  --device cuda \
  --read-count 8 \
  --quiet-results \
  --idle-timeout-sec 10 \
  --result-log runs/step_gru/fb_threshold08_online_results.jsonl
```

GPU가 없으면 `--device cpu`를 사용합니다. `--device auto`는 CUDA 가능 시 자동으로 CUDA를 선택합니다.

### 2. 실시간 파이프라인 실행

VL2 seq10:

```bash
sudo python3 main.py 50 \
  --run-id vl2_cuda_50 \
  --cdf-file VL2_CDF.txt \
  --capture-mode xdp \
  --feature-packet-count 10 \
  --redis-url 'unix:///tmp/capstone-redis.sock?db=0' \
  --redis-stream flow_features \
  --redis-response-channel flow_results \
  --publish-direction dst_to_src
```

FB seq3:

```bash
sudo python3 main.py 100 \
  --run-id fb_seq3_th08_100 \
  --cdf-file FB_CDF.txt \
  --capture-mode xdp \
  --feature-packet-count 3 \
  --redis-url 'unix:///tmp/capstone-redis.sock?db=0' \
  --redis-stream flow_features \
  --redis-response-channel flow_results \
  --publish-direction dst_to_src
```

`main.py N`의 `N`은 각 source client가 생성하는 요청 수입니다. 전체 결과 수는 ready 조건을 만족한 `dst_to_src` flow 수에 따라 달라집니다.

## 실행 확인

Stream 길이 확인:

```bash
watch -n 1 'redis-cli -s /tmp/capstone-redis.sock XLEN flow_features; redis-cli -s /tmp/capstone-redis.sock XLEN flow_features:latency'
```

최근 모델 입력 확인:

```bash
redis-cli -s /tmp/capstone-redis.sock --raw XREVRANGE flow_features + - COUNT 1
```

모델 worker 정상 출력 예:

```text
[result] stream_id=... flow=3:1:dst_to_src score=0.9993 label=elephant
```

결과 로그:

```text
runs/step_gru/*.jsonl
```

각 줄에는 `online_flow_key`, `run_id`, `logical_flow_id`, `inference_ms`, `score`, `predicted_label`, `threshold`, `exit_step`, end-to-end latency 필드가 저장됩니다.

## 결과 요약 명령

```bash
python3 - <<'PY'
import json, statistics
p = "runs/step_gru/fb_threshold08_online_results.jsonl"
run_id = "fb_seq3_th08_100"
rows = [json.loads(line) for line in open(p) if run_id in line]
xs = [r["inference_ms"] for r in rows]
ys = xs[1:] if len(xs) > 1 else xs
print("count", len(xs))
print("first", xs[0] if xs else None)
print("steady_p50", statistics.median(ys) if ys else None)
print("steady_p95", sorted(ys)[int(len(ys)*0.95)-1] if len(ys) >= 20 else None)
print("steady_max", max(ys) if ys else None)
print("labels", {label: sum(r["predicted_label"] == label for r in rows) for label in ("elephant", "mice")})
PY
```

첫 CUDA 추론은 warm-up 때문에 크게 튈 수 있습니다. 성능 해석은 첫 1개를 제외한 p50/p95를 기준으로 보는 것이 좋습니다.

## End-to-end 실시간 동작 증명

`--capture-mode xdp`, `--capture-mode tshark`, 또는 `xdp-verify`에서 Redis를 켜면 `main.py`가 기본적으로 다음 파일을 남깁니다.

```text
runs/mininet_<run_id>/results/online_classification_latency.jsonl
```

이 JSONL의 각 줄은 `flow_features` Stream 전송, worker 추론, `flow_results` Pub/Sub 수신, `FlowCache` 반영까지 끝난 flow 하나를 의미합니다. 즉 줄 수가 `classified` 수와 같으면 online loop가 실제로 cache까지 돌아온 것입니다.

요약:

```bash
python3 analyze/online_e2e_latency.py \
  --classification-log runs/mininet_fb_seq3_th08_100/results/online_classification_latency.jsonl \
  --run-id fb_seq3_th08_100
```

주요 지표:

- `ready_to_cache_updated_ms`: feature ready부터 FlowCache 반영 완료까지의 end-to-end 시간
- `ready_to_worker_received_ms`: feature ready부터 worker가 Redis Stream 항목을 받은 시간
- `worker_received_to_done_ms`: worker 수신부터 모델 추론 완료까지
- `pubsub_publish_to_subscriber_ms`: worker가 Pub/Sub publish를 시작한 뒤 subscriber가 받은 시간
- `subscriber_to_cache_updated_ms`: subscriber 수신부터 FlowCache 반영 완료까지

## 테스트

```bash
pytest -q
```

권한 문제로 `__pycache__` 생성이 막히면:

```bash
PYTHONPYCACHEPREFIX=/tmp/capstone_pycache pytest -q
```

## 라이선스

이 capstone 팀이 작성한 코드와 문서는 `LICENSE`에 명시된 MIT License를 따릅니다.

`TrafficGenerator/` 디렉터리는 외부 third-party 코드에 capstone 실험용 수정이 더해진
로컬 의존성입니다. 이 프로젝트는 TrafficGenerator 원본 코드를 포함하거나 재라이선스하지
않습니다. 자세한 내용은 `THIRD_PARTY_NOTICES.md`와
`docs/TRAFFIC_GENERATOR_MODIFICATIONS.md`를 참고하세요.

## 참고

- 내부 구조/설계: `docs/OVERVIEW.md`
- XDP/Redis 실시간 경로 구조 참고: `docs/REALTIME_PACKET_REDIS_PIPELINE.md`
- TrafficGenerator 원본: https://github.com/HKUST-SING/TrafficGenerator
