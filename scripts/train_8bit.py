#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import torch

from CNN_8bit import EEGCNN8bit, export_int8_model
from train_common import TrainingPipelineSpec, run_nas_training_pipeline


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RNN-NAS + 8-bit QAT CNN training pipeline for SIENA slices.")
    p.add_argument("--data-root", default="data/processed/siena_slices")
    p.add_argument("--window", default="win10s", choices=["win10s", "win20s", "win30s"])
    p.add_argument("--version", default="v1", choices=["v1", "v2"])
    p.add_argument("--output-root", default="outputs/nas_cnn_8bit_runs")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--num-workers", type=int, default=0)
    p.add_argument("--epochs", type=int, default=8)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--test-ratio", type=float, default=0.2)
    p.add_argument("--nas-rounds", type=int, default=15)
    p.add_argument("--nas-samples", type=int, default=8)
    p.add_argument("--bits", type=int, default=8)
    p.add_argument("--export-int8-name", default="best_{rank}_int8.pt")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def build_8bit_model(kwargs, args, device: torch.device):
    return EEGCNN8bit(**kwargs, bits=args.bits).to(device)


def export_8bit_artifact(model, args):
    return export_int8_model(model, bits=args.bits)


def quant_section_lines(args) -> list[str]:
    return [
        f"- bits: {args.bits}",
        "- weights: quantized during forward with STE updates to FP32 master weights",
        "- activations: quantized after each block activation and at model input",
        "- batch_norm: retained in FP32 and exported separately",
    ]


def main() -> None:
    args = parse_args()
    spec = TrainingPipelineSpec(
        model_builder=build_8bit_model,
        export_artifact_fn=export_8bit_artifact,
        report_title="RNN-NAS + 8-bit QAT CNN Training Report",
        quant_section_title="Quantization",
        quant_section_lines_fn=quant_section_lines,
        export_result_key="int8_export",
        export_template=args.export_int8_name,
    )
    run_nas_training_pipeline(args, spec)


if __name__ == "__main__":
    main()
