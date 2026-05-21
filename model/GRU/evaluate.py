import argparse
import csv
import json
import time
from pathlib import Path

import torch

try:
    from .data import FeatureScaler, FlowJsonlDataset, compute_cdf_targets, fit_feature_scaler
    from .models import DiffEarlyExitGRU
except ImportError:
    from data import FeatureScaler, FlowJsonlDataset, compute_cdf_targets, fit_feature_scaler
    from models import DiffEarlyExitGRU


def confusion_counts(y_true: list[int], y_pred: list[int]) -> dict[str, int]:
    tp = sum(1 for true, pred in zip(y_true, y_pred) if true == 1 and pred == 1)
    tn = sum(1 for true, pred in zip(y_true, y_pred) if true == 0 and pred == 0)
    fp = sum(1 for true, pred in zip(y_true, y_pred) if true == 0 and pred == 1)
    fn = sum(1 for true, pred in zip(y_true, y_pred) if true == 1 and pred == 0)
    return {"tn": tn, "fp": fp, "fn": fn, "tp": tp}


def classification_metrics(y_true: list[int], scores: list[float], threshold: float) -> dict[str, float | int]:
    y_pred = [1 if score >= threshold else 0 for score in scores]
    cm = confusion_counts(y_true, y_pred)
    total = len(y_true)
    accuracy = (cm["tp"] + cm["tn"]) / total if total else 0.0
    precision = cm["tp"] / (cm["tp"] + cm["fp"]) if (cm["tp"] + cm["fp"]) else 0.0
    recall = cm["tp"] / (cm["tp"] + cm["fn"]) if (cm["tp"] + cm["fn"]) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "threshold": threshold,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        **cm,
    }


def average_precision(y_true: list[int], scores: list[float]) -> float | None:
    positives = sum(y_true)
    if positives == 0:
        return None

    ranked = sorted(zip(scores, y_true), key=lambda item: item[0], reverse=True)
    hit_count = 0
    precision_sum = 0.0
    for rank, (_, label) in enumerate(ranked, start=1):
        if label == 1:
            hit_count += 1
            precision_sum += hit_count / rank
    return precision_sum / positives


def roc_auc(y_true: list[int], scores: list[float]) -> float | None:
    positives = sum(y_true)
    negatives = len(y_true) - positives
    if positives == 0 or negatives == 0:
        return None

    indexed_scores = sorted((score, index) for index, score in enumerate(scores))
    ranks = [0.0] * len(scores)
    start = 0
    while start < len(indexed_scores):
        end = start
        while end + 1 < len(indexed_scores) and indexed_scores[end + 1][0] == indexed_scores[start][0]:
            end += 1
        avg_rank = (start + end + 2) / 2  # 1-based average rank
        for position in range(start, end + 1):
            _, original_index = indexed_scores[position]
            ranks[original_index] = avg_rank
        start = end + 1

    positive_rank_sum = sum(rank for rank, label in zip(ranks, y_true) if label == 1)
    return (positive_rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def build_truth(samples: list[dict], label_mode: str, cdf_method: str, size_field: str, threshold: float) -> list[int]:
    if label_mode == "label":
        return [int(sample["label"]) for sample in samples]

    cdfs = compute_cdf_targets(samples, size_field=size_field, method=cdf_method)
    return [1 if cdf >= threshold else 0 for cdf in cdfs]


def build_cdfs(samples: list[dict], cdf_method: str, size_field: str) -> list[float]:
    return compute_cdf_targets(samples, size_field=size_field, method=cdf_method)


def jittered_binary_x(values: list[int], jitter: float = 0.08) -> list[float]:
    if not values:
        return []

    window = max(1, int(round(2 / jitter)))
    centered = (window - 1) / 2
    return [value + ((index % window) - centered) * (jitter / window) for index, value in enumerate(values)]


def load_model(model_path: str | Path, device: torch.device, input_size: int, hidden_size: int) -> DiffEarlyExitGRU:
    model = DiffEarlyExitGRU(input_size=input_size, hidden_size=hidden_size).to(device)
    state = torch.load(model_path, map_location=device)
    if isinstance(state, dict) and "model_state_dict" in state:
        state = state["model_state_dict"]
    model.load_state_dict(state)
    model.eval()
    return model


def run_inference(
    model: DiffEarlyExitGRU,
    dataset: FlowJsonlDataset,
    device: torch.device,
    tolerance: float,
) -> tuple[list[float], list[int], list[float]]:
    scores: list[float] = []
    exit_steps: list[int] = []
    inference_times: list[float] = []

    with torch.no_grad():
        for item in dataset:
            x = item["x"].unsqueeze(0).to(device)
            direction = item["direction"].unsqueeze(0).to(device)
            seq_len = item["seq_len"].unsqueeze(0).to(device)

            if device.type == "cuda":
                torch.cuda.synchronize()
            start_time = time.perf_counter()
            score, step = model(
                x,
                direction,
                seq_len=seq_len,
                enable_early_exit=True,
                tolerance=tolerance,
            )
            if device.type == "cuda":
                torch.cuda.synchronize()
            end_time = time.perf_counter()

            scores.append(float(score))
            exit_steps.append(int(step))
            inference_times.append((end_time - start_time) * 1000)

    return scores, exit_steps, inference_times


def write_threshold_sweep(rows: list[dict], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "tolerance",
                "threshold",
                "accuracy",
                "precision",
                "recall",
                "f1",
                "tn",
                "fp",
                "fn",
                "tp",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def save_scatter(
    x_values: list[float],
    scores: list[float],
    labels: list[int],
    threshold: float,
    path: str | Path,
    x_label: str,
    title_suffix: str = "",
) -> None:
    import matplotlib.pyplot as plt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 7))
    colors = ["#4C72B0" if label == 0 else "#C44E52" for label in labels]
    ax.scatter(x_values, scores, c=colors, s=18, alpha=0.75, edgecolors="none")
    ax.plot([0, 1], [0, 1], linestyle=":", color="gray", linewidth=1.2)
    ax.axvline(threshold, linestyle="--", color="gray", linewidth=1.2)
    ax.axhline(threshold, linestyle="--", color="gray", linewidth=1.2)
    ax.set_xlim(0, 1.02)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel(x_label)
    ax.set_ylabel("Predicted score")
    title = f"{x_label} vs Predicted score"
    if title_suffix:
        title = f"{title} ({title_suffix})"
    ax.set_title(title)
    legend_handles = [
        plt.Line2D([0], [0], marker="o", color="w", label="Label 0", markerfacecolor="#4C72B0", markersize=7),
        plt.Line2D([0], [0], marker="o", color="w", label="Label 1", markerfacecolor="#C44E52", markersize=7),
    ]
    ax.legend(handles=legend_handles, title="True Labels", loc="upper left")
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--test-file", required=True)
    parser.add_argument("--scaler-path")
    parser.add_argument("--fit-scaler-on-test", action="store_true")
    parser.add_argument("--label-mode", choices=["label", "cdf"], default="label")
    parser.add_argument("--cdf-method", choices=["average", "min", "max", "ordinal"], default="average")
    parser.add_argument("--size-field", default="flow_size_bytes")
    parser.add_argument("--thresholds", nargs="+", type=float, default=[0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95])
    parser.add_argument("--tolerances", nargs="+", type=float, default=[0.01])
    parser.add_argument("--input-size", type=int, default=11)
    parser.add_argument("--hidden-size", type=int, default=64)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--metrics-out")
    parser.add_argument("--sweep-out")
    parser.add_argument("--plot-out")
    args = parser.parse_args()

    device = torch.device(args.device)
    model = load_model(args.model_path, device, input_size=args.input_size, hidden_size=args.hidden_size)

    raw_samples = FlowJsonlDataset(args.test_file).samples
    scaler = None
    if args.scaler_path:
        scaler = FeatureScaler.load(args.scaler_path)
    elif args.fit_scaler_on_test:
        scaler = fit_feature_scaler(raw_samples, input_size=args.input_size)

    dataset = FlowJsonlDataset(
        args.test_file,
        scaler=scaler,
        target="label",
        cdf_method=args.cdf_method,
        size_field=args.size_field,
    )
    samples = dataset.samples
    true_cdfs = build_cdfs(samples, cdf_method=args.cdf_method, size_field=args.size_field)
    true_labels = [int(sample["label"]) for sample in samples]

    all_rows: list[dict] = []
    tolerance_results: list[dict] = []

    for tolerance in args.tolerances:
        scores, exit_steps, inference_times = run_inference(model, dataset, device, tolerance=tolerance)

        y_true_for_auc = build_truth(
            samples,
            label_mode=args.label_mode,
            cdf_method=args.cdf_method,
            size_field=args.size_field,
            threshold=0.5,
        )
        pr_auc = average_precision(y_true_for_auc, scores)
        roc = roc_auc(y_true_for_auc, scores)

        rows = []
        for threshold in args.thresholds:
            y_true = build_truth(
                samples,
                label_mode=args.label_mode,
                cdf_method=args.cdf_method,
                size_field=args.size_field,
                threshold=threshold,
            )
            metrics = classification_metrics(y_true, scores, threshold)
            row = {"tolerance": tolerance, **metrics}
            rows.append(row)
            all_rows.append(row)

        best_f1 = max(rows, key=lambda row: row["f1"])
        avg_time = sum(inference_times) / len(inference_times) if inference_times else 0.0
        avg_step = sum(exit_steps) / len(exit_steps) if exit_steps else 0.0

        if args.plot_out:
            plot_path = Path(args.plot_out)
            if plot_path.suffix:
                if len(args.tolerances) == 1:
                    target_path = plot_path
                else:
                    target_path = plot_path.with_name(f"{plot_path.stem}_tol{tolerance:g}{plot_path.suffix}")
            else:
                target_path = plot_path / f"true_vs_predicted_cdf_tol{tolerance:g}.png"

            if args.label_mode == "label":
                x_values = jittered_binary_x(true_labels)
                x_label = "True Label"
            else:
                x_values = true_cdfs
                x_label = "True CDF"

            save_scatter(
                x_values,
                scores,
                true_labels,
                threshold=0.5,
                path=target_path,
                x_label=x_label,
                title_suffix=f"tolerance={tolerance:g}",
            )

        tolerance_result = {
            "tolerance": tolerance,
            "samples": len(samples),
            "label_mode": args.label_mode,
            "pr_auc": pr_auc,
            "roc_auc": roc,
            "avg_inference_ms": avg_time,
            "min_inference_ms": min(inference_times) if inference_times else 0.0,
            "max_inference_ms": max(inference_times) if inference_times else 0.0,
            "avg_exit_step": avg_step,
            "best_f1": best_f1,
        }
        tolerance_results.append(tolerance_result)

        print("-" * 72)
        print(f"tolerance={tolerance:g} samples={len(samples)} label_mode={args.label_mode}")
        print(f"PR-AUC/AP: {pr_auc if pr_auc is not None else 'n/a'}")
        print(f"ROC-AUC:   {roc if roc is not None else 'n/a'}")
        print(f"Avg inference: {avg_time:.4f} ms")
        print(f"Avg exit step: {avg_step:.2f}")
        print("threshold sweep:")
        for row in rows:
            print(
                f"  th={row['threshold']:.3g} acc={row['accuracy']:.4f} "
                f"prec={row['precision']:.4f} rec={row['recall']:.4f} "
                f"f1={row['f1']:.4f} cm=[[{row['tn']},{row['fp']}],[{row['fn']},{row['tp']}]]"
            )
        print(f"best_f1_threshold={best_f1['threshold']:.3g} best_f1={best_f1['f1']:.4f}")

    if args.sweep_out:
        write_threshold_sweep(all_rows, args.sweep_out)

    if args.metrics_out:
        path = Path(args.metrics_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(tolerance_results, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
