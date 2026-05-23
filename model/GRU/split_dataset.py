import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path


def load_jsonl(path: str | Path) -> list[dict]:
    """입력 JSONL dataset을 sample dict 목록으로 읽는다."""
    samples = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                samples.append(json.loads(line))
    return samples


def save_jsonl(samples: list[dict], path: str | Path) -> None:
    """sample 목록을 JSONL 파일로 저장한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")


def group_value(sample: dict, group_key: str) -> str:
    """split leakage를 막기 위해 sample을 어떤 단위로 묶을지 결정한다."""
    flow_key = sample.get("flow_key", {})
    trace_key = sample.get("trace_key", {})

    if group_key == "run_id":
        # 같은 실험 run 전체를 train/val/test 중 하나에만 배치한다.
        return str(sample.get("run_id"))
    if group_key == "trace_key":
        # UNI1처럼 pcap source_file + tcp_stream으로 원본 TCP stream을 묶는다.
        return f"{trace_key.get('source_file')}:{trace_key.get('tcp_stream')}"
    if group_key == "parent_flow":
        # 같은 요청의 src_to_dst/dst_to_src 방향 sample이 서로 다른 split에 섞이지 않게 한다.
        return f"{sample.get('run_id')}:{flow_key.get('src_index')}:{flow_key.get('flow_id')}"
    if group_key == "source_file":
        # 같은 pcap 파일에서 나온 sample을 한 split에 몰아넣는다.
        return str(trace_key.get("source_file"))
    if group_key == "sample":
        # 완전히 sample 단위로 나누고 싶을 때 사용한다. leakage 방지는 가장 약하다.
        return f"{sample.get('run_id')}:{flow_key.get('src_index')}:{flow_key.get('flow_id')}:{flow_key.get('direction')}"

    raise ValueError(f"unsupported group key: {group_key}")


def summarize(name: str, samples: list[dict]) -> None:
    """split별 label/direction/run 분포와 짧은 시퀀스 비율을 출력한다."""
    labels = Counter(sample.get("label") for sample in samples)
    directions = Counter(sample.get("flow_key", {}).get("direction") for sample in samples)
    runs = Counter(sample.get("run_id") for sample in samples)
    seq_lens = Counter(sample.get("seq_len", len(sample.get("x", []))) for sample in samples)
    short = sum(count for seq_len, count in seq_lens.items() if seq_len < 10)
    total = len(samples)
    pos_rate = labels[1] / total if total else 0.0
    short_rate = short / total if total else 0.0
    print(
        f"{name}: samples={total} labels={dict(labels)} pos_rate={pos_rate:.4f} "
        f"directions={dict(directions)} runs={dict(runs)} short_seq_rate={short_rate:.4f}"
    )


def split_by_named_groups(
    grouped: dict[str, list[dict]],
    train_groups: list[str],
    val_groups: list[str],
    test_groups: list[str],
) -> tuple[list[dict], list[dict], list[dict]]:
    """사용자가 명시한 group 목록대로 train/val/test를 만든다."""
    unknown = (set(train_groups) | set(val_groups) | set(test_groups)) - set(grouped)
    if unknown:
        raise ValueError(f"unknown split groups: {sorted(unknown)}")

    overlap = (set(train_groups) & set(val_groups)) | (set(train_groups) & set(test_groups)) | (set(val_groups) & set(test_groups))
    if overlap:
        # 같은 group이 두 split에 들어가면 평가 누수가 생기므로 막는다.
        raise ValueError(f"split groups overlap: {sorted(overlap)}")

    train = [sample for group in train_groups for sample in grouped[group]]
    val = [sample for group in val_groups for sample in grouped[group]]
    test = [sample for group in test_groups for sample in grouped[group]]
    return train, val, test


def split_by_ratio(
    grouped: dict[str, list[dict]],
    train_ratio: float,
    val_ratio: float,
    seed: int,
) -> tuple[list[dict], list[dict], list[dict], list[str], list[str], list[str]]:
    """group 목록을 seed로 섞은 뒤 비율대로 train/val/test에 배정한다."""
    groups = list(grouped)
    # sample이 아니라 group을 섞어 같은 group 내부 sample이 split을 넘나들지 않게 한다.
    random.Random(seed).shuffle(groups)

    train_end = int(len(groups) * train_ratio)
    val_end = train_end + int(len(groups) * val_ratio)

    train_groups = groups[:train_end]
    val_groups = groups[train_end:val_end]
    test_groups = groups[val_end:]

    train = [sample for group in train_groups for sample in grouped[group]]
    val = [sample for group in val_groups for sample in grouped[group]]
    test = [sample for group in test_groups for sample in grouped[group]]
    return train, val, test, train_groups, val_groups, test_groups


def main() -> None:
    """JSONL dataset을 train/val/test JSONL과 split metadata로 나눈다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument(
        "--group-key",
        choices=["run_id", "trace_key", "parent_flow", "source_file", "sample"],
        default="run_id",
    )
    parser.add_argument("--train-groups", nargs="*", default=None)
    parser.add_argument("--val-groups", nargs="*", default=None)
    parser.add_argument("--test-groups", nargs="*", default=None)
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    samples = load_jsonl(args.input)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        # 먼저 group_key 기준으로 sample을 묶고, split은 group 단위로 수행한다.
        grouped[group_value(sample, args.group_key)].append(sample)

    named_split = args.train_groups is not None or args.val_groups is not None or args.test_groups is not None
    if named_split:
        # 그룹명을 직접 넘긴 경우 재현 가능한 수동 split을 만든다.
        train_groups = args.train_groups or []
        val_groups = args.val_groups or []
        test_groups = args.test_groups or []
        train, val, test = split_by_named_groups(grouped, train_groups, val_groups, test_groups)
    else:
        # 그룹명을 넘기지 않으면 ratio와 seed 기준으로 자동 split한다.
        train, val, test, train_groups, val_groups, test_groups = split_by_ratio(
            grouped,
            train_ratio=args.train_ratio,
            val_ratio=args.val_ratio,
            seed=args.seed,
        )

    out_dir = Path(args.out_dir)
    save_jsonl(train, out_dir / "train.jsonl")
    save_jsonl(val, out_dir / "val.jsonl")
    save_jsonl(test, out_dir / "test.jsonl")

    metadata = {
        # 나중에 같은 split을 재현하거나 실험 로그를 확인하기 위한 metadata다.
        "input": args.input,
        "group_key": args.group_key,
        "train_groups": train_groups,
        "val_groups": val_groups,
        "test_groups": test_groups,
        "train_count": len(train),
        "val_count": len(val),
        "test_count": len(test),
    }
    (out_dir / "split.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print(f"groups: {len(grouped)}")
    summarize("train", train)
    summarize("val", val)
    summarize("test", test)
    print(f"wrote {out_dir}")


if __name__ == "__main__":
    main()
