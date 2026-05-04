#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import mne
import numpy as np
from tqdm import tqdm


SPH_SEC = 300
PIL_SEC = 1800
WINDOW_SECS = (10, 20, 30)
PREICTAL_OVERLAP_SEC = 5


@dataclass
class RecordTask:
    subject: str
    record: str
    root_dir: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build SIENA sliced npy datasets with SPH/PIL labeling.")
    p.add_argument("--processed-root", default="data/processed/siena")
    p.add_argument("--raw-root", default="data/raw/siena")
    p.add_argument("--output-root", default="data/processed/siena_slices")
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.add_argument("--max-records", type=int, default=0)
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


def normalize_record_name(name: str) -> str:
    s = name.strip().upper().replace("PNO", "PN0")
    m = re.search(r"(PN\d+)[-_]?(\d+(?:\.\d+)*)", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    s = re.sub(r"\.EDF$", "", s)
    return s


def parse_hms_to_sec(hms: str) -> Optional[int]:
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{1,2})", hms.replace(" ", ""))
    if not m:
        return None
    hh, mm, ss = map(int, m.groups())
    return hh * 3600 + mm * 60 + ss


def parse_siena_seizure_list(path: Path) -> Dict[str, List[Dict[str, float]]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\s*\n", text)
    out: Dict[str, List[Dict[str, float]]] = {}
    for b in blocks:
        if "File name" not in b or "Seizure start time" not in b:
            continue
        fn = re.search(r"File name:\s*([^\n\r]+)", b, flags=re.I)
        rs = re.search(r"Registration start time:\s*([^\n\r]+)", b, flags=re.I)
        ss = re.search(r"Seizure start time:\s*([^\n\r]+)", b, flags=re.I)
        se = re.search(r"Seizure end time:\s*([^\n\r]+)", b, flags=re.I)
        if not (fn and rs and ss):
            continue
        rec = normalize_record_name(fn.group(1))
        reg_s = parse_hms_to_sec(rs.group(1))
        sei_s = parse_hms_to_sec(ss.group(1))
        sei_e = parse_hms_to_sec(se.group(1)) if se else None
        if reg_s is None or sei_s is None:
            continue
        dt = sei_s - reg_s
        if dt < 0:
            dt += 24 * 3600
        end_dt = None
        if sei_e is not None:
            end_dt = sei_e - reg_s
            if end_dt < 0:
                end_dt += 24 * 3600
        out.setdefault(rec, []).append(
            {
                "seizure_start_sec": float(dt),
                "seizure_end_sec": float(end_dt) if end_dt is not None else None,
            }
        )
    return out


def discover_tasks(processed_root: Path) -> List[RecordTask]:
    tasks: List[RecordTask] = []
    for subject_dir in sorted(processed_root.iterdir()):
        if not subject_dir.is_dir() or subject_dir.name.startswith("_"):
            continue
        for record_dir in sorted(subject_dir.iterdir()):
            if not record_dir.is_dir():
                continue
            if (record_dir / "preprocessed_eeg_v1.fif").exists() and (record_dir / "preprocessed_eeg_v1.mapping.json").exists():
                tasks.append(RecordTask(subject=subject_dir.name, record=record_dir.name, root_dir=str(record_dir)))
    return tasks


def load_mapping(mapping_path: Path) -> List[Dict]:
    obj = json.loads(mapping_path.read_text(encoding="utf-8"))
    return obj.get("kept_segments", [])


def processed_to_original_interval(mapping: List[Dict], p_start: float, p_end: float) -> Optional[Tuple[float, float]]:
    # Keep only windows fully inside one kept segment for precise linear map
    for seg in mapping:
        ps = float(seg["processed_start_sec"])
        pe = float(seg["processed_end_sec"])
        if p_start >= ps and p_end <= pe:
            os = float(seg["original_start_sec"])
            oe = float(seg["original_end_sec"])
            frac_s = (p_start - ps) / max(1e-9, (pe - ps))
            frac_e = (p_end - ps) / max(1e-9, (pe - ps))
            o_start = os + frac_s * (oe - os)
            o_end = os + frac_e * (oe - os)
            return o_start, o_end
    return None


def in_any_preictal(t_center: float, seizure_starts: List[float]) -> bool:
    for sz in seizure_starts:
        left = sz - SPH_SEC - PIL_SEC
        right = sz - SPH_SEC
        if left <= t_center < right:
            return True
    return False


def build_windows_for_record(
    task: RecordTask,
    raw_root: Path,
    out_root: Path,
    quiet: bool,
) -> Dict:
    record_dir = Path(task.root_dir)
    subject = task.subject
    record = task.record

    v_paths = {
        "v1": (
            record_dir / "preprocessed_eeg_v1.fif",
            record_dir / "preprocessed_eeg_v1.mapping.json",
        ),
        "v2": (
            record_dir / "preprocessed_eeg_v2_ica.fif",
            record_dir / "preprocessed_eeg_v2_ica.mapping.json",
        ),
    }

    seizure_list_path = raw_root / subject / f"Seizures-list-{subject}.txt"
    if not seizure_list_path.exists():
        return {"status": "fail", "subject": subject, "record": record, "error": f"missing seizure list: {seizure_list_path}"}

    seizure_map = parse_siena_seizure_list(seizure_list_path)
    seizure_starts = [x["seizure_start_sec"] for x in seizure_map.get(record.upper(), [])]
    if not seizure_starts:
        # fallback by normalization attempt
        for k, v in seizure_map.items():
            if normalize_record_name(k) == normalize_record_name(record):
                seizure_starts = [x["seizure_start_sec"] for x in v]
                break

    results = []
    for win_sec in WINDOW_SECS:
        for version, (fif_path, map_path) in v_paths.items():
            if not fif_path.exists() or not map_path.exists():
                results.append({"window_sec": win_sec, "version": version, "status": "skip", "reason": "missing fif/mapping"})
                continue

            raw = mne.io.read_raw_fif(str(fif_path), preload=True, verbose="ERROR")
            sfreq = float(raw.info["sfreq"])
            data = raw.get_data()
            ch_names = raw.ch_names
            mapping = load_mapping(map_path)

            win_samples = int(round(win_sec * sfreq))
            step_pre = int(round((win_sec - PREICTAL_OVERLAP_SEC) * sfreq))
            step_inter = int(round(win_sec * sfreq))
            n_times = data.shape[1]

            pre_X = []
            inter_X = []
            pre_meta = []
            inter_meta = []

            # preictal sampling
            for s in range(0, max(0, n_times - win_samples + 1), max(1, step_pre)):
                e = s + win_samples
                p_start = s / sfreq
                p_end = e / sfreq
                mapped = processed_to_original_interval(mapping, p_start, p_end)
                if mapped is None:
                    continue
                o_start, o_end = mapped
                center = (o_start + o_end) / 2.0
                if in_any_preictal(center, seizure_starts):
                    pre_X.append(data[:, s:e])
                    pre_meta.append(
                        {
                            "processed_start_sec": p_start,
                            "processed_end_sec": p_end,
                            "original_start_sec": o_start,
                            "original_end_sec": o_end,
                            "label": 1,
                        }
                    )

            # interictal sampling
            for s in range(0, max(0, n_times - win_samples + 1), max(1, step_inter)):
                e = s + win_samples
                p_start = s / sfreq
                p_end = e / sfreq
                mapped = processed_to_original_interval(mapping, p_start, p_end)
                if mapped is None:
                    continue
                o_start, o_end = mapped
                center = (o_start + o_end) / 2.0
                if not in_any_preictal(center, seizure_starts):
                    inter_X.append(data[:, s:e])
                    inter_meta.append(
                        {
                            "processed_start_sec": p_start,
                            "processed_end_sec": p_end,
                            "original_start_sec": o_start,
                            "original_end_sec": o_end,
                            "label": 0,
                        }
                    )

            if pre_X:
                X_pre = np.stack(pre_X, axis=0)
            else:
                X_pre = np.zeros((0, data.shape[0], win_samples), dtype=np.float32)
            if inter_X:
                X_inter = np.stack(inter_X, axis=0)
            else:
                X_inter = np.zeros((0, data.shape[0], win_samples), dtype=np.float32)

            X_all = np.concatenate([X_pre, X_inter], axis=0) if (X_pre.shape[0] + X_inter.shape[0]) > 0 else np.zeros((0, data.shape[0], win_samples), dtype=np.float32)
            y_all = np.concatenate(
                [
                    np.ones((X_pre.shape[0],), dtype=np.int64),
                    np.zeros((X_inter.shape[0],), dtype=np.int64),
                ],
                axis=0,
            ) if X_all.shape[0] > 0 else np.zeros((0,), dtype=np.int64)

            base = out_root / "SPH5m_PIL30m" / f"win{win_sec}s" / subject / record / version
            base.mkdir(parents=True, exist_ok=True)
            np.save(base / "X_preictal.npy", X_pre.astype(np.float32, copy=False))
            np.save(base / "X_interictal.npy", X_inter.astype(np.float32, copy=False))
            np.save(base / "X_all.npy", X_all.astype(np.float32, copy=False))
            np.save(base / "y_all.npy", y_all)

            meta = {
                "subject": subject,
                "record": record,
                "version": version,
                "window_sec": win_sec,
                "sfreq": sfreq,
                "channels": ch_names,
                "sph_sec": SPH_SEC,
                "pil_sec": PIL_SEC,
                "preictal_overlap_sec": PREICTAL_OVERLAP_SEC,
                "interictal_overlap_sec": 0,
                "counts": {
                    "preictal": int(X_pre.shape[0]),
                    "interictal": int(X_inter.shape[0]),
                    "all": int(X_all.shape[0]),
                },
                "windows_preictal": pre_meta,
                "windows_interictal": inter_meta,
            }
            (base / "meta_windows.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

            results.append(
                {
                    "window_sec": win_sec,
                    "version": version,
                    "status": "ok",
                    "counts": meta["counts"],
                    "out_dir": str(base),
                }
            )

    if not quiet:
        print(f"[DONE ] {subject}/{record}")
    return {"status": "ok", "subject": subject, "record": record, "results": results}


def main() -> None:
    args = parse_args()
    processed_root = Path(args.processed_root)
    raw_root = Path(args.raw_root)
    output_root = Path(args.output_root)
    run_logs = output_root / "run_logs"
    run_logs.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = run_logs / f"build_siena_windows_{ts}.log"
    err_path = run_logs / f"errors_{ts}.jsonl"
    summary_path = run_logs / f"build_summary_{ts}.json"

    tasks = discover_tasks(processed_root)
    if args.max_records and args.max_records > 0:
        tasks = tasks[: args.max_records]
    if not tasks:
        raise RuntimeError("No valid record tasks found under processed root.")

    if not args.quiet:
        print(f"Tasks: {len(tasks)}, jobs={args.jobs}")

    t0 = time.time()
    summary_rows = []
    n_ok = n_fail = n_skip = 0

    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as ex, log_path.open("w", encoding="utf-8") as lf:
        futures = {}
        for t in tasks:
            if not args.quiet:
                print(f"[START] {t.subject}/{t.record}")
            fut = ex.submit(build_windows_for_record, t, raw_root, output_root, args.quiet)
            futures[fut] = t

        iterator = as_completed(futures)
        if not args.no_progress:
            iterator = tqdm(iterator, total=len(futures), desc="records")

        for fut in iterator:
            task = futures[fut]
            try:
                res = fut.result()
                summary_rows.append(res)
                lf.write(json.dumps(res, ensure_ascii=False) + "\n")
                if res.get("status") == "ok":
                    n_ok += 1
                    if not args.quiet:
                        print(f"[DONE ] {task.subject}/{task.record}")
                elif res.get("status") == "skip":
                    n_skip += 1
                    if not args.quiet:
                        print(f"[SKIP ] {task.subject}/{task.record}")
                else:
                    n_fail += 1
                    err = {"subject": task.subject, "record": task.record, "error": res.get("error", "unknown")}
                    with err_path.open("a", encoding="utf-8") as ef:
                        ef.write(json.dumps(err, ensure_ascii=False) + "\n")
                    if not args.quiet:
                        print(f"[FAIL ] {task.subject}/{task.record}: {err['error']}")
            except Exception as e:
                n_fail += 1
                err = {"subject": task.subject, "record": task.record, "error": f"{type(e).__name__}: {e}"}
                with err_path.open("a", encoding="utf-8") as ef:
                    ef.write(json.dumps(err, ensure_ascii=False) + "\n")
                if not args.quiet:
                    print(f"[FAIL ] {task.subject}/{task.record}: {e}")

    elapsed = time.time() - t0
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "processed_root": str(processed_root),
        "raw_root": str(raw_root),
        "output_root": str(output_root),
        "config": {
            "sph_sec": SPH_SEC,
            "pil_sec": PIL_SEC,
            "window_secs": list(WINDOW_SECS),
            "versions": ["v1", "v2"],
            "preictal_overlap_sec": PREICTAL_OVERLAP_SEC,
            "interictal_overlap_sec": 0,
        },
        "tasks_total": len(tasks),
        "ok": n_ok,
        "skip": n_skip,
        "fail": n_fail,
        "elapsed_sec": elapsed,
        "log_file": str(log_path),
        "error_file": str(err_path),
        "records": summary_rows,
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    if not args.quiet:
        print(f"Summary: {summary_path}")
        print(f"Done. ok={n_ok}, skip={n_skip}, fail={n_fail}, elapsed={elapsed:.1f}s")


if __name__ == "__main__":
    main()
