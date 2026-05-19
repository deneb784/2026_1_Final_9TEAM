import argparse
import json
from pathlib import Path

from feature_pipeline.dataset_builder import percentile


def load_jsonl(path: Path) -> list[dict]:
    samples = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                samples.append(json.loads(line))
    return samples


def save_jsonl(samples: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")


def relabel(
    samples: list[dict],
    quantile: float,
    label_size_field: str,
    threshold_bytes: int | None = None,
) -> tuple[list[dict], int]:
    sizes = [sample[label_size_field] for sample in samples]
    threshold = threshold_bytes
    if threshold is None:
        threshold = percentile(sorted(sizes), quantile)

    for sample in samples:
        sample["flow_size_bytes"] = sample[label_size_field]
        sample["label"] = 1 if sample[label_size_field] >= threshold else 0

    return samples, threshold


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("--output", required=True)
    parser.add_argument("--quantile", type=float, default=0.90)
    parser.add_argument("--threshold-bytes", type=int)
    parser.add_argument(
        "--label-size-field",
        choices=["directional_size_bytes", "flow_size_bytes", "parent_flow_size_bytes"],
        default="directional_size_bytes",
    )
    args = parser.parse_args()

    samples = load_jsonl(Path(args.input))
    samples, threshold = relabel(
        samples,
        args.quantile,
        args.label_size_field,
        args.threshold_bytes,
    )
    save_jsonl(samples, Path(args.output))
    print(f"samples: {len(samples)}")
    print(f"label_size_field: {args.label_size_field}")
    if args.threshold_bytes is None:
        print(f"quantile: {args.quantile:g}")
    else:
        print("quantile: ignored")
    print(f"threshold: {threshold} bytes")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
