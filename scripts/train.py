#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import os
import platform
import random
import subprocess
import sys
import time
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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RNN-NAS + CNN training pipeline for SIENA slices.")
    p.add_argument("--data-root", default="data/processed/siena_slices")
    p.add_argument("--window", default="win10s", choices=["win10s", "win20s", "win30s"])
    p.add_argument("--version", default="v1", choices=["v1", "v2"])
    p.add_argument("--output-root", default="outputs/nas_cnn_runs")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--num-workers", type=int, default=0)
    p.add_argument("--epochs", type=int, default=8)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--test-ratio", type=float, default=0.2)
    p.add_argument("--nas-rounds", type=int, default=15)
    p.add_argument("--nas-samples", type=int, default=8)
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
    specificity = tn / max(1, tn + fp)
    sensitivity = recall
    far = far_fp_per_negative_hour(fp=fp, tn=tn, window_sec=window_sec)
    return {
        "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "accuracy": acc, "precision": precision, "recall": recall, "f1": f1,
        "specificity": specificity, "sensitivity": sensitivity, "far": far,
    }


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
def eval_epoch(model, loader, criterion, device, window_sec: int) -> Dict[str, float]:
    model.eval()
    losses = []
    ys, ps = [], []
    pbar = tqdm(loader, desc="val", leave=False)
    for x, y in pbar:
        x, y = x.to(device), y.to(device)
        logits = model(x)
        loss = criterion(logits, y)
        loss_v = float(loss.item())
        losses.append(loss_v)
        pbar.set_postfix(loss=f"{loss_v:.4f}")
        pred = torch.argmax(logits, dim=1)
        ys.append(y.cpu().numpy())
        ps.append(pred.cpu().numpy())
    if ys:
        y_true = np.concatenate(ys)
        y_pred = np.concatenate(ps)
    else:
        y_true = np.zeros((0,), dtype=np.int64)
        y_pred = np.zeros((0,), dtype=np.int64)
    m = compute_metrics(y_true, y_pred, window_sec=window_sec)
    m["loss"] = float(np.mean(losses)) if losses else 0.0
    return m


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


def rank_key(metrics: Dict[str, float]):
    return (metrics["sensitivity"], -metrics["far"])


def parse_window_sec(window: str) -> int:
    return int(window.replace("win", "").replace("s", ""))


def save_curves(train_losses: List[float], val_losses: List[float], out_dir: Path) -> None:
    plt.figure(figsize=(8, 5))
    plt.plot(train_losses, label="train_loss")
    plt.plot(val_losses, label="val_loss")
    plt.xlabel("Epoch")
    plt.ylabel("Loss")
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_dir / "loss_curve.png", dpi=150)
    plt.close()


def main() -> None:
    args = parse_args()
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

    controller = NASController(search_space=get_default_search_space()).to(device)
    criterion = nn.CrossEntropyLoss()
    window_sec = parse_window_sec(args.window)

    nas_trials = []
    best_two = []
    train_losses_global = []
    val_losses_global = []
    trial_id = 0
    t_start = datetime.now()

    for r in range(args.nas_rounds):
        archs, _, _ = controller.sample_batch(batch_size=args.nas_samples)
        for arch in archs:
            trial_id += 1
            trial = {"trial_id": trial_id, "round": r + 1, "sampled_arch": arch}
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

                train_losses_global = local_train_losses
                val_losses_global = local_val_losses
                val_m = eval_epoch(model, val_loader, criterion, device, window_sec=window_sec)
                trial["status"] = "ok"
                trial["val_metrics_for_selection"] = val_m
                nas_trials.append(trial)

                candidate = {"trial_id": trial_id, "model": model, "kwargs": kwargs, "val_metrics": val_m}
                best_two.append(candidate)
                best_two = sorted(best_two, key=lambda x: rank_key(x["val_metrics"]), reverse=True)[:2]
            except Exception as e:
                trial["status"] = "invalid"
                trial["failure_reason"] = f"{type(e).__name__}: {e}"
                nas_trials.append(trial)
                continue

    (out_dir / "nas_trials.json").write_text(json.dumps(nas_trials, indent=2), encoding="utf-8")
    save_curves(train_losses_global, val_losses_global, out_dir=out_dir)

    final_test_results = []
    for i, c in enumerate(best_two, start=1):
        model = c["model"]
        test_m = eval_epoch(model, test_loader, criterion, device, window_sec=window_sec)
        final_test_results.append({"rank": i, "trial_id": c["trial_id"], "test_metrics": test_m, "val_metrics": c["val_metrics"], "kwargs": c["kwargs"]})
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
    report_lines.append(f"- start_time: {t_start.isoformat(timespec='seconds')}")
    report_lines.append(f"- end_time: {t_end.isoformat(timespec='seconds')}")
    report_lines.append("")
    report_lines.append("Dataset Selection")
    report_lines.append(f"- window: {args.window}")
    report_lines.append(f"- version: {args.version}")
    report_lines.append(f"- split_manifest: {manifest_path}")
    report_lines.append("")
    report_lines.append("FAR Definition")
    report_lines.append("- FAR = false_positive_windows / negative_hours")
    report_lines.append("- negative_hours = (num_true_negative_windows * window_sec) / 3600")
    report_lines.append("")
    report_lines.append("NAS Selection (Permanent Validation Only)")
    for i, c in enumerate(best_two, start=1):
        vm = c["val_metrics"]
        report_lines.append(f"- rank_{i}: trial={c['trial_id']}, sensitivity={vm['sensitivity']:.6f}, far={vm['far']:.6f}, f1={vm['f1']:.6f}")
    report_lines.append("")
    report_lines.append("Final Test Metrics (Held-out Test Split)")
    for r in final_test_results:
        tm = r["test_metrics"]
        report_lines.append(f"- rank_{r['rank']} trial={r['trial_id']}: sensitivity={tm['sensitivity']:.6f}, far={tm['far']:.6f}, f1={tm['f1']:.6f}, acc={tm['accuracy']:.6f}")

    (out_dir / "report.txt").write_text("\n".join(report_lines), encoding="utf-8")
    print(f"Done: {out_dir}")


if __name__ == "__main__":
    main()
