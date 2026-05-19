#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CONDA_BIN="${CONDA_BIN:-/root/miniforge3/bin/conda}"
CONDA_ENV="${CONDA_ENV:-nuro-pt}"
DATA_ROOT="${DATA_ROOT:-/tmp/siena_data}"
WINDOW="${WINDOW:-win10s}"
VERSION="${VERSION:-v1}"
EPOCHS="${EPOCHS:-8}"
LR="${LR:-0.0001}"
REWARD="${REWARD:-macro_f1}"
CLASS_WEIGHT="${CLASS_WEIGHT:-none}"
TRAIN_SAMPLER="${TRAIN_SAMPLER:-balanced-over}"
LOG_ROOT="${LOG_ROOT:-/hy-tmp/result/fixed_resample_logs}"

RUN_TAG="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="$LOG_ROOT/$RUN_TAG"
mkdir -p "$LOG_DIR"

run_one() {
  local name="$1"
  local arch_json="$2"
  local output_root="$3"
  local log_file="$LOG_DIR/${name}.log"

  echo "[$(date '+%F %T')] START $name"
  echo "arch_json=$arch_json" | tee "$log_file"
  echo "output_root=$output_root" | tee -a "$log_file"

  "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" python scripts/train.py \
    --fixed-arch \
    --fixed-arch-json "$arch_json" \
    --data-root "$DATA_ROOT" \
    --window "$WINDOW" \
    --version "$VERSION" \
    --output-root "$output_root" \
    --epochs "$EPOCHS" \
    --lr "$LR" \
    --reward "$REWARD" \
    --class-weight "$CLASS_WEIGHT" \
    --train-sampler "$TRAIN_SAMPLER" \
    2>&1 | tee -a "$log_file"

  echo "[$(date '+%F %T')] DONE $name" | tee -a "$log_file"
}

run_one \
  "stable_trial_0004" \
  "outputs/fixed_arch_configs/trial_0004_stable_arch.json" \
  "/hy-tmp/result/fixed_resample_stable"

run_one \
  "all0_trial_0005" \
  "outputs/fixed_arch_configs/trial_0005_all0_arch.json" \
  "/hy-tmp/result/fixed_resample_all0"

run_one \
  "all1_trial_0014" \
  "outputs/fixed_arch_configs/trial_0014_all1_arch.json" \
  "/hy-tmp/result/fixed_resample_all1"

echo "All sequential fixed CNN runs finished."
echo "Logs: $LOG_DIR"
