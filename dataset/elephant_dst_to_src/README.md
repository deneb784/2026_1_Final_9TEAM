# 서버 응답 방향 Elephant Flow 데이터셋

이 폴더는 elephant/mice 분류 실험을 위해 `dst_to_src` 방향 flow만 모아 정리한 데이터셋이다.

현재 TrafficGenerator는 client가 server로 작은 요청을 보내고(`src_to_dst`), server가 요청된 크기의 실제 payload를 client로 응답하는(`dst_to_src`) 구조다. 따라서 elephant flow의 핵심 payload는 `dst_to_src` 방향에서 관측되므로, 이 폴더의 데이터셋은 `src_to_dst` sample을 제외하고 `dst_to_src` sample만 사용한다.

## 폴더 구조

```text
dataset/elephant_dst_to_src/
  vl2/
    seq3/
      dataset.jsonl
      splits_parent_flow/{train,val,test}.jsonl
    seq5/
    seq10/
  dctcp/
    seq3/
    seq5/
    seq10/
  fb/
    seq3/
    seq5/
    seq10/
```

각 `dataset.jsonl`은 flow의 앞 N개 packet만 남긴 버전이다.

```text
seq3  = 앞 3개 packet 사용
seq5  = 앞 5개 packet 사용
seq10 = 앞 10개 packet 사용
```

label은 `dst_to_src` 방향의 `directional_size_bytes` 기준으로 relabel된 elephant/mice 값을 유지한다.

각 `splits_parent_flow` 폴더는 아래 명령으로 만든 train/validation/test split이다.

```bash
python3 -m model.GRU.split_dataset \
  --input dataset.jsonl \
  --out-dir splits_parent_flow \
  --group-key parent_flow
```

기본 split 비율은 70/15/15이고 seed는 42다. `parent_flow` 기준으로 나누기 때문에 같은 parent request에서 나온 sample이 서로 다른 split으로 섞이지 않는다.

## 원본 파일

```text
dataset/relabel/dataset_vl2_dst_to_src_relabel.jsonl
dataset/relabel/dataset_dctcp_dst_to_src_relabel.jsonl
dataset/relabel/dataset_fb_dst_to_src_relabel.jsonl
```

## 사용 예시

예를 들어 VL2의 앞 5개 packet 기준 데이터셋을 학습하려면 아래 파일들을 사용한다.

```text
dataset/elephant_dst_to_src/vl2/seq5/splits_parent_flow/train.jsonl
dataset/elephant_dst_to_src/vl2/seq5/splits_parent_flow/val.jsonl
dataset/elephant_dst_to_src/vl2/seq5/splits_parent_flow/test.jsonl
```
