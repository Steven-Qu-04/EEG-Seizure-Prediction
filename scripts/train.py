#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import platform
import random
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from tqdm import tqdm

from arch_decode import decode_arch, get_default_search_space, validate_arch_kwargs
from CNN_base import EEGCNN
from dataloader import build_dataloaders
from RNN import NASController


ARCH_PARAM_NAMES = [
    "t_out_1",
    "t_out_2",
    "t_out_3",
    "t_k_1",
    "t_k_2",
    "t_k_3",
    "t_pool_1",
    "t_pool_2",
    "t_pool_3",
    "s_out_1",
    "s_out_2",
    "s_k_1",
    "s_k_2",
    "s_pool_1",
    "s_pool_2",
    "use_bn",
    "activation",
    "pool_type",
    "conv_bias",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RNN-NAS + CNN training pipeline for SIENA slices.")
    p.add_argument("--data-root", default="data/processed/siena_slices")
    p.add_argument("--dataset-layout", default="siena", choices=["siena", "kaggle"])
    p.add_argument("--window", default="win10s", choices=["win10s", "win20s", "win30s"])
    p.add_argument("--version", default="v1", choices=["v1", "v2", "v3"])
    p.add_argument("--output-root", default="/hy-tmp/result")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--num-workers", type=int, default=0)
    p.add_argument("--epochs", type=int, default=8)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--test-ratio", type=float, default=0.2)
    p.add_argument("--val-ratio", type=float, default=0.2)
    p.add_argument("--train-sampler", default="balanced-over", choices=["none", "balanced-over", "balanced-under"])
    p.add_argument("--nas-rounds", type=int, default=15)
    p.add_argument("--nas-samples", type=int, default=8)
    p.add_argument("--controller-lr", type=float, default=1e-3)
    p.add_argument("--controller-grad-clip", type=float, default=5.0)
    p.add_argument("--baseline-decay", type=float, default=0.9)
    p.add_argument("--fixed-arch", action="store_true", help="Train one default CNN directly without RNN-NAS search.")
    p.add_argument("--fixed-arch-json", default=None, help="JSON architecture file used with --fixed-arch.")
    p.add_argument("--class-weight", default="none", choices=["balanced", "auto", "none"])
    p.add_argument("--far-penalty", type=float, default=1e-3)
    p.add_argument(
        "--reward",
        default="balanced",
        choices=["balanced", "sensitivity", "specificity", "accuracy", "precision", "recall", "f1", "macro_f1", "macro-f1"],
        help="Validation metric used as the RNN-NAS controller reward. 'balanced' keeps sensitivity - far_penalty * FAR.",
    )
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def get_git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
        return out
    except Exception:
        return "N/A"


def far_fp_per_negative_hour(fp: int, tn: int, window_sec: int) -> float:
    negative_hours = (tn * window_sec) / 3600.0
    if negative_hours <= 0:
        return 0.0
    return float(fp / negative_hours)


def compute_metrics(y_true: np.ndarray, y_pred: np.ndarray, window_sec: int) -> Dict[str, float]:
    tp = int(((y_true == 1) & (y_pred == 1)).sum())
    tn = int(((y_true == 0) & (y_pred == 0)).sum())
    fp = int(((y_true == 0) & (y_pred == 1)).sum())
    fn = int(((y_true == 1) & (y_pred == 0)).sum())
    acc = (tp + tn) / max(1, tp + tn + fp + fn)
    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    f1 = 2 * precision * recall / max(1e-12, (precision + recall))
    neg_precision = tn / max(1, tn + fn)
    neg_recall = tn / max(1, tn + fp)
    neg_f1 = 2 * neg_precision * neg_recall / max(1e-12, (neg_precision + neg_recall))
    macro_f1 = (neg_f1 + f1) / 2
    specificity = tn / max(1, tn + fp)
    sensitivity = recall
    far = far_fp_per_negative_hour(fp=fp, tn=tn, window_sec=window_sec)
    return {
        "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "accuracy": acc, "precision": precision, "recall": recall, "f1": f1, "macro_f1": macro_f1,
        "specificity": specificity, "sensitivity": sensitivity, "far": far,
    }


def build_criterion(train_ds, device, class_weight: str):
    if class_weight == "none":
        return nn.CrossEntropyLoss(), {"neg": 1.0, "pos": 1.0}

    if class_weight == "balanced":
        weights = torch.tensor([1.0, 1.5], dtype=torch.float32, device=device)
        return nn.CrossEntropyLoss(weight=weights), {"neg": float(weights[0].item()), "pos": float(weights[1].item())}

    counts = train_ds.class_counts()
    neg = max(1, int(counts["neg"]))
    pos = max(1, int(counts["pos"]))
    total = neg + pos
    weights = torch.tensor(
        [total / (2.0 * neg), total / (2.0 * pos)],
        dtype=torch.float32,
        device=device,
    )
    return nn.CrossEntropyLoss(weight=weights), {"neg": float(weights[0].item()), "pos": float(weights[1].item())}


def run_epoch(model, loader, criterion, optimizer, device, desc: str = "train") -> float:
    model.train()
    losses = []
    pbar = tqdm(loader, desc=desc, leave=False)
    for x, y in pbar:
        x, y = x.to(device), y.to(device)
        optimizer.zero_grad()
        logits = model(x)
        loss = criterion(logits, y)
        loss.backward()
        optimizer.step()
        loss_v = float(loss.item())
        losses.append(loss_v)
        pbar.set_postfix(loss=f"{loss_v:.4f}")
    return float(np.mean(losses)) if losses else 0.0


@torch.no_grad()
def eval_epoch(model, loader, criterion, device, window_sec: int, return_outputs: bool = False) -> Dict[str, float]:
    model.eval()
    losses = []
    ys, ps, pos_probs = [], [], []
    pbar = tqdm(loader, desc="val", leave=False)
    for x, y in pbar:
        x, y = x.to(device), y.to(device)
        logits = model(x)
        loss = criterion(logits, y)
        loss_v = float(loss.item())
        losses.append(loss_v)
        pbar.set_postfix(loss=f"{loss_v:.4f}")
        pred = torch.argmax(logits, dim=1)
        prob = torch.softmax(logits, dim=1)[:, 1]
        ys.append(y.cpu().numpy())
        ps.append(pred.cpu().numpy())
        pos_probs.append(prob.cpu().numpy())
    if ys:
        y_true = np.concatenate(ys)
        y_pred = np.concatenate(ps)
        y_prob = np.concatenate(pos_probs)
    else:
        y_true = np.zeros((0,), dtype=np.int64)
        y_pred = np.zeros((0,), dtype=np.int64)
        y_prob = np.zeros((0,), dtype=np.float32)
    m = compute_metrics(y_true, y_pred, window_sec=window_sec)
    m["loss"] = float(np.mean(losses)) if losses else 0.0
    if return_outputs:
        m["y_true"] = y_true
        m["y_pred"] = y_pred
        m["y_prob_pos"] = y_prob
    return m


def binary_roc_curve(y_true: np.ndarray, y_score: np.ndarray):
    y_true = np.asarray(y_true).astype(np.int64)
    y_score = np.asarray(y_score).astype(np.float64)
    pos = int((y_true == 1).sum())
    neg = int((y_true == 0).sum())
    if pos == 0 or neg == 0:
        return np.array([0.0, 1.0]), np.array([0.0, 1.0]), np.array([np.inf, -np.inf]), float("nan")

    order = np.argsort(-y_score, kind="mergesort")
    y_true = y_true[order]
    y_score = y_score[order]
    distinct = np.where(np.diff(y_score))[0]
    threshold_idxs = np.r_[distinct, y_true.size - 1]

    tps = np.cumsum(y_true == 1)[threshold_idxs]
    fps = np.cumsum(y_true == 0)[threshold_idxs]
    tpr = np.r_[0.0, tps / pos]
    fpr = np.r_[0.0, fps / neg]
    thresholds = np.r_[np.inf, y_score[threshold_idxs]]
    auc = float(np.trapz(tpr, fpr))
    return fpr, tpr, thresholds, auc


def strip_array_outputs(metrics: Dict[str, float]) -> Dict[str, float]:
    return {k: v for k, v in metrics.items() if k not in {"y_true", "y_pred", "y_prob_pos"}}


def save_probability_analysis(metrics: Dict[str, float], out_dir: Path, split_name: str) -> Dict[str, float]:
    y_true = np.asarray(metrics["y_true"]).astype(np.int64)
    y_pred = np.asarray(metrics["y_pred"]).astype(np.int64)
    y_prob = np.asarray(metrics["y_prob_pos"]).astype(np.float64)
    fpr, tpr, thresholds, auc = binary_roc_curve(y_true, y_prob)

    csv_path = out_dir / f"{split_name}_probabilities.csv"
    with csv_path.open("w", encoding="utf-8") as f:
        f.write("index,y_true,y_pred,p_preictal\n")
        for i, (yt, yp, prob) in enumerate(zip(y_true, y_pred, y_prob)):
            f.write(f"{i},{int(yt)},{int(yp)},{float(prob):.8f}\n")

    roc_csv_path = out_dir / f"{split_name}_roc_curve.csv"
    with roc_csv_path.open("w", encoding="utf-8") as f:
        f.write("threshold,fpr,tpr\n")
        for thr, fp, tp in zip(thresholds, fpr, tpr):
            thr_s = "inf" if np.isposinf(thr) else f"{float(thr):.8f}"
            f.write(f"{thr_s},{float(fp):.8f},{float(tp):.8f}\n")

    plt.figure(figsize=(7, 5))
    neg_probs = y_prob[y_true == 0]
    pos_probs = y_prob[y_true == 1]
    bins = np.linspace(0.0, 1.0, 51)
    plt.hist(neg_probs, bins=bins, alpha=0.65, density=True, label=f"interictal(0), n={len(neg_probs)}")
    plt.hist(pos_probs, bins=bins, alpha=0.65, density=True, label=f"preictal(1), n={len(pos_probs)}")
    plt.axvline(0.5, color="black", linestyle="--", linewidth=1, label="argmax threshold")
    plt.xlabel("P(preictal)")
    plt.ylabel("Density")
    plt.title(f"{split_name} P(preictal) Distribution")
    plt.legend()
    plt.tight_layout()
    prob_plot_path = out_dir / f"{split_name}_p_preictal_distribution.png"
    plt.savefig(prob_plot_path, dpi=180)
    plt.close()

    plt.figure(figsize=(6, 6))
    plt.plot(fpr, tpr, label=f"AUC={auc:.4f}" if np.isfinite(auc) else "AUC=N/A")
    plt.plot([0, 1], [0, 1], linestyle="--", color="gray", linewidth=1)
    plt.xlabel("False Positive Rate")
    plt.ylabel("True Positive Rate")
    plt.title(f"{split_name} ROC Curve")
    plt.legend(loc="lower right")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    roc_plot_path = out_dir / f"{split_name}_roc_curve.png"
    plt.savefig(roc_plot_path, dpi=180)
    plt.close()

    return {
        "auc_roc": auc,
        "probabilities_csv": str(csv_path),
        "roc_curve_csv": str(roc_csv_path),
        "probability_plot": str(prob_plot_path),
        "roc_plot": str(roc_plot_path),
    }


def preflight_forward(model, loader, device) -> Dict[str, str]:
    try:
        x, _ = next(iter(loader))
        x = x.to(device)
        y = model(x)
        if y.ndim != 2:
            return {"ok": "false", "reason": f"output ndim={y.ndim}"}
        return {"ok": "true", "reason": "ok"}
    except Exception as e:
        return {"ok": "false", "reason": f"{type(e).__name__}: {e}"}


def balanced_selection_score(metrics: Dict[str, float], far_penalty: float) -> float:
    return float(metrics["sensitivity"] - far_penalty * metrics["far"])


def compute_selection_score(metrics: Dict[str, float], reward: str, far_penalty: float) -> float:
    reward = reward.replace("-", "_")
    if reward == "balanced":
        return balanced_selection_score(metrics, far_penalty=far_penalty)
    return float(metrics[reward])


def reward_description(reward: str, far_penalty: float) -> str:
    reward = reward.replace("-", "_")
    if reward == "balanced":
        return f"sensitivity - {far_penalty} * FAR"
    return reward


def snapshot_model_state(model) -> Dict[str, torch.Tensor]:
    return {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}


def parse_window_sec(window: str) -> int:
    return int(window.replace("win", "").replace("s", ""))


def save_curves(train_losses: List[float], val_losses: List[float], out_dir: Path) -> None:
    save_loss_curve(
        train_losses=train_losses,
        val_losses=val_losses,
        out_path=out_dir / "loss_curve.png",
        title="Training and Validation Loss",
    )


def save_loss_curve(train_losses: List[float], val_losses: List[float], out_path: Path, title: str) -> None:
    plt.figure(figsize=(8, 5))
    plt.plot(train_losses, label="train_loss")
    plt.plot(val_losses, label="val_loss")
    plt.xlabel("Epoch")
    plt.ylabel("Loss")
    plt.title(title)
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def classification_report_from_metrics(metrics: Dict[str, float]) -> str:
    tn = int(metrics["tn"])
    fp = int(metrics["fp"])
    fn = int(metrics["fn"])
    tp = int(metrics["tp"])

    neg_precision = tn / max(1, tn + fn)
    neg_recall = tn / max(1, tn + fp)
    neg_f1 = 2 * neg_precision * neg_recall / max(1e-12, neg_precision + neg_recall)
    neg_support = tn + fp

    pos_precision = tp / max(1, tp + fp)
    pos_recall = tp / max(1, tp + fn)
    pos_f1 = 2 * pos_precision * pos_recall / max(1e-12, pos_precision + pos_recall)
    pos_support = tp + fn

    total = neg_support + pos_support
    accuracy = (tp + tn) / max(1, total)
    macro_precision = (neg_precision + pos_precision) / 2
    macro_recall = (neg_recall + pos_recall) / 2
    macro_f1 = (neg_f1 + pos_f1) / 2
    weighted_precision = (neg_precision * neg_support + pos_precision * pos_support) / max(1, total)
    weighted_recall = (neg_recall * neg_support + pos_recall * pos_support) / max(1, total)
    weighted_f1 = (neg_f1 * neg_support + pos_f1 * pos_support) / max(1, total)

    lines = [
        "Classification Report (validation split)",
        "",
        f"{'class':<14}{'precision':>11}{'recall':>11}{'f1-score':>11}{'support':>11}",
        f"{'interictal(0)':<14}{neg_precision:>11.4f}{neg_recall:>11.4f}{neg_f1:>11.4f}{neg_support:>11d}",
        f"{'preictal(1)':<14}{pos_precision:>11.4f}{pos_recall:>11.4f}{pos_f1:>11.4f}{pos_support:>11d}",
        "",
        f"{'accuracy':<14}{'':>11}{'':>11}{accuracy:>11.4f}{total:>11d}",
        f"{'macro avg':<14}{macro_precision:>11.4f}{macro_recall:>11.4f}{macro_f1:>11.4f}{total:>11d}",
        f"{'weighted avg':<14}{weighted_precision:>11.4f}{weighted_recall:>11.4f}{weighted_f1:>11.4f}{total:>11d}",
        "",
        "Confusion Matrix",
        f"- tn={tn}, fp={fp}, fn={fn}, tp={tp}",
        "",
        "Seizure-prediction Metrics",
        f"- sensitivity={metrics['sensitivity']:.6f}",
        f"- specificity={metrics['specificity']:.6f}",
        f"- far={metrics['far']:.6f}",
        f"- loss={metrics['loss']:.6f}",
    ]
    return "\n".join(lines)


def get_default_cnn_kwargs(input_channels: int, num_classes: int = 2) -> Dict:
    return {
        "input_channels": int(input_channels),
        "num_classes": int(num_classes),
        "temporal_out_channels": (4, 16, 16),
        "temporal_kernel_widths": (8, 16, 8),
        "temporal_pool_widths": (8, 4, 4),
        "spatial_out_channels": (16, 16),
        "spatial_kernel_heights": (16, 16),
        "spatial_pool_heights": (4, 4),
        "use_batch_norm": True,
        "activation": "relu",
        "pool_type": "max",
        "conv_bias": False,
        "return_probabilities": False,
    }


def load_fixed_arch_kwargs(path: str, input_channels: int, num_classes: int = 2) -> Dict:
    kwargs = json.loads(Path(path).read_text(encoding="utf-8"))
    tuple_keys = [
        "temporal_out_channels",
        "temporal_kernel_widths",
        "temporal_pool_widths",
        "spatial_out_channels",
        "spatial_kernel_heights",
        "spatial_pool_heights",
    ]
    for key in tuple_keys:
        if key in kwargs:
            kwargs[key] = tuple(kwargs[key])
    kwargs["input_channels"] = int(input_channels)
    kwargs["num_classes"] = int(num_classes)
    kwargs["return_probabilities"] = False
    return kwargs


def _ok_trials(nas_trials: List[Dict]) -> List[Dict]:
    return [t for t in nas_trials if t.get("status") == "ok" and "val_metrics_for_selection" in t]


def save_nas_hyperparameter_search_plot(nas_trials: List[Dict], out_dir: Path) -> None:
    trials = [t for t in nas_trials if "sampled_arch" in t]
    if not trials:
        return

    arch_matrix = np.array([t["sampled_arch"] for t in trials], dtype=float).T
    trial_labels = [int(t.get("trial_id", i + 1)) for i, t in enumerate(trials)]

    fig_w = max(10, min(24, 0.35 * len(trials) + 7))
    fig_h = 9
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    im = ax.imshow(arch_matrix, aspect="auto", cmap="viridis")
    ax.set_title("RNN-NAS Hyperparameter Search Process")
    ax.set_xlabel("Trial")
    ax.set_ylabel("Sampled hyperparameter")
    ax.set_yticks(np.arange(len(ARCH_PARAM_NAMES)))
    ax.set_yticklabels(ARCH_PARAM_NAMES)

    tick_step = max(1, len(trial_labels) // 20)
    tick_pos = np.arange(0, len(trial_labels), tick_step)
    ax.set_xticks(tick_pos)
    ax.set_xticklabels([str(trial_labels[i]) for i in tick_pos], rotation=45, ha="right")

    for i in range(arch_matrix.shape[0]):
        for j in range(arch_matrix.shape[1]):
            if arch_matrix.shape[1] <= 40:
                ax.text(j, i, f"{int(arch_matrix[i, j])}", ha="center", va="center", fontsize=7, color="white")

    invalid_x = [i for i, t in enumerate(trials) if t.get("status") != "ok"]
    if invalid_x:
        ax.scatter(invalid_x, [-0.7] * len(invalid_x), marker="x", color="red", label="invalid trial", clip_on=False)
        ax.legend(loc="upper right")

    cbar = fig.colorbar(im, ax=ax, pad=0.01)
    cbar.set_label("Sampled value")
    fig.tight_layout()
    fig.savefig(out_dir / "nas_hyperparameter_search.png", dpi=180)
    plt.close(fig)


def save_nas_performance_progress_plot(nas_trials: List[Dict], out_dir: Path) -> None:
    trials = _ok_trials(nas_trials)
    if not trials:
        return

    trial_ids = np.array([int(t["trial_id"]) for t in trials])
    val_metrics = [t["val_metrics_for_selection"] for t in trials]
    sensitivity = np.array([float(m["sensitivity"]) for m in val_metrics])
    f1 = np.array([float(m["f1"]) for m in val_metrics])
    loss = np.array([float(m["loss"]) for m in val_metrics])
    far = np.array([float(m["far"]) for m in val_metrics])
    best_sensitivity = np.maximum.accumulate(sensitivity)
    best_f1 = np.maximum.accumulate(f1)

    fig, axes = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    axes[0].plot(trial_ids, sensitivity, marker="o", label="sensitivity")
    axes[0].plot(trial_ids, best_sensitivity, linestyle="--", label="best sensitivity")
    axes[0].plot(trial_ids, f1, marker="s", label="f1")
    axes[0].plot(trial_ids, best_f1, linestyle="--", label="best f1")
    axes[0].set_ylabel("Score")
    axes[0].set_ylim(-0.02, 1.02)
    axes[0].set_title("Validation Performance Across NAS Trials")
    axes[0].grid(alpha=0.3)
    axes[0].legend(loc="best")

    ax_loss = axes[1]
    ax_far = ax_loss.twinx()
    ax_loss.plot(trial_ids, loss, color="tab:blue", marker="o", label="val loss")
    ax_far.plot(trial_ids, far, color="tab:red", marker="s", label="FAR")
    ax_loss.set_xlabel("Trial")
    ax_loss.set_ylabel("Validation loss", color="tab:blue")
    ax_far.set_ylabel("FAR (FP / negative hour)", color="tab:red")
    ax_loss.tick_params(axis="y", labelcolor="tab:blue")
    ax_far.tick_params(axis="y", labelcolor="tab:red")
    ax_loss.grid(alpha=0.3)

    lines_1, labels_1 = ax_loss.get_legend_handles_labels()
    lines_2, labels_2 = ax_far.get_legend_handles_labels()
    ax_loss.legend(lines_1 + lines_2, labels_1 + labels_2, loc="best")

    fig.tight_layout()
    fig.savefig(out_dir / "nas_performance_progress.png", dpi=180)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    if args.fixed_arch_json and not args.fixed_arch:
        raise ValueError("--fixed-arch-json can only be used with --fixed-arch")
    set_seed(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_root) / f"{ts}_{args.window}_{args.version}"
    out_dir.mkdir(parents=True, exist_ok=True)

    d = build_dataloaders(
        data_root=args.data_root,
        window=args.window,
        version=args.version,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        seed=args.seed,
        test_ratio=args.test_ratio,
        train_sampler=args.train_sampler,
        layout=args.dataset_layout,
        val_ratio=args.val_ratio,
    )
    train_loader = d["train_loader"]
    val_loader = d["val_loader"]     # permanent val for NAS selection
    test_loader = d["test_loader"]   # final test on remaining split
    manifest = d["manifest"]
    window_sec = parse_window_sec(args.window)

    manifest_path = out_dir / "split_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if args.dry_run:
        dummy_search = get_default_search_space()
        controller = NASController(search_space=dummy_search)
        arch, _, _ = controller.sample()
        kwargs = decode_arch(arch, input_channels=d["train_ds"].num_channels(), num_classes=2)
        ok, reason = validate_arch_kwargs(kwargs)
        dry = {"dry_run": True, "sampled_arch": arch, "decoded_ok": ok, "reason": reason, "manifest": manifest}
        (out_dir / "nas_trials.json").write_text(json.dumps([dry], indent=2), encoding="utf-8")
        print(f"Dry-run complete: {out_dir}")
        return

    if args.fixed_arch:
        criterion, class_weights = build_criterion(d["train_ds"], device=device, class_weight=args.class_weight)
        fixed_arch_json_path = str(Path(args.fixed_arch_json).resolve()) if args.fixed_arch_json else None
        if args.fixed_arch_json:
            kwargs = load_fixed_arch_kwargs(args.fixed_arch_json, input_channels=d["train_ds"].num_channels(), num_classes=2)
        else:
            kwargs = get_default_cnn_kwargs(input_channels=d["train_ds"].num_channels(), num_classes=2)
        valid, reason = validate_arch_kwargs(kwargs)
        if not valid:
            raise RuntimeError(f"Invalid fixed CNN architecture: {reason}")

        model = EEGCNN(**kwargs).to(device)
        pf = preflight_forward(model, train_loader, device=device)
        if pf["ok"] != "true":
            raise RuntimeError(f"Fixed CNN preflight failed: {pf['reason']}")

        optimizer = optim.Adam(model.parameters(), lr=args.lr)
        train_losses = []
        val_losses = []
        epoch_history = []
        best_epoch = None
        best_epoch_score = float("-inf")
        best_epoch_metrics = None
        best_epoch_state = None
        t_start = datetime.now()
        for e in range(args.epochs):
            tr_loss = run_epoch(
                model,
                train_loader,
                criterion,
                optimizer,
                device,
                desc=f"fixed CNN epoch {e + 1}/{args.epochs} train",
            )
            va = eval_epoch(model, val_loader, criterion, device, window_sec=window_sec)
            train_losses.append(tr_loss)
            val_losses.append(va["loss"])
            selection_score = compute_selection_score(va, reward=args.reward, far_penalty=args.far_penalty)
            epoch_history.append({"epoch": e + 1, "train_loss": tr_loss, "selection_score": selection_score, "val_metrics": va})
            if selection_score > best_epoch_score:
                best_epoch = e + 1
                best_epoch_score = selection_score
                best_epoch_metrics = va
                best_epoch_state = snapshot_model_state(model)

        if best_epoch_state is not None:
            model.load_state_dict(best_epoch_state)
        val_m_full = eval_epoch(model, val_loader, criterion, device, window_sec=window_sec, return_outputs=True)
        test_m_full = eval_epoch(model, test_loader, criterion, device, window_sec=window_sec, return_outputs=True)
        val_analysis = save_probability_analysis(val_m_full, out_dir=out_dir, split_name="val")
        test_analysis = save_probability_analysis(test_m_full, out_dir=out_dir, split_name="test")
        val_m = strip_array_outputs(val_m_full)
        test_m = strip_array_outputs(test_m_full)
        t_end = datetime.now()

        torch.save(model.state_dict(), out_dir / "fixed_cnn.pt")
        (out_dir / "fixed_cnn_arch.json").write_text(json.dumps(kwargs, indent=2), encoding="utf-8")
        (out_dir / "fixed_cnn_classification_report.txt").write_text(
            classification_report_from_metrics(val_m),
            encoding="utf-8",
        )
        (out_dir / "fixed_cnn_history.json").write_text(
            json.dumps(
                {
                    "mode": "fixed_arch",
                    "epoch_history": epoch_history,
                    "best_epoch": best_epoch,
                    "best_epoch_selection_score": best_epoch_score,
                    "best_epoch_val_metrics": best_epoch_metrics,
                    "val_metrics": val_m,
                    "test_metrics": test_m,
                    "val_probability_analysis": val_analysis,
                    "test_probability_analysis": test_analysis,
                    "class_weights": class_weights,
                    "train_sampler": d["train_sampler_info"],
                    "fixed_arch_json": fixed_arch_json_path,
                    "fixed_arch_kwargs": kwargs,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        save_curves(train_losses, val_losses, out_dir=out_dir)

        report_lines = []
        report_lines.append("Fixed CNN Training Report")
        report_lines.append("")
        report_lines.append("Reproducibility")
        report_lines.append(f"- seed: {args.seed}")
        report_lines.append(f"- git_commit: {get_git_commit()}")
        report_lines.append(f"- python: {platform.python_version()}")
        report_lines.append(f"- torch: {torch.__version__}")
        report_lines.append(f"- cli_args: {vars(args)}")
        report_lines.append(f"- class_weights: {class_weights}")
        report_lines.append(f"- train_sampler: {d['train_sampler_info']}")
        report_lines.append(f"- fixed_arch_json: {fixed_arch_json_path}")
        report_lines.append(f"- selection_score: {reward_description(args.reward, args.far_penalty)}")
        report_lines.append(f"- best_epoch: {best_epoch}")
        report_lines.append(f"- start_time: {t_start.isoformat(timespec='seconds')}")
        report_lines.append(f"- end_time: {t_end.isoformat(timespec='seconds')}")
        report_lines.append("")
        report_lines.append("Dataset Selection")
        report_lines.append(f"- dataset_layout: {args.dataset_layout}")
        report_lines.append(f"- window: {args.window}")
        report_lines.append(f"- version: {args.version}")
        report_lines.append(f"- split_manifest: {manifest_path}")
        report_lines.append("")
        report_lines.append("Figures")
        report_lines.append(f"- loss_curve: {out_dir / 'loss_curve.png'}")
        report_lines.append(f"- val_probability_plot: {val_analysis['probability_plot']}")
        report_lines.append(f"- val_roc_curve: {val_analysis['roc_plot']}")
        report_lines.append(f"- test_probability_plot: {test_analysis['probability_plot']}")
        report_lines.append(f"- test_roc_curve: {test_analysis['roc_plot']}")
        report_lines.append("")
        report_lines.append("Final Validation Metrics")
        report_lines.append(
            f"- sensitivity={val_m['sensitivity']:.6f}, far={val_m['far']:.6f}, f1={val_m['f1']:.6f}, "
            f"macro_f1={val_m['macro_f1']:.6f}, acc={val_m['accuracy']:.6f}, auc_roc={val_analysis['auc_roc']:.6f}"
        )
        report_lines.append("")
        report_lines.append("Final Test Metrics (Held-out Test Split)")
        report_lines.append(
            f"- sensitivity={test_m['sensitivity']:.6f}, far={test_m['far']:.6f}, f1={test_m['f1']:.6f}, "
            f"macro_f1={test_m['macro_f1']:.6f}, acc={test_m['accuracy']:.6f}, auc_roc={test_analysis['auc_roc']:.6f}"
        )
        (out_dir / "report.txt").write_text("\n".join(report_lines), encoding="utf-8")
        print(f"Done: {out_dir}")
        return

    search_space = get_default_search_space()
    controller = NASController(search_space=search_space).to(device)
    controller_optimizer = optim.Adam(controller.parameters(), lr=args.controller_lr)
    criterion, class_weights = build_criterion(d["train_ds"], device=device, class_weight=args.class_weight)
    window_sec = parse_window_sec(args.window)

    nas_trials = []
    controller_updates = []
    best_two = []
    train_losses_global = []
    val_losses_global = []
    trial_id = 0
    baseline = None
    t_start = datetime.now()

    for r in range(args.nas_rounds):
        archs, log_probs, entropies = controller.sample_batch(batch_size=args.nas_samples)
        round_trials = []
        round_rewards = []
        valid_rewards = []

        for sample_idx, arch in enumerate(archs):
            trial_id += 1
            trial = {
                "trial_id": trial_id,
                "round": r + 1,
                "controller_round": r + 1,
                "sample_idx": sample_idx,
                "sampled_arch": arch,
                "log_prob": float(log_probs[sample_idx].detach().cpu().item()),
                "entropy": float(entropies[sample_idx].detach().cpu().item()),
            }
            round_trials.append(trial)
            round_rewards.append(None)
            try:
                kwargs = decode_arch(arch, input_channels=d["train_ds"].num_channels(), num_classes=2)
                trial["decoded_kwargs"] = kwargs
                valid, reason = validate_arch_kwargs(kwargs)
                if not valid:
                    trial["status"] = "invalid"
                    trial["failure_reason"] = reason
                    nas_trials.append(trial)
                    continue

                model = EEGCNN(**kwargs).to(device)
                pf = preflight_forward(model, train_loader, device=device)
                if pf["ok"] != "true":
                    trial["status"] = "invalid"
                    trial["failure_reason"] = f"preflight: {pf['reason']}"
                    nas_trials.append(trial)
                    continue

                optimizer = optim.Adam(model.parameters(), lr=args.lr)
                local_train_losses = []
                local_val_losses = []
                epoch_history = []
                best_epoch = None
                best_epoch_score = float("-inf")
                best_epoch_metrics = None
                best_epoch_state = None
                for e in range(args.epochs):
                    tr_loss = run_epoch(
                        model,
                        train_loader,
                        criterion,
                        optimizer,
                        device,
                        desc=f"trial {trial_id}/{args.nas_rounds * args.nas_samples} epoch {e + 1}/{args.epochs} train",
                    )
                    va = eval_epoch(model, val_loader, criterion, device, window_sec=window_sec)
                    local_train_losses.append(tr_loss)
                    local_val_losses.append(va["loss"])
                    selection_score = compute_selection_score(va, reward=args.reward, far_penalty=args.far_penalty)
                    epoch_history.append({"epoch": e + 1, "train_loss": tr_loss, "selection_score": selection_score, "val_metrics": va})
                    if selection_score > best_epoch_score:
                        best_epoch = e + 1
                        best_epoch_score = selection_score
                        best_epoch_metrics = va
                        best_epoch_state = snapshot_model_state(model)

                train_losses_global = local_train_losses
                val_losses_global = local_val_losses
                if best_epoch_state is not None:
                    model.load_state_dict(best_epoch_state)
                val_m = eval_epoch(model, val_loader, criterion, device, window_sec=window_sec)
                trial["status"] = "ok"
                trial["epoch_history"] = epoch_history
                trial["best_epoch"] = best_epoch
                trial["best_epoch_selection_score"] = best_epoch_score
                trial["best_epoch_val_metrics"] = best_epoch_metrics
                trial["val_metrics_for_selection"] = val_m
                reward = compute_selection_score(val_m, reward=args.reward, far_penalty=args.far_penalty)
                trial["reward"] = reward
                round_rewards[sample_idx] = reward
                valid_rewards.append(reward)
                nas_trials.append(trial)

                trial_dir = out_dir / "trials"
                trial_dir.mkdir(parents=True, exist_ok=True)
                save_loss_curve(
                    train_losses=local_train_losses,
                    val_losses=local_val_losses,
                    out_path=trial_dir / f"trial_{trial_id:04d}_loss_curve.png",
                    title=f"Trial {trial_id} Loss",
                )
                (trial_dir / f"trial_{trial_id:04d}_classification_report.txt").write_text(
                    classification_report_from_metrics(val_m),
                    encoding="utf-8",
                )

                candidate = {
                    "trial_id": trial_id,
                    "model": model,
                    "kwargs": kwargs,
                    "val_metrics": val_m,
                    "selection_score": reward,
                    "best_epoch": best_epoch,
                }
                best_two.append(candidate)
                best_two = sorted(best_two, key=lambda x: x["selection_score"], reverse=True)[:2]
            except Exception as e:
                trial["status"] = "invalid"
                trial["failure_reason"] = f"{type(e).__name__}: {e}"
                nas_trials.append(trial)
                continue

        invalid_reward = min(valid_rewards) - 0.1 if valid_rewards else -1.0
        for i, reward in enumerate(round_rewards):
            if reward is None:
                round_rewards[i] = invalid_reward
                round_trials[i]["reward"] = invalid_reward

        rewards = torch.tensor(round_rewards, dtype=torch.float32, device=device)
        mean_reward = rewards.mean().detach()
        if baseline is None:
            baseline = mean_reward
        else:
            baseline = args.baseline_decay * baseline + (1.0 - args.baseline_decay) * mean_reward

        baseline_for_loss = baseline.detach()
        advantage = rewards - baseline_for_loss
        policy_loss = -(log_probs * advantage.detach()).mean()
        entropy_bonus = controller.entropy_coeff * entropies.mean()
        controller_loss = controller.compute_loss(log_probs, rewards, baseline_for_loss, entropies=entropies)

        controller_optimizer.zero_grad()
        controller_loss.backward()
        grad_norm = torch.nn.utils.clip_grad_norm_(controller.parameters(), args.controller_grad_clip)
        controller_optimizer.step()

        controller_updates.append(
            {
                "round": r + 1,
                "mean_reward": float(mean_reward.cpu().item()),
                "baseline": float(baseline_for_loss.cpu().item()),
                "controller_loss": float(controller_loss.detach().cpu().item()),
                "policy_loss": float(policy_loss.detach().cpu().item()),
                "entropy_bonus": float(entropy_bonus.detach().cpu().item()),
                "grad_norm": float(grad_norm.detach().cpu().item() if isinstance(grad_norm, torch.Tensor) else grad_norm),
                "valid_trials": len(valid_rewards),
                "invalid_trials": int(len(round_rewards) - len(valid_rewards)),
                "invalid_reward": float(invalid_reward),
            }
        )

    (out_dir / "nas_trials.json").write_text(json.dumps(nas_trials, indent=2), encoding="utf-8")
    (out_dir / "controller_updates.json").write_text(json.dumps(controller_updates, indent=2), encoding="utf-8")
    torch.save(controller.state_dict(), out_dir / "controller.pt")
    (out_dir / "controller_config.json").write_text(
        json.dumps(
            {
                "search_space": search_space,
                "hidden_size": controller.lstm.hidden_size,
                "embedding_dim": controller.embedding.embedding_dim,
                "num_layers": controller.lstm.num_layers,
                "entropy_coeff": controller.entropy_coeff,
                "controller_lr": args.controller_lr,
                "controller_grad_clip": args.controller_grad_clip,
                "baseline_decay": args.baseline_decay,
                "reward": reward_description(args.reward, args.far_penalty),
                "invalid_reward": "min(valid_rewards) - 0.1, or -1.0 if all trials fail",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    save_curves(train_losses_global, val_losses_global, out_dir=out_dir)
    save_nas_hyperparameter_search_plot(nas_trials, out_dir=out_dir)
    save_nas_performance_progress_plot(nas_trials, out_dir=out_dir)

    final_test_results = []
    for i, c in enumerate(best_two, start=1):
        model = c["model"]
        test_m = eval_epoch(model, test_loader, criterion, device, window_sec=window_sec)
        final_test_results.append(
            {
                "rank": i,
                "trial_id": c["trial_id"],
                "best_epoch": c["best_epoch"],
                "selection_score": c["selection_score"],
                "test_metrics": test_m,
                "val_metrics": c["val_metrics"],
                "kwargs": c["kwargs"],
            }
        )
        torch.save(model.state_dict(), out_dir / f"best_{i}.pt")
        (out_dir / f"best_{i}_arch.json").write_text(json.dumps(c["kwargs"], indent=2), encoding="utf-8")

    t_end = datetime.now()
    report_lines = []
    report_lines.append("RNN-NAS + CNN Training Report")
    report_lines.append("")
    report_lines.append("Reproducibility")
    report_lines.append(f"- seed: {args.seed}")
    report_lines.append(f"- git_commit: {get_git_commit()}")
    report_lines.append(f"- python: {platform.python_version()}")
    report_lines.append(f"- torch: {torch.__version__}")
    report_lines.append(f"- cli_args: {vars(args)}")
    report_lines.append(f"- class_weights: {class_weights}")
    report_lines.append(f"- train_sampler: {d['train_sampler_info']}")
    report_lines.append(f"- selection_score: {reward_description(args.reward, args.far_penalty)}")
    report_lines.append(f"- start_time: {t_start.isoformat(timespec='seconds')}")
    report_lines.append(f"- end_time: {t_end.isoformat(timespec='seconds')}")
    report_lines.append("")
    report_lines.append("RNN-RL Controller")
    report_lines.append("- algorithm: REINFORCE with moving baseline")
    report_lines.append(f"- reward: {reward_description(args.reward, args.far_penalty)}")
    report_lines.append(f"- controller_lr: {args.controller_lr}")
    report_lines.append(f"- baseline_decay: {args.baseline_decay}")
    report_lines.append(f"- entropy_coeff: {controller.entropy_coeff}")
    report_lines.append(f"- controller_grad_clip: {args.controller_grad_clip}")
    report_lines.append("- invalid_reward: min(valid_rewards) - 0.1, or -1.0 if all trials fail")
    report_lines.append("")
    report_lines.append("Dataset Selection")
    report_lines.append(f"- dataset_layout: {args.dataset_layout}")
    report_lines.append(f"- window: {args.window}")
    report_lines.append(f"- version: {args.version}")
    report_lines.append(f"- split_manifest: {manifest_path}")
    report_lines.append("")
    report_lines.append("FAR Definition")
    report_lines.append("- FAR = false_positive_windows / negative_hours")
    report_lines.append("- negative_hours = (num_true_negative_windows * window_sec) / 3600")
    report_lines.append("")
    report_lines.append("Figures")
    report_lines.append(f"- loss_curve: {out_dir / 'loss_curve.png'}")
    report_lines.append(f"- nas_hyperparameter_search: {out_dir / 'nas_hyperparameter_search.png'}")
    report_lines.append(f"- nas_performance_progress: {out_dir / 'nas_performance_progress.png'}")
    report_lines.append(f"- controller_updates: {out_dir / 'controller_updates.json'}")
    report_lines.append("")
    report_lines.append("NAS Selection (Permanent Validation Only)")
    for i, c in enumerate(best_two, start=1):
        vm = c["val_metrics"]
        report_lines.append(
            f"- rank_{i}: trial={c['trial_id']}, best_epoch={c['best_epoch']}, score={c['selection_score']:.6f}, "
            f"sensitivity={vm['sensitivity']:.6f}, far={vm['far']:.6f}, f1={vm['f1']:.6f}, macro_f1={vm['macro_f1']:.6f}"
        )
    report_lines.append("")
    report_lines.append("Final Test Metrics (Held-out Test Split)")
    for r in final_test_results:
        tm = r["test_metrics"]
        report_lines.append(
            f"- rank_{r['rank']} trial={r['trial_id']} best_epoch={r['best_epoch']}: "
            f"sensitivity={tm['sensitivity']:.6f}, far={tm['far']:.6f}, f1={tm['f1']:.6f}, "
            f"macro_f1={tm['macro_f1']:.6f}, acc={tm['accuracy']:.6f}"
        )

    (out_dir / "report.txt").write_text("\n".join(report_lines), encoding="utf-8")
    print(f"Done: {out_dir}")


if __name__ == "__main__":
    main()
