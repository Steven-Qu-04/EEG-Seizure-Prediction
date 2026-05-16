#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import torch

from CNN_bin import EEGCNNBinary, export_binary_model
from train_common import TrainingPipelineSpec, run_nas_training_pipeline


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RNN-NAS + binary QAT BNN training pipeline for SIENA slices.")
    p.add_argument("--data-root", default="data/processed/siena_slices")
    p.add_argument("--window", default="win10s", choices=["win10s", "win20s", "win30s"])
    p.add_argument("--version", default="v1", choices=["v1", "v2"])
    p.add_argument("--output-root", default="outputs/nas_cnn_bin_runs")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--num-workers", type=int, default=0)
    p.add_argument("--epochs", type=int, default=8)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--test-ratio", type=float, default=0.2)
    p.add_argument("--nas-rounds", type=int, default=15)
    p.add_argument("--nas-samples", type=int, default=8)
    p.add_argument("--export-bin-name", default="best_{rank}_bin.pt")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def build_binary_model(kwargs, args, device: torch.device):
    return EEGCNNBinary(**kwargs).to(device)


def export_binary_artifact(model, args):
    return export_binary_model(model)


def quant_section_lines(args) -> list[str]:
    return [
        "- first_block: FP32 conv + NAS-selected activation",
        "- later_blocks: binary input, binary weight, BinaryAct",
        "- classifier: FP32 linear head",
        "- batch_norm: retained in FP32 and exported separately",
    ]


def main() -> None:
    args = parse_args()
    spec = TrainingPipelineSpec(
        model_builder=build_binary_model,
        export_artifact_fn=export_binary_artifact,
        report_title="RNN-NAS + Binary QAT BNN Training Report",
        quant_section_title="Binarization",
        quant_section_lines_fn=quant_section_lines,
        export_result_key="binary_export",
        export_template=args.export_bin_name,
    )
    run_nas_training_pipeline(args, spec)


if __name__ == "__main__":
    main()
