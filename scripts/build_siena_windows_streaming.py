#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import mne
import numpy as np
from mne.preprocessing import ICA
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from scripts import siena_eeg_preprocess as siena_pre
    from scripts.build_siena_windows import (
        PIL_SEC,
        PREICTAL_OVERLAP_SEC,
        SPH_SEC,
        WINDOW_SECS,
        in_any_preictal,
        normalize_record_name,
        parse_siena_seizure_list,
        processed_to_original_interval,
    )
except ModuleNotFoundError:
    import siena_eeg_preprocess as siena_pre
    from build_siena_windows import (
        PIL_SEC,
        PREICTAL_OVERLAP_SEC,
        SPH_SEC,
        WINDOW_SECS,
        in_any_preictal,
        normalize_record_name,
        parse_siena_seizure_list,
        processed_to_original_interval,
    )


LAYOUT_NAME = "SPH5m_PIL30m"
VERSIONS = ("v1", "v2")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stream SIENA EDF preprocessing directly into window npy outputs.")
    p.add_argument("--input-root", default="data_image/raw/siena")
    p.add_argument("--output-root", default="data_image/processed/siena_slices")
    p.add_argument("--source-tag", default="edf_stream", help="Suffix added to output filenames, e.g. X_all_<tag>.npy.")
    p.add_argument("--versions", nargs="+", choices=VERSIONS, default=list(VERSIONS))
    p.add_argument("--segment-sec", type=int, default=siena_pre.PARAMS["segment_sec"])
    p.add_argument("--local-win-sec", type=int, default=siena_pre.PARAMS["local_win_sec"])
    p.add_argument("--local-step-sec", type=int, default=siena_pre.PARAMS["local_step_sec"])
    p.add_argument("--corr-threshold", type=float, default=siena_pre.PARAMS["corr_threshold"])
    p.add_argument("--max-segment-bad-channels", type=int, default=siena_pre.PARAMS["max_segment_bad_channels"])
    p.add_argument("--max-files", type=int, default=0)
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.add_argument("--subjects", nargs="*", default=None)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def discover_edfs(input_root: Path, subjects: Sequence[str] | None, max_files: int) -> List[Path]:
    subject_filter = set(subjects or [])
    files = []
    for path in sorted(p for p in input_root.rglob("*") if p.is_file() and p.suffix.lower() == ".edf"):
        try:
            subject = path.relative_to(input_root).parts[0]
        except ValueError:
            subject = path.parent.name
        if subject_filter and subject not in subject_filter:
            continue
        files.append(path)
    if max_files and max_files > 0:
        files = files[:max_files]
    return files


def subject_record_for_edf(edf_path: Path, input_root: Path) -> Tuple[str, str]:
    try:
        rel = edf_path.relative_to(input_root)
        subject = rel.parts[0] if len(rel.parts) > 1 else edf_path.parent.name
    except ValueError:
        subject = edf_path.parent.name
    return subject, siena_pre.sanitize_name(edf_path.stem)


def stack_or_empty(windows: List[np.ndarray], n_channels: int, win_samples: int) -> np.ndarray:
    if windows:
        return np.stack(windows, axis=0).astype(np.float32, copy=False)
    return np.zeros((0, n_channels, win_samples), dtype=np.float32)


def output_files_exist(base: Path, source_tag: str) -> bool:
    return all(
        (base / name).exists()
        for name in (
            f"X_preictal_{source_tag}.npy",
            f"X_interictal_{source_tag}.npy",
            f"X_all_{source_tag}.npy",
            f"y_all_{source_tag}.npy",
            f"meta_windows_{source_tag}.json",
        )
    )


def preprocess_edf_to_memory(edf_path: Path, args: argparse.Namespace) -> Dict:
    raw = mne.io.read_raw_edf(str(edf_path), preload=False, verbose="ERROR")
    sfreq = float(raw.info["sfreq"])
    eeg_channels = [ch for ch in raw.ch_names if siena_pre.is_eeg_channel_name(ch)]
    raw.pick(eeg_channels)
    ch_names = raw.ch_names

    seg_samples = int(args.segment_sec * sfreq)
    win_samples = int(args.local_win_sec * sfreq)
    step_samples = int(args.local_step_sec * sfreq)
    n_segments = int(raw.n_times) // seg_samples

    kept_data = []
    kept_segments: List[Dict] = []
    discarded_segments: List[Dict] = []
    line_noise_mask = []
    corr_mask = []
    imp_mask = []
    local_bad_mask = []
    segment_level_mask = []
    interpolation_mask = []

    proc_cursor_sec = 0.0
    for seg_i in range(n_segments):
        s = seg_i * seg_samples
        e = s + seg_samples
        seg_raw = raw.get_data(start=s, stop=e).astype(float, copy=False)

        line_powers = np.array([siena_pre.line_noise_power_50hz(seg_raw[c], sfreq) for c in range(seg_raw.shape[0])], dtype=float)
        thr = float(np.mean(line_powers) + siena_pre.PARAMS["line_noise_sd_threshold"] * np.std(line_powers))
        line_bad = line_powers > thr
        line_noise_mask.append(line_bad.tolist())

        seg_notched = mne.filter.notch_filter(
            seg_raw,
            Fs=sfreq,
            freqs=[siena_pre.PARAMS["line_freq"]],
            notch_widths=4.0,
            method="fir",
            phase="zero",
            verbose="ERROR",
        )
        seg_corr_aux = mne.filter.filter_data(seg_notched, sfreq=sfreq, l_freq=1.0, h_freq=45.0, method="iir", verbose="ERROR")
        corr_bad = siena_pre.max_abs_corr_per_channel(seg_corr_aux) < args.corr_threshold
        corr_mask.append(corr_bad.tolist())

        imp_bad = np.zeros(seg_notched.shape[0], dtype=bool)
        imp_mask.append(imp_bad.tolist())

        bad_w, _ = siena_pre.window_bad_mask(seg_notched, sfreq, args.local_win_sec, args.local_step_sec)
        local_bad_mask.append(bad_w.tolist())
        bad_duration_sec = np.sum(bad_w, axis=1) * args.local_step_sec
        upgraded = bad_duration_sec > siena_pre.PARAMS["local_bad_duration_upgrade_sec"]

        seg_bad = line_bad | corr_bad | imp_bad | upgraded
        segment_level_mask.append(seg_bad.tolist())
        n_bad = int(np.sum(seg_bad))
        if n_bad > args.max_segment_bad_channels:
            discarded_segments.append(
                {
                    "segment_index": seg_i,
                    "reason": "too_many_bad_channels",
                    "bad_channel_count": n_bad,
                    "original_start_sec": float(s / sfreq),
                    "original_end_sec": float(e / sfreq),
                }
            )
            interpolation_mask.append({"segment_level_channels": [], "local_windows_interpolated": {}})
            continue

        seg_bad_idx = np.where(seg_bad)[0]
        seg_interp = siena_pre.spherical_interpolate_segment(seg_notched, ch_names, sfreq, seg_bad_idx)
        seg_local_interp = siena_pre.local_window_interpolate(
            seg_interp,
            bad_w,
            win_samples=win_samples,
            step_samples=step_samples,
            exclude_channels=seg_bad_idx,
        )

        kept_data.append(seg_local_interp)
        kept_segments.append(
            {
                "segment_index": seg_i,
                "original_start_sec": float(s / sfreq),
                "original_end_sec": float(e / sfreq),
                "processed_start_sec": float(proc_cursor_sec),
                "processed_end_sec": float(proc_cursor_sec + args.segment_sec),
            }
        )
        proc_cursor_sec += args.segment_sec

        local_windows_interpolated = {}
        non_seg_bad = [i for i in range(seg_notched.shape[0]) if i not in set(seg_bad_idx.tolist())]
        for ch_i in non_seg_bad:
            wins = np.where(bad_w[ch_i])[0].tolist()
            if wins:
                local_windows_interpolated[ch_names[ch_i]] = wins
        interpolation_mask.append(
            {
                "segment_level_channels": [ch_names[i] for i in seg_bad_idx],
                "local_windows_interpolated": local_windows_interpolated,
            }
        )

    if kept_data:
        v1_data = np.concatenate(kept_data, axis=1)
    else:
        v1_data = np.zeros((len(ch_names), 0), dtype=float)

    if v1_data.shape[1] > 0:
        v1_data = mne.filter.filter_data(
            v1_data,
            sfreq=sfreq,
            l_freq=siena_pre.PARAMS["bandpass_hz"][0],
            h_freq=siena_pre.PARAMS["bandpass_hz"][1],
            method="iir",
            verbose="ERROR",
        )

    mapping = {
        "kept_segments": kept_segments,
        "discarded_segments": discarded_segments,
        "original_start_sec": 0.0,
        "original_end_sec": float(n_segments * args.segment_sec),
        "processed_start_sec": 0.0,
        "processed_end_sec": float(v1_data.shape[1] / sfreq),
    }
    return {
        "v1_data": v1_data,
        "mapping": mapping,
        "sfreq": sfreq,
        "ch_names": ch_names,
        "n_segments": n_segments,
        "line_noise_mask": line_noise_mask,
        "corr_mask": corr_mask,
        "imp_mask": imp_mask,
        "local_bad_mask": local_bad_mask,
        "segment_level_mask": segment_level_mask,
        "interpolation_mask": interpolation_mask,
    }


def apply_ica(raw_v1: mne.io.BaseRaw, ch_names: List[str]) -> Dict:
    iclabel_available = False
    fallback_rule_used = False
    v2_method = "iclabel"
    removed_components: List[int] = []
    ica_component_labels = []
    ica_removal_log = []
    raw_v2 = raw_v1.copy()

    if raw_v2.n_times > 0:
        n_comp = min(max(2, len(ch_names) - 1), len(ch_names))
        ica = ICA(n_components=n_comp, method="fastica", random_state=97, max_iter=800)
        ica.fit(raw_v2, verbose="ERROR")
        iclabel_available, iclabel_result = siena_pre.detect_iclabel(ica, raw_v2)
        if iclabel_available:
            labels = iclabel_result.get("labels", [])
            y_pred = iclabel_result.get("y_pred_proba", None)
            classes = iclabel_result.get("classes", [])
            for i, lab in enumerate(labels):
                eye_prob = 0.0
                muscle_prob = 0.0
                if isinstance(y_pred, np.ndarray) and y_pred.ndim == 2 and y_pred.shape[0] > i:
                    if "eye" in classes:
                        eye_prob = float(y_pred[i, classes.index("eye")])
                    if "muscle" in classes:
                        muscle_prob = float(y_pred[i, classes.index("muscle")])
                if eye_prob >= siena_pre.PARAMS["ica_eye_threshold"] or muscle_prob >= siena_pre.PARAMS["ica_muscle_threshold"]:
                    removed_components.append(i)
                ica_component_labels.append({"component": i, "label": str(lab), "eye_prob": eye_prob, "muscle_prob": muscle_prob})
        else:
            fallback_rule_used = True
            v2_method = "fallback_eog_emg_heuristic"
            fb = siena_pre.fallback_ica_labels(ica, raw_v2)
            for i, lab in enumerate(fb["labels"]):
                eye_prob = float(fb["probs"][i]["eye"])
                muscle_prob = float(fb["probs"][i]["muscle"])
                if eye_prob >= siena_pre.PARAMS["ica_eye_threshold"] or muscle_prob >= siena_pre.PARAMS["ica_muscle_threshold"]:
                    removed_components.append(i)
                ica_component_labels.append({"component": i, "label": str(lab), "eye_prob": eye_prob, "muscle_prob": muscle_prob})
            ica_removal_log.append("Fallback heuristic is NOT equivalent to ICLabel.")

        ica.exclude = sorted(set(removed_components))
        ica.apply(raw_v2, verbose="ERROR")
    else:
        fallback_rule_used = True
        v2_method = "fallback_eog_emg_heuristic"
        ica_removal_log.append("No samples kept after segment rejection; ICA skipped.")

    return {
        "raw_v2": raw_v2,
        "iclabel_available": iclabel_available,
        "fallback_rule_used": fallback_rule_used,
        "v2_method": v2_method,
        "removed_components": sorted(set(removed_components)),
        "ica_component_labels": ica_component_labels,
        "ica_removal_log": ica_removal_log,
    }


def seizure_starts_for_record(raw_root: Path, subject: str, record: str) -> List[float]:
    seizure_list_path = raw_root / subject / f"Seizures-list-{subject}.txt"
    if not seizure_list_path.exists():
        raise FileNotFoundError(f"missing seizure list: {seizure_list_path}")
    seizure_map = parse_siena_seizure_list(seizure_list_path)
    starts = [x["seizure_start_sec"] for x in seizure_map.get(record.upper(), [])]
    if starts:
        return starts
    for key, values in seizure_map.items():
        if normalize_record_name(key) == normalize_record_name(record):
            return [x["seizure_start_sec"] for x in values]
    return []


def build_windows_for_version(
    data: np.ndarray,
    mapping: Dict,
    sfreq: float,
    ch_names: List[str],
    seizure_starts: List[float],
    subject: str,
    record: str,
    version: str,
    source_edf: Path,
    output_root: Path,
    source_tag: str,
    overwrite: bool,
) -> List[Dict]:
    results = []
    mapping_segments = mapping.get("kept_segments", [])
    for win_sec in WINDOW_SECS:
        win_samples = int(round(win_sec * sfreq))
        step_pre = max(1, int(round((win_sec - PREICTAL_OVERLAP_SEC) * sfreq)))
        step_inter = max(1, int(round(win_sec * sfreq)))

        pre_x, inter_x = [], []
        pre_meta, inter_meta = [], []
        for s in range(0, max(0, data.shape[1] - win_samples + 1), step_pre):
            e = s + win_samples
            p_start, p_end = s / sfreq, e / sfreq
            mapped = processed_to_original_interval(mapping_segments, p_start, p_end)
            if mapped is None:
                continue
            o_start, o_end = mapped
            if in_any_preictal((o_start + o_end) / 2.0, seizure_starts):
                pre_x.append(data[:, s:e])
                pre_meta.append({"processed_start_sec": p_start, "processed_end_sec": p_end, "original_start_sec": o_start, "original_end_sec": o_end, "label": 1})

        for s in range(0, max(0, data.shape[1] - win_samples + 1), step_inter):
            e = s + win_samples
            p_start, p_end = s / sfreq, e / sfreq
            mapped = processed_to_original_interval(mapping_segments, p_start, p_end)
            if mapped is None:
                continue
            o_start, o_end = mapped
            if not in_any_preictal((o_start + o_end) / 2.0, seizure_starts):
                inter_x.append(data[:, s:e])
                inter_meta.append({"processed_start_sec": p_start, "processed_end_sec": p_end, "original_start_sec": o_start, "original_end_sec": o_end, "label": 0})

        X_pre = stack_or_empty(pre_x, data.shape[0], win_samples)
        X_inter = stack_or_empty(inter_x, data.shape[0], win_samples)
        if X_pre.shape[0] + X_inter.shape[0] > 0:
            X_all = np.concatenate([X_pre, X_inter], axis=0).astype(np.float32, copy=False)
            y_all = np.concatenate([np.ones((X_pre.shape[0],), dtype=np.int64), np.zeros((X_inter.shape[0],), dtype=np.int64)])
        else:
            X_all = np.zeros((0, data.shape[0], win_samples), dtype=np.float32)
            y_all = np.zeros((0,), dtype=np.int64)

        base = output_root / LAYOUT_NAME / f"win{win_sec}s" / subject / record / version
        if output_files_exist(base, source_tag) and not overwrite:
            results.append({"window_sec": win_sec, "version": version, "status": "skip", "reason": "already_exists", "out_dir": str(base)})
            continue
        base.mkdir(parents=True, exist_ok=True)

        np.save(base / f"X_preictal_{source_tag}.npy", X_pre.astype(np.float32, copy=False))
        np.save(base / f"X_interictal_{source_tag}.npy", X_inter.astype(np.float32, copy=False))
        np.save(base / f"X_all_{source_tag}.npy", X_all)
        np.save(base / f"y_all_{source_tag}.npy", y_all)

        meta = {
            "source": source_tag,
            "source_edf": str(source_edf),
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
            "counts": {"preictal": int(X_pre.shape[0]), "interictal": int(X_inter.shape[0]), "all": int(X_all.shape[0])},
            "windows_preictal": pre_meta,
            "windows_interictal": inter_meta,
        }
        (base / f"meta_windows_{source_tag}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        results.append({"window_sec": win_sec, "version": version, "status": "ok", "counts": meta["counts"], "out_dir": str(base)})
    return results


def process_one(edf_path: Path, args: argparse.Namespace) -> Dict:
    input_root = Path(args.input_root)
    output_root = Path(args.output_root)
    subject, record = subject_record_for_edf(edf_path, input_root)
    seizure_starts = seizure_starts_for_record(input_root, subject, record)

    pre = preprocess_edf_to_memory(edf_path, args)
    sfreq = pre["sfreq"]
    ch_names = pre["ch_names"]
    info = mne.create_info(ch_names=ch_names, sfreq=sfreq, ch_types="eeg")
    raw_v1 = mne.io.RawArray(pre["v1_data"], info, verbose="ERROR")
    raw_v1.set_montage("standard_1020", on_missing="ignore")

    version_results = {}
    if "v1" in args.versions:
        version_results["v1"] = build_windows_for_version(
            pre["v1_data"], pre["mapping"], sfreq, ch_names, seizure_starts, subject, record, "v1", edf_path, output_root, args.source_tag, args.overwrite
        )

    ica_out = None
    if "v2" in args.versions:
        ica_out = apply_ica(raw_v1, ch_names)
        v2_mapping = dict(pre["mapping"])
        v2_mapping["processed_end_sec"] = float(ica_out["raw_v2"].n_times / sfreq)
        version_results["v2"] = build_windows_for_version(
            ica_out["raw_v2"].get_data(), v2_mapping, sfreq, ch_names, seizure_starts, subject, record, "v2", edf_path, output_root, args.source_tag, args.overwrite
        )

    report = {
        "status": "ok",
        "source": args.source_tag,
        "source_edf": str(edf_path),
        "subject": subject,
        "record": record,
        "sfreq": sfreq,
        "channels": ch_names,
        "n_segments": pre["n_segments"],
        "line_noise_bad_channel_mask": pre["line_noise_mask"],
        "correlation_bad_channel_mask": pre["corr_mask"],
        "impedance_bad_channel_mask": pre["imp_mask"],
        "local_bad_window_mask": pre["local_bad_mask"],
        "segment_level_bad_channel_mask": pre["segment_level_mask"],
        "interpolation_mask": pre["interpolation_mask"],
        "discarded_segment_log": pre["mapping"]["discarded_segments"],
        "v1_output": {"streamed_to_windows": True, **pre["mapping"]},
        "window_outputs": version_results,
    }
    if ica_out is not None:
        report["v2_output"] = {
            "streamed_to_windows": True,
            "iclabel_available": ica_out["iclabel_available"],
            "fallback_rule_used": ica_out["fallback_rule_used"],
            "v2_method": ica_out["v2_method"],
            "removed_ica_components": ica_out["removed_components"],
            "ica_component_labels": ica_out["ica_component_labels"],
            "ica_removal_log": ica_out["ica_removal_log"],
        }
    return report


def count_windows(report: Dict) -> int:
    total = 0
    for rows in report.get("window_outputs", {}).values():
        for row in rows:
            total += int(row.get("counts", {}).get("all", 0))
    return total


def main() -> int:
    args = parse_args()
    input_root = Path(args.input_root)
    output_root = Path(args.output_root)
    if not input_root.exists():
        print(f"ERROR: input root not found: {input_root}", file=sys.stderr)
        return 2
    if args.jobs < 1:
        print("ERROR: --jobs must be >= 1", file=sys.stderr)
        return 2

    files = discover_edfs(input_root, args.subjects, args.max_files)
    if not files:
        print(f"ERROR: no EDF files found under {input_root}", file=sys.stderr)
        return 2

    run_logs = output_root / "run_logs"
    run_logs.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    reports_path = run_logs / f"siena_streaming_segments_{args.source_tag}_{ts}.jsonl"
    errors_path = run_logs / f"siena_streaming_errors_{args.source_tag}_{ts}.jsonl"
    summary_path = run_logs / f"siena_streaming_summary_{args.source_tag}_{ts}.json"

    if not args.quiet:
        print(f"EDF files: {len(files)}, jobs={args.jobs}, versions={','.join(args.versions)}, source_tag={args.source_tag}")

    t0 = time.time()
    ok = failed = 0
    total_windows = 0
    reports = []

    def handle_report(res: Dict) -> None:
        nonlocal ok, total_windows
        ok += 1
        total_windows += count_windows(res)
        reports.append(res)
        with reports_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")

    def handle_error(path: Path, e: Exception) -> None:
        nonlocal failed
        failed += 1
        err = {"file": str(path), "error": f"{type(e).__name__}: {e}"}
        with errors_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(err, ensure_ascii=False) + "\n")
        if args.verbose or not args.quiet:
            print(f"[FAIL] {path}: {err['error']}")

    if args.jobs == 1:
        iterator = files
        if not args.no_progress:
            iterator = tqdm(iterator, total=len(files), desc="siena-stream", unit="file")
        for path in iterator:
            try:
                handle_report(process_one(path, args))
            except Exception as e:
                handle_error(path, e)
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            futures = {ex.submit(process_one, path, args): path for path in files}
            iterator = as_completed(futures)
            if not args.no_progress:
                iterator = tqdm(iterator, total=len(futures), desc=f"siena-stream x{args.jobs}", unit="file")
            for fut in iterator:
                path = futures[fut]
                try:
                    handle_report(fut.result())
                except Exception as e:
                    handle_error(path, e)

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "script": "scripts/build_siena_windows_streaming.py",
        "source_tag": args.source_tag,
        "input_root": str(input_root),
        "output_root": str(output_root),
        "layout": LAYOUT_NAME,
        "versions": list(dict.fromkeys(args.versions)),
        "window_secs": list(WINDOW_SECS),
        "counts": {"files": len(files), "ok": ok, "fail": failed, "windows_all_versions": total_windows},
        "elapsed_sec": time.time() - t0,
        "segment_report_file": str(reports_path),
        "error_file": str(errors_path),
        "records": reports,
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if not args.quiet:
        print(f"Segment reports: {reports_path}")
        print(f"Summary: {summary_path}")
        print(f"Done. ok={ok}, fail={failed}, windows={total_windows}, elapsed={summary['elapsed_sec']:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
