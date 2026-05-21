import argparse
import json
import random
from pathlib import Path

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

try:
    from .data import FeatureScaler, FlowJsonlDataset, collate_flow_batch, fit_feature_scaler, load_jsonl
    from .models import DiffEarlyExitGRU
except ImportError:
    from data import FeatureScaler, FlowJsonlDataset, collate_flow_batch, fit_feature_scaler, load_jsonl
    from models import DiffEarlyExitGRU


def set_seed(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def load_checkpoint_state(path: str | Path, device: torch.device) -> dict:
    state = torch.load(path, map_location=device)
    if isinstance(state, dict) and "model_state_dict" in state:
        return state["model_state_dict"]
    return state


def freeze_layers(model: DiffEarlyExitGRU, mode: str) -> list[str]:
    if mode == "none":
        for parameter in model.parameters():
            parameter.requires_grad = True
        return []

    if mode != "backbone":
        raise ValueError(f"unsupported freeze mode: {mode}")

    frozen: list[str] = []
    for name, parameter in model.named_parameters():
        trainable = name.startswith("classifier")
        parameter.requires_grad = trainable
        if not trainable:
            frozen.append(name)
    return frozen


def final_step_predictions(outputs: torch.Tensor, seq_len: torch.Tensor) -> torch.Tensor:
    batch_indices = torch.arange(outputs.size(0), device=outputs.device)
    final_indices = seq_len.clamp(min=1, max=outputs.size(1)) - 1
    return outputs[batch_indices, final_indices, :]


def masked_step_predictions(outputs: torch.Tensor, seq_len: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    max_len = outputs.size(1)
    steps = torch.arange(max_len, device=outputs.device).unsqueeze(0)
    mask = steps < seq_len.unsqueeze(1)
    return outputs.squeeze(-1), mask


def loss_values(pred: torch.Tensor, target: torch.Tensor, loss_name: str) -> torch.Tensor:
    if loss_name in {"bce", "weighted-bce"}:
        return F.binary_cross_entropy(pred.clamp(1e-6, 1 - 1e-6), target, reduction="none")
    if loss_name in {"mse", "weighted-mse"}:
        return F.mse_loss(pred, target, reduction="none")
    if loss_name in {"huber", "weighted-huber"}:
        return F.smooth_l1_loss(pred, target, reduction="none")
    raise ValueError(f"unsupported loss: {loss_name}")


def weighted_mean(values: torch.Tensor, weights: torch.Tensor) -> torch.Tensor:
    denominator = weights.sum().clamp(min=1e-6)
    return (values * weights).sum() / denominator


def batch_loss(
    outputs: torch.Tensor,
    batch: dict,
    loss_name: str,
    elephant_weight: float,
    step_loss_weight: float,
) -> torch.Tensor:
    seq_len = batch["seq_len"]
    labels = batch["label"].view(-1, 1)
    targets = batch["target"]

    final_pred = final_step_predictions(outputs, seq_len)
    final_losses = loss_values(final_pred, targets, loss_name)
    sample_weights = torch.where(
        labels >= 0.5,
        torch.full_like(labels, elephant_weight),
        torch.ones_like(labels),
    )
    total = weighted_mean(final_losses, sample_weights)

    if step_loss_weight <= 0:
        return total

    step_pred, step_mask = masked_step_predictions(outputs, seq_len)
    step_targets = targets.view(-1, 1).expand_as(step_pred)
    step_losses = loss_values(step_pred, step_targets, loss_name)
    step_weights = sample_weights.expand_as(step_pred) * step_mask.float()
    return total + step_loss_weight * weighted_mean(step_losses, step_weights)


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
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        **cm,
    }


def move_batch(batch: dict, device: torch.device) -> dict:
    moved = {}
    for key, value in batch.items():
        moved[key] = value.to(device) if isinstance(value, torch.Tensor) else value
    return moved


def train_one_epoch(
    model: DiffEarlyExitGRU,
    loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    device: torch.device,
    loss_name: str,
    elephant_weight: float,
    step_loss_weight: float,
) -> float:
    model.train()
    loss_sum = 0.0
    sample_count = 0

    for batch in loader:
        batch = move_batch(batch, device)
        optimizer.zero_grad()
        outputs = model(batch["x"], batch["direction"], seq_len=batch["seq_len"])
        loss = batch_loss(outputs, batch, loss_name, elephant_weight, step_loss_weight)
        loss.backward()
        optimizer.step()

        batch_size = batch["x"].size(0)
        loss_sum += float(loss.item()) * batch_size
        sample_count += batch_size

    return loss_sum / sample_count if sample_count else 0.0


@torch.no_grad()
def evaluate_epoch(
    model: DiffEarlyExitGRU,
    loader: DataLoader,
    device: torch.device,
    loss_name: str,
    elephant_weight: float,
    step_loss_weight: float,
    threshold: float,
) -> dict:
    model.eval()
    loss_sum = 0.0
    sample_count = 0
    scores: list[float] = []
    labels: list[int] = []

    for batch in loader:
        batch = move_batch(batch, device)
        outputs = model(batch["x"], batch["direction"], seq_len=batch["seq_len"])
        loss = batch_loss(outputs, batch, loss_name, elephant_weight, step_loss_weight)
        final_pred = final_step_predictions(outputs, batch["seq_len"])

        batch_size = batch["x"].size(0)
        loss_sum += float(loss.item()) * batch_size
        sample_count += batch_size
        scores.extend(float(value) for value in final_pred.view(-1).cpu())
        labels.extend(int(value) for value in batch["label"].view(-1).cpu())

    metrics = classification_metrics(labels, scores, threshold=threshold)
    metrics["loss"] = loss_sum / sample_count if sample_count else 0.0
    return metrics


def make_loader(
    paths: list[str],
    scaler: FeatureScaler,
    target: str,
    cdf_method: str,
    size_field: str,
    batch_size: int,
    shuffle: bool,
) -> DataLoader:
    dataset = FlowJsonlDataset(
        paths,
        scaler=scaler,
        target=target,
        cdf_method=cdf_method,
        size_field=size_field,
    )
    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        collate_fn=collate_flow_batch,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-file", nargs="+", required=True)
    parser.add_argument("--val-file", nargs="+", required=True)
    parser.add_argument("--checkpoint-out", required=True)
    parser.add_argument("--scaler-in")
    parser.add_argument("--scaler-out")
    parser.add_argument("--init-checkpoint")
    parser.add_argument("--target", choices=["label", "cdf"], default="label")
    parser.add_argument(
        "--loss",
        choices=["bce", "weighted-bce", "mse", "weighted-mse", "huber", "weighted-huber"],
        default="bce",
    )
    parser.add_argument("--cdf-method", choices=["average", "min", "max", "ordinal"], default="average")
    parser.add_argument("--size-field", default="flow_size_bytes")
    parser.add_argument("--input-size", type=int, default=11)
    parser.add_argument("--hidden-size", type=int, default=64)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--elephant-weight", type=float, default=1.0)
    parser.add_argument("--step-loss-weight", type=float, default=1.0)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--freeze", choices=["none", "backbone"], default="none")
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--metrics-out")
    args = parser.parse_args()

    set_seed(args.seed)
    device = torch.device(args.device)

    if args.scaler_in:
        scaler = FeatureScaler.load(args.scaler_in)
    else:
        train_samples = load_jsonl(args.train_file)
        scaler = fit_feature_scaler(train_samples, input_size=args.input_size)

    if args.scaler_out:
        scaler.save(args.scaler_out)

    train_loader = make_loader(
        args.train_file,
        scaler=scaler,
        target=args.target,
        cdf_method=args.cdf_method,
        size_field=args.size_field,
        batch_size=args.batch_size,
        shuffle=True,
    )
    val_loader = make_loader(
        args.val_file,
        scaler=scaler,
        target=args.target,
        cdf_method=args.cdf_method,
        size_field=args.size_field,
        batch_size=args.batch_size,
        shuffle=False,
    )

    model = DiffEarlyExitGRU(input_size=args.input_size, hidden_size=args.hidden_size).to(device)
    if args.init_checkpoint:
        model.load_state_dict(load_checkpoint_state(args.init_checkpoint, device))

    frozen_layers = freeze_layers(model, args.freeze)
    trainable_parameters = [parameter for parameter in model.parameters() if parameter.requires_grad]
    if not trainable_parameters:
        raise ValueError("no trainable parameters remain after freeze")

    optimizer = torch.optim.Adam(trainable_parameters, lr=args.lr)

    checkpoint_path = Path(args.checkpoint_out)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    best_val_loss = float("inf")
    history: list[dict] = []

    for epoch in range(1, args.epochs + 1):
        train_loss = train_one_epoch(
            model,
            train_loader,
            optimizer,
            device,
            loss_name=args.loss,
            elephant_weight=args.elephant_weight,
            step_loss_weight=args.step_loss_weight,
        )
        val_metrics = evaluate_epoch(
            model,
            val_loader,
            device,
            loss_name=args.loss,
            elephant_weight=args.elephant_weight,
            step_loss_weight=args.step_loss_weight,
            threshold=args.threshold,
        )
        row = {"epoch": epoch, "train_loss": train_loss, **val_metrics}
        history.append(row)

        print(
            f"epoch={epoch:03d} train_loss={train_loss:.6f} val_loss={val_metrics['loss']:.6f} "
            f"acc={val_metrics['accuracy']:.4f} prec={val_metrics['precision']:.4f} "
            f"rec={val_metrics['recall']:.4f} f1={val_metrics['f1']:.4f}"
        )

        if val_metrics["loss"] < best_val_loss:
            best_val_loss = float(val_metrics["loss"])
            torch.save(
                {
                    "model_state_dict": model.state_dict(),
                    "config": vars(args),
                    "frozen_layers": frozen_layers,
                    "best_val_loss": best_val_loss,
                    "epoch": epoch,
                    "history": history,
                    "scaler": {"x_min": scaler.x_min, "x_max": scaler.x_max},
                },
                checkpoint_path,
            )

    if args.metrics_out:
        metrics_path = Path(args.metrics_out)
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(
            json.dumps(
                {
                    "best_val_loss": best_val_loss,
                    "history": history,
                    "checkpoint": str(checkpoint_path),
                    "scaler": args.scaler_out,
                    "frozen_layers": frozen_layers,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    print(f"best checkpoint: {checkpoint_path}")


if __name__ == "__main__":
    main()
