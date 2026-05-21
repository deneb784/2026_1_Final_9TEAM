import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path


def load_jsonl(path: str | Path) -> list[dict]:
    samples = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                samples.append(json.loads(line))
    return samples


def save_jsonl(samples: list[dict], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")


def group_value(sample: dict, group_key: str) -> str:
    flow_key = sample.get("flow_key", {})
    trace_key = sample.get("trace_key", {})

    if group_key == "run_id":
        return str(sample.get("run_id"))
    if group_key == "trace_key":
        return f"{trace_key.get('source_file')}:{trace_key.get('tcp_stream')}"
    if group_key == "parent_flow":
        return f"{sample.get('run_id')}:{flow_key.get('src_index')}:{flow_key.get('flow_id')}"
    if group_key == "source_file":
        return str(trace_key.get("source_file"))
    if group_key == "sample":
        return f"{sample.get('run_id')}:{flow_key.get('src_index')}:{flow_key.get('flow_id')}:{flow_key.get('direction')}"

    raise ValueError(f"unsupported group key: {group_key}")


def summarize(name: str, samples: list[dict]) -> None:
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
    unknown = (set(train_groups) | set(val_groups) | set(test_groups)) - set(grouped)
    if unknown:
        raise ValueError(f"unknown split groups: {sorted(unknown)}")

    overlap = (set(train_groups) & set(val_groups)) | (set(train_groups) & set(test_groups)) | (set(val_groups) & set(test_groups))
    if overlap:
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
    groups = list(grouped)
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
        grouped[group_value(sample, args.group_key)].append(sample)

    named_split = args.train_groups is not None or args.val_groups is not None or args.test_groups is not None
    if named_split:
        train_groups = args.train_groups or []
        val_groups = args.val_groups or []
        test_groups = args.test_groups or []
        train, val, test = split_by_named_groups(grouped, train_groups, val_groups, test_groups)
    else:
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
