import argparse
import json
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.dataset.dataset_builder import (
    percentile,
    run_dataset_builder,
)


def relabel_samples(samples: list[dict]) -> tuple[list[dict], int]:
    """여러 run을 합친 뒤 전체 분포 기준으로 라벨을 다시 매긴다."""
    sizes = [sample["flow_size_bytes"] for sample in samples]
    # run마다 따로 p80/p90을 쓰면 run 간 라벨 기준이 달라지므로 병합 후 p90을 사용한다.
    threshold = percentile(sorted(sizes), 0.90)
    for sample in samples:
        sample["label"] = 1 if sample["flow_size_bytes"] >= threshold else 0

    return samples, threshold


def build_run_samples(
    run_dir: Path,
    packet_count: int,
    raw_sequences: bool = False,
    sample_mode: str = "direction",
    direction_filter: str | None = None,
) -> list[dict]:
    """run 하나의 results/captured_packet에서 sample을 만들고 run_id를 붙인다."""
    results_dir = run_dir / "results"
    pcap_dir = run_dir / "captured_packet"
    samples = run_dataset_builder(
        results_dir=str(results_dir),
        pcap_dir=str(pcap_dir),
        packet_count=packet_count,
        label_threshold=None,
        direction_filter=direction_filter,
        raw_sequences=raw_sequences,
        sample_mode=sample_mode,
    )
    for sample in samples:
        # 병합 dataset에서도 어떤 실험 run에서 나온 sample인지 추적할 수 있게 한다.
        sample["run_id"] = run_dir.name
    return samples


def save_jsonl(samples: list[dict], output_path: Path) -> None:
    """병합된 sample을 JSONL 파일로 저장한다."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")


def main() -> None:
    """여러 Mininet run 디렉터리를 하나의 학습 dataset으로 병합한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dirs", nargs="+")
    parser.add_argument("--packet-count", type=int, default=10)
    parser.add_argument("--sample-mode", choices=["direction", "request"], default="direction")
    parser.add_argument("--direction-filter", choices=["src_to_dst", "dst_to_src"], default=None)
    parser.add_argument("--output", default="dataset_mininet_merged.jsonl")
    parser.add_argument(
        "--raw-sequences",
        action="store_true",
        help="write only observed packets in x and omit seq_len; padding can be applied later",
    )
    args = parser.parse_args()

    all_samples: list[dict] = []
    for run_dir_str in args.run_dirs:
        # 각 run은 독립적으로 packet/meta 매칭을 수행한 뒤 sample 목록만 합친다.
        run_dir = Path(run_dir_str)
        samples = build_run_samples(
            run_dir,
            args.packet_count,
            raw_sequences=args.raw_sequences,
            sample_mode=args.sample_mode,
            direction_filter=args.direction_filter,
        )
        print(f"{run_dir}: {len(samples)} samples")
        all_samples.extend(samples)

    all_samples, threshold = relabel_samples(all_samples)
    save_jsonl(all_samples, Path(args.output))
    print(f"merged samples: {len(all_samples)}")
    print(f"merged p90 threshold: {threshold} bytes")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
