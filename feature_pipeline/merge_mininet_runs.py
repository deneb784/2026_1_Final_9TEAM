import argparse
import json
from pathlib import Path

from feature_pipeline.dataset_builder import (
    percentile,
    run_dataset_builder,
)


def relabel_samples(samples: list[dict]) -> tuple[list[dict], int]:
    directional_sizes = [sample["directional_size_bytes"] for sample in samples]
    threshold = percentile(sorted(directional_sizes), 0.80)
    for sample in samples:
        sample["flow_size_bytes"] = sample["directional_size_bytes"]
        sample["label"] = 1 if sample["directional_size_bytes"] >= threshold else 0

    return samples, threshold


def build_run_samples(run_dir: Path, packet_count: int) -> list[dict]:
    results_dir = run_dir / "results"
    pcap_dir = run_dir / "captured_packet"
    samples = run_dataset_builder(
        results_dir=str(results_dir),
        pcap_dir=str(pcap_dir),
        packet_count=packet_count,
        label_threshold=None,
        direction_filter=None,
    )
    for sample in samples:
        sample["run_id"] = run_dir.name
    return samples


def save_jsonl(samples: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dirs", nargs="+")
    parser.add_argument("--packet-count", type=int, default=10)
    parser.add_argument("--output", default="dataset_mininet_merged.jsonl")
    args = parser.parse_args()

    all_samples: list[dict] = []
    for run_dir_str in args.run_dirs:
        run_dir = Path(run_dir_str)
        samples = build_run_samples(run_dir, args.packet_count)
        print(f"{run_dir}: {len(samples)} samples")
        all_samples.extend(samples)

    all_samples, threshold = relabel_samples(all_samples)
    save_jsonl(all_samples, Path(args.output))
    print(f"merged samples: {len(all_samples)}")
    print(f"merged directional p80 threshold: {threshold} bytes")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
