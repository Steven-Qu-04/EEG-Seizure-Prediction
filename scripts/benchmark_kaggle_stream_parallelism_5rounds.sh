#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

INPUT_ROOT="${INPUT_ROOT:-/hy-tmp/data/raw}"
BENCH_ROOT="${BENCH_ROOT:-/hy-tmp/data/bench/kaggle_stream_parallel_5rounds}"
CONDA_BIN="${CONDA_BIN:-/root/miniforge3/bin/conda}"
CONDA_ENV="${CONDA_ENV:-nuro-pt}"
MAX_FILES="${MAX_FILES:-80}"
ROUNDS="${ROUNDS:-5}"
VERSIONS="${VERSIONS:-v1 v2 v3}"
SCRIPT="scripts/build_kaggle_windows_streaming.py"
RESULTS="$BENCH_ROOT/results.tsv"
ANALYSIS_TXT="$BENCH_ROOT/analysis.txt"

mkdir -p "$BENCH_ROOT"
printf "round\tcase\tjobs\tinner_threads\telapsed_sec\tok\tfail\twindows\n" > "$RESULTS"

run_case() {
  local round="$1"
  local name="$2"
  local jobs="$3"
  local inner="$4"
  local out="$BENCH_ROOT/round_${round}_${name}"
  local log="$BENCH_ROOT/round_${round}_${name}.log"
  local summary

  rm -rf "$out"
  mkdir -p "$out"

  echo "[$(date '+%F %T')] START round=$round case=$name jobs=$jobs inner_threads=$inner"
  OMP_NUM_THREADS="$inner" \
  MKL_NUM_THREADS="$inner" \
  OPENBLAS_NUM_THREADS="$inner" \
  NUMEXPR_NUM_THREADS="$inner" \
  "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" \
    python "$SCRIPT" \
    --input-root "$INPUT_ROOT" \
    --output-root "$out" \
    --jobs "$jobs" \
    --max-files "$MAX_FILES" \
    --versions $VERSIONS \
    --overwrite \
    --no-progress \
    --quiet \
    2>&1 | tee "$log"

  summary="$(find "$out/run_logs" -maxdepth 1 -name 'streaming_summary_*.json' -type f | sort | tail -1)"
  if [[ -z "$summary" ]]; then
    echo "ERROR: missing summary for round=$round case=$name" >&2
    return 1
  fi

  "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" python - "$summary" "$RESULTS" "$round" "$name" "$jobs" "$inner" <<'PY'
import json
import sys
from pathlib import Path

summary, results, round_i, name, jobs, inner = sys.argv[1:]
obj = json.loads(Path(summary).read_text(encoding="utf-8"))
counts = obj["counts"]
line = (
    f"{round_i}\t{name}\t{jobs}\t{inner}\t{obj['elapsed_sec']:.6f}\t"
    f"{counts['ok']}\t{counts['fail']}\t{counts['windows_all_versions']}\n"
)
Path(results).open("a", encoding="utf-8").write(line)
print(line, end="")
PY
}

for round in $(seq 1 "$ROUNDS"); do
  if (( round % 2 == 1 )); then
    run_case "$round" "40x1" 40 1
    run_case "$round" "10x4" 10 4
  else
    run_case "$round" "10x4" 10 4
    run_case "$round" "40x1" 40 1
  fi
done

"$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" python - "$RESULTS" "$ANALYSIS_TXT" <<'PY'
import csv
import itertools
import math
import statistics
import sys
from pathlib import Path

results = Path(sys.argv[1])
analysis = Path(sys.argv[2])
rows = list(csv.DictReader(results.open(encoding="utf-8"), delimiter="\t"))
by_round = {}
for row in rows:
    by_round.setdefault(int(row["round"]), {})[row["case"]] = row

pairs = []
for round_i in sorted(by_round):
    row_40 = by_round[round_i].get("40x1")
    row_10 = by_round[round_i].get("10x4")
    if not row_40 or not row_10:
        continue
    elapsed_40 = float(row_40["elapsed_sec"])
    elapsed_10 = float(row_10["elapsed_sec"])
    pairs.append((round_i, elapsed_40, elapsed_10, elapsed_10 - elapsed_40, elapsed_10 / elapsed_40))

if len(pairs) < 2:
    raise SystemExit("Need at least 2 paired rounds for significance tests.")

diffs = [p[3] for p in pairs]
n = len(diffs)
mean_diff = statistics.mean(diffs)
sd_diff = statistics.stdev(diffs)
se_diff = sd_diff / math.sqrt(n) if sd_diff > 0 else 0.0
t_stat = mean_diff / se_diff if se_diff > 0 else math.inf

observed = abs(mean_diff)
perm_means = [
    sum(sign * diff for sign, diff in zip(signs, diffs)) / n
    for signs in itertools.product((-1, 1), repeat=n)
]
perm_p = sum(abs(x) >= observed - 1e-12 for x in perm_means) / len(perm_means)

pos = sum(diff > 0 for diff in diffs)
neg = sum(diff < 0 for diff in diffs)
m = pos + neg
sign_p = min(1.0, 2 * sum(math.comb(m, i) for i in range(min(pos, neg) + 1)) / (2**m)) if m else 1.0

speedups = [p[4] for p in pairs]
lines = [
    "Kaggle streaming parallelism benchmark",
    "",
    "Hypotheses:",
    "- H0: mean elapsed(40x1) == mean elapsed(10x4)",
    "- H1: mean elapsed(40x1) != mean elapsed(10x4)",
    "- diff = elapsed(10x4) - elapsed(40x1); positive diff means 40x1 is faster.",
    "",
    f"paired_rounds: {n}",
    f"mean_elapsed_40x1_sec: {statistics.mean(p[1] for p in pairs):.6f}",
    f"mean_elapsed_10x4_sec: {statistics.mean(p[2] for p in pairs):.6f}",
    f"mean_diff_sec: {mean_diff:.6f}",
    f"sd_diff_sec: {sd_diff:.6f}",
    f"paired_t_statistic: {t_stat:.6f}",
    f"exact_sign_flip_permutation_p_two_sided: {perm_p:.6f}",
    f"sign_test_p_two_sided: {sign_p:.6f}",
    f"mean_speedup_40x1_vs_10x4: {statistics.mean(speedups):.6f}x",
    f"median_speedup_40x1_vs_10x4: {statistics.median(speedups):.6f}x",
    "",
    "Per round:",
]
for round_i, elapsed_40, elapsed_10, diff, speedup in pairs:
    lines.append(
        f"- round {round_i}: 40x1={elapsed_40:.3f}s, "
        f"10x4={elapsed_10:.3f}s, diff={diff:.3f}s, speedup={speedup:.4f}x"
    )

analysis.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(analysis.read_text(encoding="utf-8"))
PY

echo "[$(date '+%F %T')] Results: $RESULTS"
echo "[$(date '+%F %T')] Analysis: $ANALYSIS_TXT"
