#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import List, Tuple


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Parallel launcher for SIENA EEG preprocessing.")
    p.add_argument("--input-root", default="data/raw/siena", help="Root containing subject folders (e.g., PN06).")
    p.add_argument("--output-root", default="data/processed", help="Output root passed to preprocess script.")
    p.add_argument("--script-path", default="scripts/siena_eeg_preprocess.py", help="Preprocess script path.")
    p.add_argument("--python-bin", default=sys.executable, help="Python interpreter used for worker processes.")
    p.add_argument("--jobs", type=int, default=4, help="Max parallel subject jobs.")
    p.add_argument("--dry-run", action="store_true", help="Print planned commands and exit.")
    return p.parse_args()


def discover_subject_dirs(input_root: Path) -> List[Path]:
    return sorted([p for p in input_root.iterdir() if p.is_dir()])


def build_command(python_bin: str, script_path: Path, subject_dir: Path, output_root: Path) -> List[str]:
    return [
        python_bin,
        str(script_path),
        "--input-root",
        str(subject_dir),
        "--output-root",
        str(output_root),
        "--verbose",
    ]


def run_one(subject_dir: Path, cmd: List[str], output_root: Path) -> Tuple[str, int, str]:
    subject = subject_dir.name
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    subject_out_dir = output_root / "siena" / subject
    subject_out_dir.mkdir(parents=True, exist_ok=True)
    log_path = subject_out_dir / f"run_parallel_{ts}.log"
    with log_path.open("w", encoding="utf-8") as f:
        f.write(f"[CMD] {' '.join(cmd)}\n")
        f.flush()
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True)
    return subject, proc.returncode, str(log_path)


def main() -> int:
    args = parse_args()
    input_root = Path(args.input_root)
    output_root = Path(args.output_root)
    script_path = Path(args.script_path)

    if not input_root.exists() or not input_root.is_dir():
        print(f"ERROR: input root not found: {input_root}", file=sys.stderr)
        return 2
    if not script_path.exists():
        print(f"ERROR: preprocess script not found: {script_path}", file=sys.stderr)
        return 2
    if args.jobs < 1:
        print("ERROR: --jobs must be >= 1", file=sys.stderr)
        return 2

    subject_dirs = discover_subject_dirs(input_root)
    if not subject_dirs:
        print(f"ERROR: no subject dirs found under: {input_root}", file=sys.stderr)
        return 2

    print(f"Input root : {input_root}")
    print(f"Output root: {output_root}")
    print(f"Script     : {script_path}")
    print(f"Python     : {args.python_bin}")
    print(f"Jobs       : {args.jobs}")
    print(f"Subjects   : {len(subject_dirs)}")

    tasks = []
    for sdir in subject_dirs:
        cmd = build_command(args.python_bin, script_path, sdir, output_root)
        tasks.append((sdir, cmd))
        if args.dry_run:
            print("[DRY-RUN]", " ".join(cmd))

    if args.dry_run:
        return 0

    failed = []
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futures = {ex.submit(run_one, sdir, cmd, output_root): sdir.name for sdir, cmd in tasks}
        for fut in as_completed(futures):
            subject, code, log_path = fut.result()
            if code == 0:
                print(f"[DONE ] {subject} (log: {log_path})")
            else:
                print(f"[FAIL ] {subject} code={code} (log: {log_path})")
                failed.append((subject, code, log_path))

    if failed:
        print("\nFailed subjects:")
        for subject, code, log_path in failed:
            print(f"- {subject}: code={code}, log={log_path}")
        return 1

    print("All jobs finished successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
