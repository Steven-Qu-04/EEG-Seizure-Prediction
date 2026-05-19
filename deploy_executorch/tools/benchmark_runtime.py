from __future__ import annotations

import argparse
import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from deploy_executorch.tools.common import DEFAULT_OUTPUT_DIR, REPO_ROOT


DEFAULT_EXECUTABLE = REPO_ROOT / "deploy_executorch" / "build_et10b" / "infer.exe"
DEFAULT_EVIDENCE_DIR = DEFAULT_OUTPUT_DIR / "benchmark_runtime"


@dataclass(frozen=True)
class BenchmarkConfig:
    name: str
    model_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark standalone ExecuTorch runtime artifacts.")
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXECUTABLE)
    parser.add_argument("--input", type=Path, default=DEFAULT_OUTPUT_DIR / "golden_input.bin")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--repeat", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--orig-fp32", type=Path, default=DEFAULT_OUTPUT_DIR / "model_orig_fp32.pte")
    parser.add_argument("--curr-fp32", type=Path, default=DEFAULT_OUTPUT_DIR / "model_fp32.pte")
    parser.add_argument("--curr-int8", type=Path, default=DEFAULT_OUTPUT_DIR / "model_int8.pte")
    return parser.parse_args()


def benchmark_configs(args: argparse.Namespace) -> list[BenchmarkConfig]:
    return [
        BenchmarkConfig(name="orig_fp32", model_path=args.orig_fp32),
        BenchmarkConfig(name="curr_fp32", model_path=args.curr_fp32),
        BenchmarkConfig(name="curr_int8", model_path=args.curr_int8),
    ]


def parse_timing_output(stdout: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        try:
            metrics[key] = float(value)
        except ValueError:
            continue
    required = {
        "timing_repeat",
        "timing_warmup",
        "timing_total_ms",
        "timing_mean_ms",
        "timing_throughput_inf_per_s",
    }
    missing = required - metrics.keys()
    if missing:
        raise RuntimeError(f"Missing timing metrics from runtime output: {sorted(missing)}")
    return metrics


def run_single_sample(
    exe_path: Path,
    model_path: Path,
    input_path: Path,
    repeat: int,
    warmup: int,
) -> tuple[dict[str, float], str]:
    cmd = [
        str(exe_path),
        "--repeat",
        str(repeat),
        "--warmup",
        str(warmup),
        "--quiet",
        "--timing-only",
        str(model_path),
        str(input_path),
    ]
    completed = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_timing_output(completed.stdout), completed.stdout


def student_t_ci(samples: np.ndarray, confidence: float = 0.95) -> tuple[float, float]:
    mean = float(np.mean(samples))
    if len(samples) < 2:
        return mean, mean
    sem = stats.sem(samples)
    if math.isnan(sem):
        return mean, mean
    interval = stats.t.interval(confidence, df=len(samples) - 1, loc=mean, scale=sem)
    return float(interval[0]), float(interval[1])


def holm_correct(pairs: list[dict[str, float]]) -> list[dict[str, float]]:
    ordered = sorted(enumerate(pairs), key=lambda item: item[1]["p_value"])
    corrected = [0.0] * len(pairs)
    running_max = 0.0
    m = len(pairs)
    for rank, (index, pair) in enumerate(ordered):
        adjusted = min(1.0, pair["p_value"] * (m - rank))
        running_max = max(running_max, adjusted)
        corrected[index] = running_max
    output = []
    for pair, p_corrected in zip(pairs, corrected):
        row = dict(pair)
        row["p_value_holm"] = p_corrected
        output.append(row)
    return output


def summarize_samples(samples_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, float | str]] = []
    for model_name, group in samples_df.groupby("model", sort=False):
        values = group["latency_ms"].to_numpy(dtype=float)
        ci_low, ci_high = student_t_ci(values)
        mean_latency = float(np.mean(values))
        rows.append(
            {
                "model": model_name,
                "n": int(len(values)),
                "mean_latency_ms": mean_latency,
                "std_latency_ms": float(np.std(values, ddof=1)) if len(values) > 1 else 0.0,
                "ci95_low_ms": ci_low,
                "ci95_high_ms": ci_high,
                "mean_throughput_inf_per_s": 1000.0 / mean_latency,
            }
        )
    return pd.DataFrame(rows)


def pairwise_tests(samples_df: pd.DataFrame) -> pd.DataFrame:
    pivot = samples_df.pivot(index="sample", columns="model", values="latency_ms")
    pairs: list[dict[str, float | str]] = []
    model_names = list(pivot.columns)
    for idx, left in enumerate(model_names):
        for right in model_names[idx + 1 :]:
            left_values = pivot[left].to_numpy(dtype=float)
            right_values = pivot[right].to_numpy(dtype=float)
            test = stats.ttest_rel(left_values, right_values)
            slower_mean = max(float(np.mean(left_values)), float(np.mean(right_values)))
            faster_mean = min(float(np.mean(left_values)), float(np.mean(right_values)))
            faster_name = left if np.mean(left_values) <= np.mean(right_values) else right
            slower_name = right if faster_name == left else left
            pairs.append(
                {
                    "left_model": left,
                    "right_model": right,
                    "mean_diff_ms": float(np.mean(left_values - right_values)),
                    "t_statistic": float(test.statistic),
                    "p_value": float(test.pvalue),
                    "faster_model": faster_name,
                    "slower_model": slower_name,
                    "speedup_percent": ((slower_mean - faster_mean) / slower_mean) * 100.0,
                }
            )
    corrected = holm_correct(pairs)
    return pd.DataFrame(corrected)


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_log_dir = args.output_dir / "raw_logs"
    raw_log_dir.mkdir(parents=True, exist_ok=True)

    sample_rows: list[dict[str, float | int | str]] = []
    metadata = {
        "exe": str(args.exe),
        "input": str(args.input),
        "samples": args.samples,
        "repeat": args.repeat,
        "warmup": args.warmup,
        "models": {},
    }

    for config in benchmark_configs(args):
        metadata["models"][config.name] = str(config.model_path)
        for sample_idx in range(1, args.samples + 1):
            metrics, stdout = run_single_sample(
                exe_path=args.exe,
                model_path=config.model_path,
                input_path=args.input,
                repeat=args.repeat,
                warmup=args.warmup,
            )
            sample_rows.append(
                {
                    "model": config.name,
                    "sample": sample_idx,
                    "latency_ms": metrics["timing_mean_ms"],
                    "total_ms": metrics["timing_total_ms"],
                    "throughput_inf_per_s": metrics["timing_throughput_inf_per_s"],
                    "repeat": int(metrics["timing_repeat"]),
                    "warmup": int(metrics["timing_warmup"]),
                }
            )
            log_path = raw_log_dir / f"{config.name}_sample_{sample_idx}.txt"
            log_path.write_text(stdout, encoding="utf-8")

    samples_df = pd.DataFrame(sample_rows)
    summary_df = summarize_samples(samples_df)
    tests_df = pairwise_tests(samples_df)

    samples_path = args.output_dir / "benchmark_samples.csv"
    summary_path = args.output_dir / "benchmark_summary.csv"
    tests_path = args.output_dir / "benchmark_pairwise_tests.csv"
    json_path = args.output_dir / "benchmark_summary.json"

    samples_df.to_csv(samples_path, index=False)
    summary_df.to_csv(summary_path, index=False)
    tests_df.to_csv(tests_path, index=False)
    json_path.write_text(
        json.dumps(
            {
                "metadata": metadata,
                "summary": summary_df.to_dict(orient="records"),
                "pairwise_tests": tests_df.to_dict(orient="records"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"samples_csv={samples_path}")
    print(f"summary_csv={summary_path}")
    print(f"pairwise_csv={tests_path}")
    print(f"summary_json={json_path}")


if __name__ == "__main__":
    main()
