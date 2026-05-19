#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

INPUT_ROOT="${INPUT_ROOT:-/hy-tmp/data/raw}"
BENCH_ROOT="${BENCH_ROOT:-/tmp/kaggle_stream_parallel_bench}"
CONDA_BIN="${CONDA_BIN:-/root/miniforge3/bin/conda}"
CONDA_ENV="${CONDA_ENV:-nuro-pt}"
MAX_FILES="${MAX_FILES:-80}"
VERSIONS="${VERSIONS:-v1 v2 v3}"
SCRIPT="scripts/build_kaggle_windows_streaming.py"

mkdir -p "$BENCH_ROOT"

run_case() {
  local name="$1"
  local jobs="$2"
  local inner_threads="$3"
  local out_dir="$BENCH_ROOT/$name"
  local log_path="$BENCH_ROOT/${name}.log"
  local summary_path
  local start
  local end
  local elapsed

  rm -rf "$out_dir"
  mkdir -p "$out_dir"

  echo "[$(date '+%F %T')] START name=$name jobs=$jobs inner_threads=$inner_threads max_files=$MAX_FILES"
  start="$(date +%s)"
  OMP_NUM_THREADS="$inner_threads" \
  MKL_NUM_THREADS="$inner_threads" \
  OPENBLAS_NUM_THREADS="$inner_threads" \
  NUMEXPR_NUM_THREADS="$inner_threads" \
  "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" \
    python "$SCRIPT" \
    --input-root "$INPUT_ROOT" \
    --output-root "$out_dir" \
    --jobs "$jobs" \
    --max-files "$MAX_FILES" \
    --versions $VERSIONS \
    --overwrite \
    --no-progress \
    2>&1 | tee "$log_path"
  end="$(date +%s)"
  elapsed="$((end - start))"
  summary_path="$(find "$out_dir/run_logs" -maxdepth 1 -name 'streaming_summary_*.json' -type f 2>/dev/null | sort | tail -1)"

  echo "[$(date '+%F %T')] END name=$name elapsed_sec=$elapsed log=$log_path summary=$summary_path"
  if [[ -n "$summary_path" ]]; then
    "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" python - "$summary_path" "$elapsed" <<'PY'
import json
import sys
from pathlib import Path

summary_path = Path(sys.argv[1])
elapsed = int(sys.argv[2])
obj = json.loads(summary_path.read_text(encoding="utf-8"))
counts = obj.get("counts", {})
ok = int(counts.get("ok", 0))
fail = int(counts.get("fail", 0))
skip = int(counts.get("skip", 0))
windows = int(counts.get("windows_all_versions", 0))
rate = ok / elapsed if elapsed else 0.0
print(
    "RESULT "
    f"summary={summary_path} "
    f"elapsed_sec={elapsed} "
    f"ok={ok} skip={skip} fail={fail} windows={windows} "
    f"ok_per_sec={rate:.4f}"
)
PY
  fi
}

run_case "40x1" 40 1
run_case "10x4" 10 4

echo "[$(date '+%F %T')] Benchmark complete. Outputs: $BENCH_ROOT"
