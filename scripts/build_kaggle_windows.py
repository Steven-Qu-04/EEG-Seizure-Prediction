#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import mne
import numpy as np
from tqdm import tqdm

from build_siena_windows import PREICTAL_OVERLAP_SEC, WINDOW_SECS, load_mapping, processed_to_original_interval


LAYOUT_NAME = "KaggleSegmentLabels"
VERSIONS = ("v1", "v2", "v3")
VERSION_FILES = {
    "v1": ("preprocessed_eeg_v1.fif", "preprocessed_eeg_v1.mapping.json"),
    "v2": ("preprocessed_eeg_v2_ica.fif", "preprocessed_eeg_v2_ica.mapping.json"),
    "v3": ("preprocessed_eeg_v3_raw.fif", "preprocessed_eeg_v3_raw.mapping.json"),
}


@dataclass
class KaggleWindowTask:
    subject: str
    record: str
    root_dir: str
    label: int
    segment_kind: str
    segment_number: int
    source_mat: Optional[str]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build Kaggle seizure-prediction window npy datasets.")
    p.add_argument("--processed-root", default="/hy-tmp/data/processed/kaggle_seizure_prediction")
    p.add_argument("--output-root", default="/hy-tmp/data/processed/kaggle_seizure_prediction_slices")
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.add_argument("--max-records", type=int, default=0)
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


def discover_tasks(processed_root: Path) -> List[KaggleWindowTask]:
    tasks: List[KaggleWindowTask] = []
    for summary_path in sorted(processed_root.glob("*/*/latest_summary.json")):
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        label = summary.get("label")
        if label not in (0, 1):
            continue
        record_dir = summary_path.parent
        tasks.append(
            KaggleWindowTask(
                subject=str(summary.get("subject") or record_dir.parent.name),
                record=str(summary.get("recording_id") or record_dir.name),
                root_dir=str(record_dir),
                label=int(label),
                segment_kind=str(summary.get("segment_kind") or ""),
                segment_number=int(summary.get("segment_number") if summary.get("segment_number") is not None else -1),
                source_mat=summary.get("source_mat"),
            )
        )
    return tasks


def stack_or_empty(windows: List[np.ndarray], n_channels: int, win_samples: int) -> np.ndarray:
    if windows:
        return np.stack(windows, axis=0).astype(np.float32, copy=False)
    return np.zeros((0, n_channels, win_samples), dtype=np.float32)


def build_version_windows(
    task: KaggleWindowTask,
    version: str,
    fif_path: Path,
    map_path: Path,
    out_root: Path,
) -> Dict:
    raw = mne.io.read_raw_fif(str(fif_path), preload=True, verbose="ERROR")
    sfreq = float(raw.info["sfreq"])
    data = raw.get_data()
    ch_names = raw.ch_names
    mapping = load_mapping(map_path)

    results = []
    for win_sec in WINDOW_SECS:
        win_samples = int(round(win_sec * sfreq))
        step_sec = win_sec - PREICTAL_OVERLAP_SEC if task.label == 1 else win_sec
        step_samples = int(round(step_sec * sfreq))
        windows = []
        windows_meta = []

        for s in range(0, max(0, data.shape[1] - win_samples + 1), max(1, step_samples)):
            e = s + win_samples
            p_start = s / sfreq
            p_end = e / sfreq
            mapped = processed_to_original_interval(mapping, p_start, p_end)
            if mapped is None:
                continue
            o_start, o_end = mapped
            windows.append(data[:, s:e])
            windows_meta.append(
                {
                    "processed_start_sec": p_start,
                    "processed_end_sec": p_end,
                    "original_start_sec": o_start,
                    "original_end_sec": o_end,
                    "label": task.label,
                }
            )

        X_label = stack_or_empty(windows, data.shape[0], win_samples)
        X_pre = X_label if task.label == 1 else np.zeros((0, data.shape[0], win_samples), dtype=np.float32)
        X_inter = X_label if task.label == 0 else np.zeros((0, data.shape[0], win_samples), dtype=np.float32)
        y_all = np.full((X_label.shape[0],), task.label, dtype=np.int64)

        base = out_root / LAYOUT_NAME / f"win{win_sec}s" / task.subject / task.record / version
        base.mkdir(parents=True, exist_ok=True)
        np.save(base / "X_preictal.npy", X_pre.astype(np.float32, copy=False))
        np.save(base / "X_interictal.npy", X_inter.astype(np.float32, copy=False))
        np.save(base / "X_all.npy", X_label.astype(np.float32, copy=False))
        np.save(base / "y_all.npy", y_all)

        meta = {
            "dataset": "kaggle_seizure_prediction",
            "layout": LAYOUT_NAME,
            "subject": task.subject,
            "record": task.record,
            "source_mat": task.source_mat,
            "segment_kind": task.segment_kind,
            "segment_number": task.segment_number,
            "segment_label": task.label,
            "version": version,
            "window_sec": win_sec,
            "sfreq": sfreq,
            "channels": ch_names,
            "preictal_overlap_sec": PREICTAL_OVERLAP_SEC if task.label == 1 else 0,
            "interictal_overlap_sec": 0,
            "counts": {
                "preictal": int(X_pre.shape[0]),
                "interictal": int(X_inter.shape[0]),
                "all": int(X_label.shape[0]),
            },
            "windows_preictal": windows_meta if task.label == 1 else [],
            "windows_interictal": windows_meta if task.label == 0 else [],
        }
        (base / "meta_windows.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        results.append({"window_sec": win_sec, "version": version, "status": "ok", "counts": meta["counts"], "out_dir": str(base)})
    return {"status": "ok", "version": version, "results": results}


def build_windows_for_record(task: KaggleWindowTask, out_root: Path, quiet: bool) -> Dict:
    record_dir = Path(task.root_dir)
    results = []
    for version in VERSIONS:
        fif_name, map_name = VERSION_FILES[version]
        fif_path = record_dir / fif_name
        map_path = record_dir / map_name
        if not fif_path.exists() or not map_path.exists():
            results.append({"version": version, "status": "skip", "reason": "missing fif/mapping"})
            continue
        results.append(build_version_windows(task, version, fif_path, map_path, out_root))
    if not quiet:
        print(f"[DONE ] {task.subject}/{task.record}")
    return {"status": "ok", "subject": task.subject, "record": task.record, "label": task.label, "results": results}


def main() -> None:
    args = parse_args()
    processed_root = Path(args.processed_root)
    output_root = Path(args.output_root)
    run_logs = output_root / "run_logs"
    run_logs.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = run_logs / f"build_kaggle_windows_{ts}.log"
    err_path = run_logs / f"errors_{ts}.jsonl"
    summary_path = run_logs / f"build_summary_{ts}.json"

    tasks = discover_tasks(processed_root)
    if args.max_records and args.max_records > 0:
        tasks = tasks[: args.max_records]
    if not tasks:
        raise RuntimeError("No valid labeled Kaggle tasks found under processed root.")

    if not args.quiet:
        print(f"Tasks: {len(tasks)}, jobs={args.jobs}")

    t0 = time.time()
    summary_rows = []
    n_ok = n_fail = 0
    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as ex, log_path.open("w", encoding="utf-8") as lf:
        futures = {ex.submit(build_windows_for_record, task, output_root, args.quiet): task for task in tasks}
        iterator = as_completed(futures)
        if not args.no_progress:
            iterator = tqdm(iterator, total=len(futures), desc="kaggle-records")

        for fut in iterator:
            task = futures[fut]
            try:
                res = fut.result()
                n_ok += 1
                summary_rows.append(res)
                lf.write(json.dumps(res, ensure_ascii=False) + "\n")
            except Exception as e:
                n_fail += 1
                err = {"subject": task.subject, "record": task.record, "error": f"{type(e).__name__}: {e}"}
                with err_path.open("a", encoding="utf-8") as ef:
                    ef.write(json.dumps(err, ensure_ascii=False) + "\n")
                if not args.quiet:
                    print(f"[FAIL ] {task.subject}/{task.record}: {e}")

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "script": "scripts/build_kaggle_windows.py",
        "processed_root": str(processed_root),
        "output_root": str(output_root),
        "layout": LAYOUT_NAME,
        "config": {
            "window_secs": list(WINDOW_SECS),
            "versions": list(VERSIONS),
            "preictal_overlap_sec": PREICTAL_OVERLAP_SEC,
            "interictal_overlap_sec": 0,
        },
        "tasks_total": len(tasks),
        "ok": n_ok,
        "fail": n_fail,
        "elapsed_sec": time.time() - t0,
        "log_file": str(log_path),
        "error_file": str(err_path),
        "records": summary_rows,
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if not args.quiet:
        print(f"Summary: {summary_path}")
        print(f"Done. ok={n_ok}, fail={n_fail}, elapsed={summary['elapsed_sec']:.1f}s")


if __name__ == "__main__":
    main()
