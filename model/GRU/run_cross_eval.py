import argparse
import json
import subprocess
from pathlib import Path


def run_one(command: list[str], log_path: str | None = None) -> None:
    if log_path is None:
        subprocess.run(command, check=True)
        return

    # run and redirect both stdout and stderr to the log file (text mode)
    with open(log_path, "w", encoding="utf-8") as logf:
        try:
            subprocess.run(command, check=True, stdout=logf, stderr=subprocess.STDOUT, text=True)
        except subprocess.CalledProcessError as e:
            logf.write(f"\nProcess exited with return code: {e.returncode}\n")
            raise


def build_evaluate_command(
    python_bin: str,
    model_path: str,
    test_file: str,
    scaler_path: str,
    thresholds: list[float],
    tolerances: list[float],
    sweep_out: Path,
    metrics_out: Path,
    plot_out: Path,
    input_size: int,
    hidden_size: int,
    device: str,
) -> list[str]:
    command = [
        python_bin,
        "model/GRU/evaluate.py",
        "--model-path",
        model_path,
        "--test-file",
        test_file,
        "--scaler-path",
        scaler_path,
        "--input-size",
        str(input_size),
        "--hidden-size",
        str(hidden_size),
        "--device",
        device,
        "--thresholds",
        *[str(value) for value in thresholds],
        "--tolerances",
        *[str(value) for value in tolerances],
        "--sweep-out",
        str(sweep_out),
        "--metrics-out",
        str(metrics_out),
        "--plot-out",
        str(plot_out),
    ]
    return command


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default="runs/gru/cross_eval")
    parser.add_argument("--thresholds", nargs="+", type=float, default=[0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
    parser.add_argument(
        "--tolerances",
        nargs="+",
        type=float,
        default=[0.0, 0.0005, 0.001, 0.003, 0.005, 0.01, 0.03, 0.05, 0.1],
    )
    parser.add_argument("--input-size", type=int, default=18)
    parser.add_argument("--hidden-size", type=int, default=64)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--python", default="python3")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    experiments = [
        {
            "name": "univ1_to_univ1",
            "model_path": "runs/gru/univ1_pretrain/best.pt",
            "scaler_path": "runs/gru/univ1_pretrain/scaler.json",
            "test_file": "splits/univ1_padded/test.jsonl",
        },
        {
            "name": "univ1_to_mininet",
            "model_path": "runs/gru/univ1_pretrain/best.pt",
            "scaler_path": "runs/gru/univ1_pretrain/scaler.json",
            "test_file": "splits/mininet_padded/test.jsonl",
        },
        {
            "name": "mininet_to_univ1",
            "model_path": "runs/gru/mininet_pretrain/best.pt",
            "scaler_path": "runs/gru/mininet_pretrain/scaler.json",
            "test_file": "splits/univ1_padded/test.jsonl",
        },
        {
            "name": "mininet_to_mininet",
            "model_path": "runs/gru/mininet_pretrain/best.pt",
            "scaler_path": "runs/gru/mininet_pretrain/scaler.json",
            "test_file": "splits/mininet_padded/test.jsonl",
        },
    ]

    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []
    for experiment in experiments:
        exp_dir = output_root / experiment["name"]
        sweep_out = exp_dir / "sweep.csv"
        metrics_out = exp_dir / "metrics.json"
        plot_out = exp_dir / "plots" / "eval.png"
        log_out = exp_dir / "eval.log"

        command = build_evaluate_command(
            python_bin=args.python,
            model_path=experiment["model_path"],
            test_file=experiment["test_file"],
            scaler_path=experiment["scaler_path"],
            thresholds=args.thresholds,
            tolerances=args.tolerances,
            sweep_out=sweep_out,
            metrics_out=metrics_out,
            plot_out=plot_out,
            input_size=args.input_size,
            hidden_size=args.hidden_size,
            device=args.device,
        )

        print(f"[run] {experiment['name']}")
        print(" ".join(command))
        if not args.dry_run:
            exp_dir.mkdir(parents=True, exist_ok=True)
            # ensure plot parent exists as evaluate.py will write into it
            (plot_out.parent).mkdir(parents=True, exist_ok=True)
            run_one(command, log_path=str(log_out))

        summary.append(
            {
                **experiment,
                "sweep_out": str(sweep_out),
                "metrics_out": str(metrics_out),
                "plot_out": str(plot_out),
            }
        )

    summary_path = output_root / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {summary_path}")


if __name__ == "__main__":
    main()
