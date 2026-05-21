# Elephant Flow Detection 모델 개선 방향

## 1. 목적

이 문서는 현재 GRU 기반 elephant flow 조기 탐지 모델의 개선 방향을 정리한다.

핵심 목표는 다음과 같다.

- 초기 최대 10개 packet feature만으로 elephant flow를 조기에 탐지한다.
- 실제 환경에 가까운 mice:elephant 약 9:1 불균형 분포에서도 recall, precision, F1-score를 유지한다.
- 공공 데이터셋으로 일반적인 flow 패턴을 먼저 학습한 뒤, Mininet 데이터셋으로 fine-tuning하여 온라인 실험 환경에 적응시킨다.
- 단순 accuracy가 아니라 elephant class 기준 precision, recall, F1, PR-AUC, early-exit latency를 함께 개선한다.

최종적으로 설명하고 싶은 구조는 다음과 같다.

> 공공 데이터셋으로 일반 flow 특성을 pretraining하고, Mininet 데이터셋으로 fine-tuning하여 프로젝트 실험 환경에 맞춘 online elephant flow detector를 만들었다.

## 2. 현재 코드 상태

현재 `feat/classifier` 브랜치 기준 GRU 모델 관련 파일은 다음과 같다.

| 파일 | 역할 |
|---|---|
| `model/GRU/models.py` | `DiffEarlyExitGRU` 모델 정의 |
| `model/GRU/GRU_Model_test.ipynb` | 단일 노트북 형태의 평가 코드 |
| `model/GRU/diff_early_exit_gru*.pth` | 저장된 GRU weight |
| `dataset_univ1_p90.jsonl` | UNI1 기반 p90 elephant label dataset |
| `dataset_mininet_merged_p90.jsonl` | Mininet 기반 p90 elephant label dataset |

현재 학습 스크립트는 별도 `.py` 파일로 존재하지 않는다. 따라서 실험 재현성과 fine-tuning 비교를 위해서는 train/evaluate 로직을 노트북에서 분리해 스크립트화해야 한다.

## 3. 현재 모델 구조

현재 모델은 `DiffEarlyExitGRU`이다.

입력:

- `direction_idx`: traffic direction
- `x`: flow 초기 packet sequence, shape `[batch, seq_len, 11]`

구조:

- `direction_embedding`: 방향 정보를 hidden vector로 변환
- `init_h_layer`: direction embedding과 첫 번째 packet feature로 초기 hidden state 생성
- `gru_cell`: 두 번째 packet부터 `x_t - x_{t-1}` 차이값을 입력으로 sequential update
- `classifier`: hidden state를 0~1 사이 CDF 예측값으로 변환
- `early exit`: 현재 예측값과 직전 예측값 차이가 tolerance 이하이면 조기 종료

현재 구조에서 layer는 다음처럼 구분할 수 있다.

| 구분 | Layer |
|---|---|
| Backbone | `direction_embedding`, `init_h_layer`, `gru_cell` |
| Output head | `classifier` |
| Non-parametric | `sigmoid` |

따라서 full fine-tuning, head-only fine-tuning, gradual unfreezing 모두 적용 가능하다.

## 4. 주요 문제

### 4.1 평가 기준 문제

현재 평가 노트북은 `predicted CDF >= 0.5`이면 elephant로 분류한다. 이 기준은 실제 elephant detection 목적과 맞지 않을 수 있다.

p90 기준 elephant라면 threshold는 0.5가 아니라 0.9 근처가 더 자연스럽다. 또한 현재 metric 계산은 JSONL의 `label`이 아니라 `true_cdf >= threshold` 기준으로 이루어진다. 따라서 실제 label 기준 elephant 탐지 성능과 다를 수 있다.

필요한 개선:

- `label` 기준 평가와 `CDF threshold` 기준 평가를 분리한다.
- threshold sweep을 추가한다.
- confusion matrix, PR-AUC, ROC-AUC를 출력한다.

### 4.2 데이터 불균형 문제

실제 목표 분포는 mice:elephant 약 9:1이다. 이 경우 accuracy만 보면 대부분 mice로 예측해도 높게 나올 수 있다.

필요한 개선:

- 9:1 test set에서 평가한다.
- elephant class 기준 precision, recall, F1-score를 주요 지표로 본다.
- PR-AUC를 반드시 확인한다. 불균형 데이터에서는 ROC-AUC보다 PR-AUC가 더 직접적이다.

현재 사용 가능한 9:1 데이터:

| 데이터셋 | 전체 sample | label 0 | label 1 | positive rate |
|---|---:|---:|---:|---:|
| `dataset_univ1_p90.jsonl` | 119,072 | 107,161 | 11,911 | 약 10.0% |
| `dataset_mininet_merged_p90.jsonl` | 9,737 | 8,762 | 975 | 약 10.0% |

### 4.3 CDF tie 문제

Mininet 데이터에서는 동일한 `flow_size_bytes` 값이 많이 반복된다. 예를 들어 `flow_size_bytes=20`인 sample이 매우 많다.

이 경우 `rankdata(..., method="average")` 기반 CDF는 특정 CDF 값에 sample이 몰린다. CDF regression target이 뭉치면 모델이 fine-grained ordering을 학습하기 어렵다.

검토 가능한 개선:

- `rankdata` method 비교: `average`, `min`, `max`, `dense`, `ordinal`
- `log1p(flow_size_bytes)` 기반 CDF
- quantile binning
- tie 내부 jitter
- 단, label의 의미가 훼손되지 않도록 p90 elephant 기준은 유지한다.

### 4.4 Normalization leakage

현재 평가 노트북은 test set 내부 min/max로 정규화한다. 이는 평가 시 test 분포 정보를 사용하는 형태라서 실험 해석에 좋지 않다.

필요한 개선:

- train set에서 `x_min`, `x_max`를 계산한다.
- `scaler.json`으로 저장한다.
- validation, test, fine-tuning, online inference에서 같은 scaler를 재사용한다.

특히 public pretraining 후 Mininet fine-tuning에서는 public train scaler를 유지하는 것이 좋다. Mininet에서 scaler를 새로 fit하면 pretrained weight가 학습한 입력 공간이 바뀌어 fine-tuning 효과 해석이 흐려진다.

## 5. 우선 적용할 개선 순서

### 5.1 1순위: 평가 스크립트 개선

새 파일:

```text
model/GRU/evaluate.py
```

기능:

- model checkpoint load
- JSONL test load
- 저장된 scaler 적용
- tolerance sweep
- threshold sweep
- label 기준 metric
- CDF threshold 기준 metric
- confusion matrix
- PR-AUC, ROC-AUC
- 평균 inference time
- 평균 early exit step

출력 예시:

```text
dataset: dataset_mininet_merged_p90.jsonl
model: runs/gru/D_full_finetune/best.pt
tolerance: 0.01
threshold: 0.90

accuracy: 0.9421
precision: 0.7312
recall: 0.6845
f1: 0.7071
pr_auc: 0.7420
roc_auc: 0.9618
confusion_matrix:
  tn fp
  fn tp
avg_inference_ms: 0.62
avg_exit_step: 3.8
```

### 5.2 2순위: Threshold tuning

고정 threshold 0.5를 제거하고 다음 후보를 비교한다.

```text
0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95
```

선택 기준:

- `best_f1_threshold`: F1-score 최대
- `recall_first_threshold`: recall이 목표 이상인 threshold 중 precision 최대
- `precision_first_threshold`: precision이 목표 이상인 threshold 중 recall 최대

불균형 데이터에서는 threshold가 성능을 크게 바꿀 수 있으므로, 모델 구조 변경 전에 반드시 수행한다.

### 5.3 3순위: Train scaler 저장 및 재사용

새 파일:

```text
model/GRU/data.py
```

기능:

- JSONL dataset load
- direction encoding
- CDF target 생성
- scaler fit/save/load
- train/val/test에 동일 scaler 적용

저장 파일:

```text
runs/gru/<experiment>/scaler.json
```

### 5.4 4순위: Weighted loss 또는 weighted sampler

현재 CDF regression 구조를 유지한다면 weighted regression을 먼저 적용한다.

예:

```python
loss = smooth_l1_loss(pred_cdf, true_cdf, reduction="none")
weight = torch.where(label == 1, elephant_weight, 1.0)
loss = (loss * weight).mean()
```

가능한 옵션:

- `weighted_mse`
- `weighted_huber`
- `label_weight`
- `cdf_tail_weight`
- `WeightedRandomSampler`

권장 시작점:

```text
loss: weighted_huber
elephant_weight: 3.0 ~ 9.0
sampler: off first, then compare on
```

### 5.5 5순위: CDF tie 처리 개선

Mininet CDF regression target이 tie로 뭉치는 문제를 해결하기 위해 target 생성 옵션을 실험한다.

권장 비교:

```text
cdf_method=average
cdf_method=dense
cdf_method=log_rank
cdf_method=quantile_bin
```

단, 최종 elephant label은 `label` 또는 p90 threshold 기준으로 유지한다.

### 5.6 6순위: Multi-task head

현재는 CDF regression output 하나만 있다. 최종 목표가 elephant/mice 탐지라면 binary classification head를 추가하는 것이 자연스럽다.

구조:

```text
shared backbone
  ├── cdf_head: CDF regression
  └── binary_head: elephant/mice classification
```

loss:

```text
total_loss = regression_loss + alpha * classification_loss
```

classification loss에는 `BCEWithLogitsLoss(pos_weight=...)`를 적용할 수 있다.

단, 이 변경은 기존 checkpoint 호환성에 영향을 줄 수 있으므로 평가/학습 파이프라인이 먼저 안정된 뒤 적용한다.

## 6. Fine-tuning 전략

### 6.1 Public pretraining

목적:

- 공공 데이터셋에서 일반적인 flow sequence pattern을 학습한다.
- direction, 초기 packet feature, packet-to-packet diff가 flow size와 어떤 관계를 갖는지 학습한다.

입력:

```text
dataset_univ1_p90.jsonl
```

출력:

```text
runs/gru/A_public_only/best.pt
runs/gru/A_public_only/scaler.json
runs/gru/A_public_only/config.json
```

### 6.2 Mininet full fine-tuning

목적:

- public pretrained weight 전체를 Mininet 환경에 적응시킨다.

특징:

- 전체 layer 학습
- public pretraining보다 낮은 learning rate
- epoch 수는 적게

권장 시작값:

```text
lr: 1e-4
epochs: 3 ~ 10
batch_size: 32 or 64
freeze: none
```

장점:

- Mininet domain shift가 클 때 가장 유연하다.

위험:

- Mininet 데이터가 작으므로 과적합 가능성이 있다.

### 6.3 Head-only fine-tuning

목적:

- public backbone은 유지하고 output head만 Mininet label 기준으로 조정한다.

freeze:

```text
freeze: direction_embedding
freeze: init_h_layer
freeze: gru_cell
train: classifier
```

권장 시작값:

```text
lr: 5e-4
epochs: 3 ~ 10
freeze: backbone
```

장점:

- 빠르다.
- 과적합 위험이 상대적으로 낮다.
- "일반 flow representation은 유지하고 Mininet decision boundary만 조정했다"라고 설명하기 좋다.

한계:

- public과 Mininet feature distribution 차이가 크면 backbone 적응이 안 되어 성능 한계가 있을 수 있다.

### 6.4 Gradual unfreezing

목적:

- 처음에는 head만 학습하고, 이후 GRU와 전체 layer를 단계적으로 풀어 안정적으로 적응시킨다.

권장 stage:

| Stage | Trainable layer | LR | Epoch |
|---|---|---:|---:|
| 1 | `classifier` | 5e-4 | 3 |
| 2 | `gru_cell`, `classifier` | 1e-4 | 3 |
| 3 | all layers | 3e-5 | 3 |

장점:

- head-only와 full fine-tuning의 중간 전략이다.
- 과적합을 줄이면서 backbone 일부를 Mininet에 적응시킬 수 있다.

## 7. Mixed training과 fine-tuning 비교

비교 대상:

1. Public + Mininet mixed train
2. Public pretraining 후 Mininet fine-tuning

Mixed training은 public과 Mininet을 처음부터 합쳐 학습한다. 이 방식은 구현이 단순하지만 public dataset이 훨씬 크면 모델이 public domain에 끌려갈 수 있다.

Mixed training을 공정하게 하려면 다음이 필요하다.

- domain-balanced sampler
- public:mininet sampling ratio 설정
- Mininet validation 기준 checkpoint 선택

반면 fine-tuning은 설명 구조가 더 명확하다.

> public dataset으로 일반 flow 패턴을 학습하고, Mininet dataset으로 실험 환경에 적응했다.

현재 프로젝트 목표에는 fine-tuning 방식이 더 적합하다. 다만 baseline 비교를 위해 mixed training도 함께 수행한다.

## 8. 실험 매트릭스

최소 비교 실험은 다음 6개다.

| 실험 | 학습 방식 | Test |
|---|---|---|
| A | Public-only train | Mininet test |
| B | Mininet-only train | Mininet test |
| C | Public + Mininet mixed train | Mininet test |
| D | Public pretrain -> Mininet full fine-tuning | Mininet test |
| E | Public pretrain -> Mininet head-only fine-tuning | Mininet test |
| F | Public pretrain -> Gradual unfreezing fine-tuning | Mininet test |

각 실험에서 저장해야 할 항목:

- checkpoint 경로
- 사용한 train/val/test dataset
- fine-tuning 여부
- 초기 checkpoint 경로
- freeze layer 정보
- learning rate
- epoch 수
- batch size
- scaler 경로
- threshold sweep 결과
- 최종 evaluation 결과
- 평균 inference time
- 평균 early exit step

## 9. 권장 파일 구조

추가할 파일:

```text
model/GRU/data.py
model/GRU/train.py
model/GRU/evaluate.py
model/GRU/split_dataset.py
```

선택적으로 수정할 파일:

```text
model/GRU/models.py
```

수정이 필요한 경우:

- freeze helper 추가
- `forward`가 학습 시 step별 output을 더 명확히 반환하도록 개선
- multi-task head 추가
- threshold 근처 uncertain sample은 early exit을 늦추는 옵션 추가

권장 run 출력 구조:

```text
runs/gru/
  A_public_only/
    best.pt
    scaler.json
    config.json
    freeze.json
    metrics_mininet_test.json
    threshold_sweep_mininet_test.csv
  D_full_finetune/
    best.pt
    config.json
    freeze.json
    metrics_mininet_test.json
    threshold_sweep_mininet_test.csv
```

## 10. 실행 명령 예시

### 10.1 Dataset split

```bash
python model/GRU/split_dataset.py \
  --input dataset_univ1_p90.jsonl \
  --out-dir splits/univ1 \
  --train-ratio 0.8 \
  --val-ratio 0.1 \
  --group-key trace_key

python model/GRU/split_dataset.py \
  --input dataset_mininet_merged_p90.jsonl \
  --out-dir splits/mininet \
  --train-ratio 0.7 \
  --val-ratio 0.15 \
  --group-key run_id
```

### 10.2 A. Public-only train -> Mininet test

```bash
python model/GRU/train.py \
  --train-file splits/univ1/train.jsonl \
  --val-file splits/univ1/val.jsonl \
  --target cdf \
  --loss weighted-huber \
  --elephant-weight 3.0 \
  --epochs 30 \
  --lr 1e-3 \
  --batch-size 64 \
  --checkpoint-out runs/gru/A_public_only/best.pt \
  --scaler-out runs/gru/A_public_only/scaler.json

python model/GRU/evaluate.py \
  --model-path runs/gru/A_public_only/best.pt \
  --test-file splits/mininet/test.jsonl \
  --scaler-path runs/gru/A_public_only/scaler.json \
  --label-mode label \
  --thresholds 0.5 0.6 0.7 0.8 0.85 0.9 0.95 \
  --tolerances 0.01 0.03 0.05 0.1
```

### 10.3 B. Mininet-only train -> Mininet test

```bash
python model/GRU/train.py \
  --train-file splits/mininet/train.jsonl \
  --val-file splits/mininet/val.jsonl \
  --target label \
  --loss bce \
  --elephant-weight 9.0 \
  --epochs 20 \
  --lr 1e-3 \
  --batch-size 32 \
  --checkpoint-out runs/gru/B_mininet_only/best.pt \
  --scaler-out runs/gru/B_mininet_only/scaler.json
```

### 10.4 C. Public + Mininet mixed train -> Mininet test

```bash
python model/GRU/train.py \
  --train-file splits/univ1/train.jsonl splits/mininet/train.jsonl \
  --val-file splits/mininet/val.jsonl \
  --target label \
  --loss bce \
  --domain-sampling balanced \
  --epochs 30 \
  --lr 1e-3 \
  --batch-size 64 \
  --checkpoint-out runs/gru/C_mixed/best.pt \
  --scaler-out runs/gru/C_mixed/scaler.json
```

### 10.5 D. Public pretrain -> Mininet full fine-tuning

```bash
python model/GRU/train.py \
  --train-file splits/mininet/train.jsonl \
  --val-file splits/mininet/val.jsonl \
  --init-checkpoint runs/gru/A_public_only/best.pt \
  --scaler-in runs/gru/A_public_only/scaler.json \
  --freeze none \
  --target label \
  --loss bce \
  --elephant-weight 9.0 \
  --epochs 5 \
  --lr 1e-4 \
  --batch-size 32 \
  --checkpoint-out runs/gru/D_full_finetune/best.pt
```

### 10.6 E. Public pretrain -> Head-only fine-tuning

```bash
python model/GRU/train.py \
  --train-file splits/mininet/train.jsonl \
  --val-file splits/mininet/val.jsonl \
  --init-checkpoint runs/gru/A_public_only/best.pt \
  --scaler-in runs/gru/A_public_only/scaler.json \
  --freeze backbone \
  --target label \
  --loss bce \
  --elephant-weight 9.0 \
  --epochs 5 \
  --lr 5e-4 \
  --batch-size 32 \
  --checkpoint-out runs/gru/E_head_only/best.pt
```

### 10.7 F. Public pretrain -> Gradual unfreezing

```bash
python model/GRU/train.py \
  --train-file splits/mininet/train.jsonl \
  --val-file splits/mininet/val.jsonl \
  --init-checkpoint runs/gru/A_public_only/best.pt \
  --scaler-in runs/gru/A_public_only/scaler.json \
  --schedule gradual-unfreeze \
  --head-epochs 3 \
  --gru-epochs 3 \
  --full-epochs 3 \
  --head-lr 5e-4 \
  --gru-lr 1e-4 \
  --full-lr 3e-5 \
  --target label \
  --loss bce \
  --elephant-weight 9.0 \
  --batch-size 32 \
  --checkpoint-out runs/gru/F_gradual/best.pt
```

## 11. Early-exit 개선 방향

현재 early exit은 예측값 변화량만 본다.

```text
abs(current_pred - previous_pred) < tolerance
```

개선 방향:

- tolerance sweep: `0.01`, `0.03`, `0.05`, `0.1`
- threshold 근처 sample은 exit을 늦춘다.
- confidence margin이 충분히 큰 sample만 조기 종료한다.

예:

```text
exit if:
  abs(current_pred - previous_pred) < tolerance
  and abs(current_pred - decision_threshold) > margin
```

이렇게 하면 decision threshold 근처의 애매한 flow는 더 많은 packet을 보고 판단한다.

평가 시 반드시 다음 trade-off를 같이 본다.

| 항목 | 의미 |
|---|---|
| recall | elephant 미탐 방지 |
| precision | mice 오탐 방지 |
| F1 | precision/recall 균형 |
| avg exit step | 조기 탐지 속도 |
| avg inference time | 온라인 적용 비용 |

## 12. 최종 권장 로드맵

1. `evaluate.py`를 먼저 만든다.
2. 기존 checkpoint를 `dataset_mininet_merged_p90.jsonl`에서 label 기준으로 평가한다.
3. threshold sweep과 tolerance sweep으로 현재 한계를 수치화한다.
4. `data.py`, `train.py`, `split_dataset.py`를 추가해 재현 가능한 학습 구조를 만든다.
5. train scaler 저장/재사용을 적용한다.
6. public-only, Mininet-only, mixed baseline을 만든다.
7. public pretraining 후 full/head-only/gradual fine-tuning을 비교한다.
8. 가장 좋은 strategy에서 weighted loss, sampler, CDF tie 처리, early-exit margin을 순차적으로 추가한다.
9. 마지막에 필요하면 multi-task head를 추가한다.

우선순위는 다음과 같다.

```text
evaluation 개선
-> threshold tuning
-> scaler 저장/재사용
-> fine-tuning 실험 구조
-> weighted loss/sampler
-> CDF tie 처리
-> early-exit margin
-> multi-task head
```

이 순서가 좋은 이유는, 먼저 평가 기준을 바로잡아야 모델 변경이 실제로 elephant detection 성능을 개선했는지 판단할 수 있기 때문이다.
