#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import mne
import numpy as np
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from scripts import kaggle_mat_preprocess as kaggle_pre
    from scripts import siena_eeg_preprocess as siena_pre
    from scripts.build_kaggle_windows import LAYOUT_NAME
    from scripts.build_siena_windows import PREICTAL_OVERLAP_SEC, WINDOW_SECS, processed_to_original_interval
except ModuleNotFoundError:
    import kaggle_mat_preprocess as kaggle_pre
    import siena_eeg_preprocess as siena_pre
    from build_kaggle_windows import LAYOUT_NAME
    from build_siena_windows import PREICTAL_OVERLAP_SEC, WINDOW_SECS, processed_to_original_interval


VERSION_CHOICES = ("v1", "v2", "v3")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stream Kaggle MAT preprocessing directly into window npy outputs.")
    p.add_argument("--input-root", default="/hy-tmp/data/raw")
    p.add_argument("--output-root", default="/hy-tmp/data/processed/kaggle_seizure_prediction_slices")
    p.add_argument("--skip-corrupt-list", default=None, help="Defaults to <input-root>/removed_corrupt_files.txt")
    p.add_argument("--segment-sec", type=int, default=siena_pre.PARAMS["segment_sec"])
    p.add_argument("--local-win-sec", type=int, default=siena_pre.PARAMS["local_win_sec"])
    p.add_argument("--local-step-sec", type=int, default=siena_pre.PARAMS["local_step_sec"])
    p.add_argument("--corr-threshold", type=float, default=siena_pre.PARAMS["corr_threshold"])
    p.add_argument("--max-segment-bad-channels", type=int, default=siena_pre.PARAMS["max_segment_bad_channels"])
    p.add_argument("--max-files", type=int, default=0)
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.add_argument("--subjects", nargs="*", default=None, help="Optional subject filter, e.g. Dog_1 Dog_2 Patient_1")
    p.add_argument("--versions", nargs="+", choices=VERSION_CHOICES, default=list(VERSION_CHOICES))
    p.add_argument("--input-unit", choices=["microvolts", "volts"], default="microvolts")
    p.add_argument("--skip-test", action="store_true", default=True)
    p.add_argument("--include-test", action="store_false", dest="skip_test")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def stack_or_empty(windows: List[np.ndarray], n_channels: int, win_samples: int) -> np.ndarray:
    if windows:
        return np.stack(windows, axis=0).astype(np.float32, copy=False)
    return np.zeros((0, n_channels, win_samples), dtype=np.float32)


def identity_mapping(n_times: int, sfreq: float) -> Dict:
    duration_sec = float(n_times / sfreq)
    return {
        "kept_segments": [
            {
                "segment_index": 0,
                "original_start_sec": 0.0,
                "original_end_sec": duration_sec,
                "processed_start_sec": 0.0,
                "processed_end_sec": duration_sec,
            }
        ]
        if n_times > 0
        else [],
        "discarded_segments": [],
        "original_start_sec": 0.0,
        "original_end_sec": duration_sec,
        "processed_start_sec": 0.0,
        "processed_end_sec": duration_sec,
    }


def requested_output_dirs(output_root: Path, subject: str, record: str, versions: Sequence[str]) -> List[Path]:
    return [
        output_root / LAYOUT_NAME / f"win{win_sec}s" / subject / record / version
        for win_sec in WINDOW_SECS
        for version in versions
    ]


def outputs_complete(output_root: Path, subject: str, record: str, versions: Sequence[str]) -> bool:
    required = ("X_preictal.npy", "X_interictal.npy", "X_all.npy", "y_all.npy", "meta_windows.json")
    return all(all((base / name).exists() for name in required) for base in requested_output_dirs(output_root, subject, record, versions))


def clear_outputs(output_root: Path, subject: str, record: str, versions: Sequence[str]) -> None:
    for base in requested_output_dirs(output_root, subject, record, versions):
        if base.exists():
            shutil.rmtree(base)


def build_windows_for_version(
    *,
    data: np.ndarray,
    mapping: Dict,
    sfreq: float,
    ch_names: List[str],
    seg: kaggle_pre.KaggleSegment,
    record_id: str,
    version: str,
    output_root: Path,
) -> List[Dict]:
    label = seg.label
    if label not in (0, 1):
        return []

    mapping_segments = mapping.get("kept_segments", [])
    results = []
    for win_sec in WINDOW_SECS:
        win_samples = int(round(win_sec * sfreq))
        step_sec = win_sec - PREICTAL_OVERLAP_SEC if label == 1 else win_sec
        step_samples = max(1, int(round(step_sec * sfreq)))
        windows = []
        windows_meta = []

        for s in range(0, max(0, data.shape[1] - win_samples + 1), step_samples):
            e = s + win_samples
            p_start = s / sfreq
            p_end = e / sfreq
            mapped = processed_to_original_interval(mapping_segments, p_start, p_end)
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
                    "label": label,
                }
            )

        X_label = stack_or_empty(windows, data.shape[0], win_samples)
        X_pre = X_label if label == 1 else np.zeros((0, data.shape[0], win_samples), dtype=np.float32)
        X_inter = X_label if label == 0 else np.zeros((0, data.shape[0], win_samples), dtype=np.float32)
        y_all = np.full((X_label.shape[0],), label, dtype=np.int64)

        base = output_root / LAYOUT_NAME / f"win{win_sec}s" / seg.subject / record_id / version
        base.mkdir(parents=True, exist_ok=True)
        np.save(base / "X_preictal.npy", X_pre.astype(np.float32, copy=False))
        np.save(base / "X_interictal.npy", X_inter.astype(np.float32, copy=False))
        np.save(base / "X_all.npy", X_label.astype(np.float32, copy=False))
        np.save(base / "y_all.npy", y_all)

        meta = {
            "dataset": kaggle_pre.DATASET_NAME,
            "layout": LAYOUT_NAME,
            "streaming": True,
            "subject": seg.subject,
            "record": record_id,
            "source_mat": str(seg.path),
            "segment_kind": seg.segment_kind,
            "segment_number": seg.segment_number,
            "segment_label": label,
            "version": version,
            "window_sec": win_sec,
            "sfreq": sfreq,
            "channels": ch_names,
            "preictal_overlap_sec": PREICTAL_OVERLAP_SEC if label == 1 else 0,
            "interictal_overlap_sec": 0,
            "counts": {
                "preictal": int(X_pre.shape[0]),
                "interictal": int(X_inter.shape[0]),
                "all": int(X_label.shape[0]),
            },
            "windows_preictal": windows_meta if label == 1 else [],
            "windows_interictal": windows_meta if label == 0 else [],
        }
        (base / "meta_windows.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        results.append({"window_sec": win_sec, "version": version, "status": "ok", "counts": meta["counts"], "out_dir": str(base)})
    return results


def preprocessing_report(
    *,
    seg: kaggle_pre.KaggleSegment,
    record_id: str,
    payload: kaggle_pre.MatPayload,
    processed: Dict,
    ica_out: Dict,
    v1_mapping: Dict,
    v2_mapping: Dict,
    v3_mapping: Dict,
    version_results: Dict[str, List[Dict]],
    args: argparse.Namespace,
) -> Dict:
    params_out = dict(siena_pre.PARAMS)
    params_out["corr_threshold"] = float(args.corr_threshold)
    params_out["max_segment_bad_channels"] = int(args.max_segment_bad_channels)
    return {
        "status": "ok",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "dataset": kaggle_pre.DATASET_NAME,
        "streaming": True,
        "source_mat": str(seg.path),
        "subject": seg.subject,
        "recording_id": record_id,
        "mat_variable": payload.variable_name,
        "segment_kind": seg.segment_kind,
        "segment_number": seg.segment_number,
        "label": seg.label,
        "raw": {
            "sfreq": payload.sfreq,
            "channels": payload.ch_names,
            "data_shape": list(payload.data.shape),
            "data_unit_after_load": "volts",
            "input_unit_assumption": args.input_unit,
            "data_length_sec": payload.data_length_sec,
            "sequence": payload.sequence,
        },
        "params": params_out,
        "impedance_unavailable": True,
        "line_noise_bad_channel_mask": processed["line_noise_mask"],
        "correlation_bad_channel_mask": processed["corr_mask"],
        "impedance_bad_channel_mask": processed["imp_mask"],
        "local_bad_window_mask": processed["local_bad_mask"],
        "segment_level_bad_channel_mask": processed["segment_level_mask"],
        "interpolation_mask": processed["interpolation_mask"],
        "discarded_segment_log": v1_mapping["discarded_segments"],
        "v1_output": {
            "streamed_to_windows": True,
            **v1_mapping,
        },
        "v2_output": {
            "streamed_to_windows": True,
            **v2_mapping,
            "iclabel_available": ica_out["iclabel_available"],
            "fallback_rule_used": ica_out["fallback_rule_used"],
            "v2_method": ica_out["v2_method"],
            "removed_ica_components": ica_out["removed_components"],
            "ica_component_labels": ica_out["ica_component_labels"],
            "ica_removal_log": ica_out["ica_removal_log"],
        },
        "v3_output": {
            "streamed_to_windows": True,
            "raw_no_preprocess": True,
            "duration_sec": float(payload.data.shape[1] / payload.sfreq),
            "channels": payload.ch_names,
            "label": seg.label,
            **v3_mapping,
        },
        "window_outputs": version_results,
    }


def process_one(seg: kaggle_pre.KaggleSegment, args: argparse.Namespace) -> Dict:
    output_root = Path(args.output_root)
    record_id = siena_pre.sanitize_name(seg.path.stem)
    versions = list(dict.fromkeys(args.versions))

    if seg.label not in (0, 1):
        return {"status": "skip", "reason": "unlabeled_segment", "file": str(seg.path)}
    if outputs_complete(output_root, seg.subject, record_id, versions) and not args.overwrite:
        return {
            "status": "skip",
            "reason": "already_windowed",
            "file": str(seg.path),
            "subject": seg.subject,
            "recording_id": record_id,
            "label": seg.label,
        }
    if args.overwrite:
        clear_outputs(output_root, seg.subject, record_id, versions)

    payload = kaggle_pre.load_kaggle_mat(seg.path, args.input_unit)
    sfreq = payload.sfreq
    ch_names = payload.ch_names
    info = mne.create_info(ch_names=ch_names, sfreq=sfreq, ch_types="eeg")

    processed = {}
    v1_data = None
    v1_mapping = None
    ica_out = {
        "raw_v2": None,
        "iclabel_available": False,
        "fallback_rule_used": False,
        "v2_method": "not_requested",
        "removed_components": [],
        "ica_component_labels": [],
        "ica_removal_log": [],
    }
    if "v1" in versions or "v2" in versions:
        processed = kaggle_pre.process_segment_data(payload.data, sfreq, ch_names, args)
        v1_data = processed["v1_data"]
        v1_mapping = processed["mapping"]
    else:
        processed = {
            "line_noise_mask": [],
            "corr_mask": [],
            "imp_mask": [],
            "local_bad_mask": [],
            "segment_level_mask": [],
            "interpolation_mask": [],
        }
        v1_mapping = {"kept_segments": [], "discarded_segments": [], "processed_end_sec": 0.0}

    raw_v2 = None
    v2_mapping = dict(v1_mapping)
    if "v2" in versions:
        raw_v1 = mne.io.RawArray(v1_data, info, verbose="ERROR")
        raw_v1.set_montage("standard_1020", on_missing="ignore")
        ica_out = kaggle_pre.apply_ica(raw_v1, ch_names)
        raw_v2 = ica_out["raw_v2"]
        v2_mapping["processed_end_sec"] = float(raw_v2.n_times / sfreq)

    v3_mapping = identity_mapping(payload.data.shape[1], sfreq)
    version_results: Dict[str, List[Dict]] = {}
    if "v1" in versions:
        version_results["v1"] = build_windows_for_version(
            data=v1_data,
            mapping=v1_mapping,
            sfreq=sfreq,
            ch_names=ch_names,
            seg=seg,
            record_id=record_id,
            version="v1",
            output_root=output_root,
        )
    if "v2" in versions and raw_v2 is not None:
        version_results["v2"] = build_windows_for_version(
            data=raw_v2.get_data(),
            mapping=v2_mapping,
            sfreq=sfreq,
            ch_names=ch_names,
            seg=seg,
            record_id=record_id,
            version="v2",
            output_root=output_root,
        )
    if "v3" in versions:
        version_results["v3"] = build_windows_for_version(
            data=payload.data,
            mapping=v3_mapping,
            sfreq=sfreq,
            ch_names=ch_names,
            seg=seg,
            record_id=record_id,
            version="v3",
            output_root=output_root,
        )

    return preprocessing_report(
        seg=seg,
        record_id=record_id,
        payload=payload,
        processed=processed,
        ica_out=ica_out,
        v1_mapping=v1_mapping,
        v2_mapping=v2_mapping,
        v3_mapping=v3_mapping,
        version_results=version_results,
        args=args,
    )


def count_windows(report: Dict) -> int:
    total = 0
    for version_rows in report.get("window_outputs", {}).values():
        for row in version_rows:
            total += int(row.get("counts", {}).get("all", 0))
    return total


def main() -> int:
    args = parse_args()
    input_root = Path(args.input_root)
    output_root = Path(args.output_root)
    if not input_root.exists() or not input_root.is_dir():
        print(f"ERROR: input root not found: {input_root}", file=sys.stderr)
        return 2
    if args.jobs < 1:
        print("ERROR: --jobs must be >= 1", file=sys.stderr)
        return 2

    output_root.mkdir(parents=True, exist_ok=True)
    run_logs = output_root / "run_logs"
    run_logs.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    segments_path = run_logs / f"streaming_segments_{ts}.jsonl"
    errors_path = run_logs / f"errors_{ts}.jsonl"
    summary_path = run_logs / f"streaming_summary_{ts}.json"

    segments, discovery_skipped = kaggle_pre.discover_segments(args)
    if not segments:
        print(f"ERROR: no matching MAT files found under: {input_root}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(f"Segments: {len(segments)}, jobs={args.jobs}, versions={','.join(args.versions)}")

    t0 = time.time()
    ok = skipped = failed = 0
    reports = []
    total_windows = 0

    def handle_report(res: Dict, seg: kaggle_pre.KaggleSegment) -> None:
        nonlocal ok, skipped, total_windows
        if res.get("status") == "skip":
            skipped += 1
            return
        ok += 1
        reports.append(res)
        total_windows += count_windows(res)
        with segments_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
        if args.verbose:
            print(f"[OK] {seg.path.name}")

    def handle_error(seg: kaggle_pre.KaggleSegment, e: Exception) -> None:
        nonlocal failed
        failed += 1
        err = {"file": str(seg.path), "error": f"{type(e).__name__}: {e}"}
        with errors_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(err, ensure_ascii=False) + "\n")
        if args.verbose or not args.quiet:
            print(f"[FAIL] {seg.path.name}: {err['error']}")

    if args.jobs == 1:
        iterator = segments
        if not args.no_progress:
            iterator = tqdm(iterator, total=len(segments), desc="kaggle-stream", unit="file")
        for seg in iterator:
            try:
                handle_report(process_one(seg, args), seg)
            except Exception as e:
                handle_error(seg, e)
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            futures = {ex.submit(process_one, seg, args): seg for seg in segments}
            iterator = as_completed(futures)
            if not args.no_progress:
                iterator = tqdm(iterator, total=len(futures), desc=f"kaggle-stream x{args.jobs}", unit="file")
            for fut in iterator:
                seg = futures[fut]
                try:
                    handle_report(fut.result(), seg)
                except Exception as e:
                    handle_error(seg, e)

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "script": "scripts/build_kaggle_windows_streaming.py",
        "dataset": kaggle_pre.DATASET_NAME,
        "streaming": True,
        "input_root": str(input_root),
        "output_root": str(output_root),
        "layout": LAYOUT_NAME,
        "skip_test": bool(args.skip_test),
        "subjects": args.subjects,
        "versions": list(dict.fromkeys(args.versions)),
        "window_secs": list(WINDOW_SECS),
        "preictal_overlap_sec": PREICTAL_OVERLAP_SEC,
        "jobs": int(args.jobs),
        "max_files": int(args.max_files),
        "overwrite": bool(args.overwrite),
        "counts": {
            "discovered_for_processing": len(segments),
            "discovery_skipped": len(discovery_skipped),
            "ok": ok,
            "skip": skipped,
            "fail": failed,
            "windows_all_versions": total_windows,
        },
        "discovery_skipped": discovery_skipped,
        "segment_report_file": str(segments_path),
        "error_file": str(errors_path),
        "elapsed_sec": time.time() - t0,
        "records": reports,
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if not args.quiet:
        print(f"Segment reports: {segments_path}")
        print(f"Summary: {summary_path}")
        print(f"Done. ok={ok}, skip={skipped}, fail={failed}, windows={total_windows}, elapsed={summary['elapsed_sec']:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
