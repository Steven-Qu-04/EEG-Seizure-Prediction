from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from deploy_executorch.tools.common import DEFAULT_OUTPUT_DIR


DEFAULT_EVIDENCE_DIR = DEFAULT_OUTPUT_DIR / "benchmark_runtime"

MODEL_ORDER = ["orig_fp32", "curr_fp32", "curr_int8"]
MODEL_LABELS = {
    "orig_fp32": "Original FP32",
    "curr_fp32": "Current FP32",
    "curr_int8": "Current INT8",
}
MODEL_COLORS = {
    "orig_fp32": "#2B6CB0",
    "curr_fp32": "#C05621",
    "curr_int8": "#2F855A",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot runtime benchmark evidence.")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    return parser.parse_args()


def load_inputs(input_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    samples = pd.read_csv(input_dir / "benchmark_samples.csv")
    summary = pd.read_csv(input_dir / "benchmark_summary.csv")
    samples["model"] = pd.Categorical(samples["model"], MODEL_ORDER, ordered=True)
    summary["model"] = pd.Categorical(summary["model"], MODEL_ORDER, ordered=True)
    samples = samples.sort_values(["model", "sample"]).reset_index(drop=True)
    summary = summary.sort_values("model").reset_index(drop=True)
    return samples, summary


def style_axes(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25, linewidth=0.8)


def plot_samples(ax, samples: pd.DataFrame) -> None:
    for model_name in MODEL_ORDER:
        group = samples[samples["model"] == model_name]
        ax.plot(
            group["sample"],
            group["latency_ms"],
            marker="o",
            linewidth=2.0,
            markersize=5.5,
            color=MODEL_COLORS[model_name],
            label=MODEL_LABELS[model_name],
        )
    ax.set_xlabel("Sample index")
    ax.set_ylabel("Latency (ms / inference)")
    ax.set_title("Five benchmark samples")
    ax.set_xticks([1, 2, 3, 4, 5])
    style_axes(ax)
    ax.legend(frameon=False, fontsize=9)


def plot_ci(ax, summary: pd.DataFrame) -> None:
    x_positions = range(len(MODEL_ORDER))
    means = summary["mean_latency_ms"].to_list()
    lower_err = (summary["mean_latency_ms"] - summary["ci95_low_ms"]).to_list()
    upper_err = (summary["ci95_high_ms"] - summary["mean_latency_ms"]).to_list()

    ax.bar(
        list(x_positions),
        means,
        color=[MODEL_COLORS[name] for name in summary["model"]],
        width=0.62,
        alpha=0.88,
    )
    ax.errorbar(
        list(x_positions),
        means,
        yerr=[lower_err, upper_err],
        fmt="none",
        ecolor="#222222",
        elinewidth=1.5,
        capsize=5,
        capthick=1.5,
    )
    ax.set_xticks(list(x_positions), [MODEL_LABELS[name] for name in summary["model"]])
    ax.set_ylabel("Latency (ms / inference)")
    ax.set_title("Mean latency with 95% CI")
    style_axes(ax)


def save_combined_figure(samples: pd.DataFrame, summary: pd.DataFrame, output_dir: Path) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.6), constrained_layout=True)
    plot_samples(axes[0], samples)
    plot_ci(axes[1], summary)
    fig.suptitle("Standalone runtime benchmark on the current platform", fontsize=13, y=1.02)
    output_path = output_dir / "benchmark_runtime_combined.png"
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return output_path


def save_samples_figure(samples: pd.DataFrame, output_dir: Path) -> Path:
    fig, ax = plt.subplots(figsize=(6.3, 4.5), constrained_layout=True)
    plot_samples(ax, samples)
    output_path = output_dir / "benchmark_runtime_samples.png"
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return output_path


def save_ci_figure(summary: pd.DataFrame, output_dir: Path) -> Path:
    fig, ax = plt.subplots(figsize=(6.0, 4.5), constrained_layout=True)
    plot_ci(ax, summary)
    output_path = output_dir / "benchmark_runtime_ci.png"
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return output_path


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    samples, summary = load_inputs(args.input_dir)
    combined = save_combined_figure(samples, summary, args.output_dir)
    samples_fig = save_samples_figure(samples, args.output_dir)
    ci_fig = save_ci_figure(summary, args.output_dir)
    print(f"combined_png={combined}")
    print(f"samples_png={samples_fig}")
    print(f"ci_png={ci_fig}")


if __name__ == "__main__":
    main()
