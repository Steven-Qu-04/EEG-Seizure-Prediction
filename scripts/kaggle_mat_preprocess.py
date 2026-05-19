#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


try:
    import mne
    import numpy as np
    from mne.preprocessing import ICA
    from scipy.io import loadmat
    from tqdm import tqdm
except ModuleNotFoundError as e:
    missing = e.name or "unknown"
    raise SystemExit(
        f"Missing dependency: {missing}. Run this script in the environment that has "
        "mne, numpy, and scipy installed."
    ) from e


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from scripts import siena_eeg_preprocess as siena_pre
except ModuleNotFoundError:
    import siena_eeg_preprocess as siena_pre


DATASET_NAME = "kaggle_seizure_prediction"


@dataclass
class KaggleSegment:
    path: Path
    subject: str
    segment_kind: str
    segment_number: int
    label: Optional[int]


@dataclass
class MatPayload:
    variable_name: str
    data: np.ndarray
    sfreq: float
    ch_names: List[str]
    data_length_sec: Optional[float]
    sequence: Optional[int]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Preprocess Kaggle seizure-prediction MAT segments using SIENA preprocessing primitives.")
    p.add_argument("--input-root", default="/hy-tmp/seizure-prediction")
    p.add_argument("--output-root", default="/hy-tmp/kaggle_seizure_prediction_processed")
    p.add_argument("--skip-corrupt-list", default=None, help="Defaults to <input-root>/removed_corrupt_files.txt")
    p.add_argument("--segment-sec", type=int, default=siena_pre.PARAMS["segment_sec"])
    p.add_argument("--local-win-sec", type=int, default=siena_pre.PARAMS["local_win_sec"])
    p.add_argument("--local-step-sec", type=int, default=siena_pre.PARAMS["local_step_sec"])
    p.add_argument("--corr-threshold", type=float, default=siena_pre.PARAMS["corr_threshold"])
    p.add_argument("--max-segment-bad-channels", type=int, default=siena_pre.PARAMS["max_segment_bad_channels"])
    p.add_argument("--max-files", type=int, default=0)
    p.add_argument("--jobs", type=int, default=1, help="Parallel worker processes. Use 1 for sequential processing.")
    p.add_argument("--subjects", nargs="*", default=None, help="Optional subject filter, e.g. Dog_1 Dog_2 Patient_1")
    p.add_argument("--input-unit", choices=["microvolts", "volts"], default="microvolts")
    p.add_argument("--skip-test", action="store_true", default=True, help="Skip *_test_segment_*.mat files. Enabled by default.")
    p.add_argument("--include-test", action="store_false", dest="skip_test", help="Include test segments with label=null.")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def infer_segment(path: Path, input_root: Path) -> Optional[KaggleSegment]:
    stem = path.stem
    subject = path.parts[-3] if len(path.parts) >= 3 else path.parent.name
    prefix = f"{subject}_"
    if not stem.startswith(prefix):
        subject = path.parent.name

    kind = None
    label: Optional[int] = None
    for candidate, candidate_label in (("preictal", 1), ("interictal", 0), ("test", None)):
        marker = f"_{candidate}_segment_"
        if marker in stem:
            kind = candidate
            label = candidate_label
            break
    if kind is None:
        return None

    try:
        seg_no = int(stem.rsplit("_", 1)[1])
    except ValueError:
        seg_no = -1

    try:
        rel_subject = path.relative_to(input_root).parts[0]
        if rel_subject:
            subject = rel_subject
    except ValueError:
        pass

    return KaggleSegment(path=path, subject=subject, segment_kind=kind, segment_number=seg_no, label=label)


def load_corrupt_relpaths(input_root: Path, corrupt_list: Optional[Path]) -> set:
    list_path = corrupt_list or (input_root / "removed_corrupt_files.txt")
    if not list_path.exists():
        return set()
    out = set()
    for line in list_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        out.add(line)
        out.add(str(Path(line)))
    return out


def discover_segments(args: argparse.Namespace) -> Tuple[List[KaggleSegment], List[Dict]]:
    input_root = Path(args.input_root)
    corrupt = load_corrupt_relpaths(input_root, Path(args.skip_corrupt_list) if args.skip_corrupt_list else None)
    subjects = set(args.subjects or [])
    segments: List[KaggleSegment] = []
    skipped: List[Dict] = []

    for path in sorted(input_root.rglob("*.mat")):
        rel = str(path.relative_to(input_root))
        if rel in corrupt:
            skipped.append({"file": str(path), "reason": "listed_corrupt"})
            continue
        seg = infer_segment(path, input_root)
        if seg is None:
            skipped.append({"file": str(path), "reason": "unrecognized_name"})
            continue
        if subjects and seg.subject not in subjects:
            continue
        if args.skip_test and seg.segment_kind == "test":
            skipped.append({"file": str(path), "reason": "test_segment_skipped"})
            continue
        segments.append(seg)

    if args.max_files and args.max_files > 0:
        segments = segments[: args.max_files]
    return segments, skipped


def matlab_string(value) -> str:
    arr = np.asarray(value)
    if arr.dtype.kind in {"U", "S"}:
        return "".join(arr.astype(str).ravel().tolist())
    if arr.dtype.kind in {"i", "u"}:
        return "".join(chr(int(x)) for x in arr.ravel() if int(x) != 0)
    return str(value)


def normalize_channels(channels, n_channels: int) -> List[str]:
    arr = np.asarray(channels, dtype=object).ravel()
    names = []
    for item in arr:
        names.append(matlab_string(item).strip())
    if len(names) != n_channels or any(not n for n in names):
        names = [f"Ch{i + 1:03d}" for i in range(n_channels)]
    return names


def struct_fields(obj) -> Sequence[str]:
    if hasattr(obj, "_fieldnames") and obj._fieldnames:
        return obj._fieldnames
    return []


def find_segment_struct(mat_obj: Dict) -> Tuple[str, object]:
    candidates = []
    for key, value in mat_obj.items():
        if key.startswith("__"):
            continue
        fields = set(struct_fields(value))
        if {"data", "sampling_frequency", "channels"}.issubset(fields):
            candidates.append((key, value))
    if not candidates:
        raise ValueError("No segment struct with data/sampling_frequency/channels fields found")
    if len(candidates) > 1:
        candidates.sort(key=lambda kv: kv[0])
    return candidates[0]


def scalar_float(value) -> float:
    arr = np.asarray(value)
    if arr.size == 0:
        raise ValueError("empty scalar")
    return float(arr.reshape(-1)[0])


def scalar_int_or_none(value) -> Optional[int]:
    try:
        arr = np.asarray(value)
        if arr.size == 0:
            return None
        return int(arr.reshape(-1)[0])
    except Exception:
        return None


def load_kaggle_mat(path: Path, input_unit: str) -> MatPayload:
    mat = loadmat(str(path), squeeze_me=True, struct_as_record=False)
    var_name, obj = find_segment_struct(mat)
    data = np.asarray(getattr(obj, "data"), dtype=float)
    if data.ndim != 2:
        raise ValueError(f"expected 2D data matrix, got shape {data.shape}")

    sfreq = scalar_float(getattr(obj, "sampling_frequency"))
    n_expected = int(round(sfreq * scalar_float(getattr(obj, "data_length_sec", 0)))) if hasattr(obj, "data_length_sec") else 0
    if data.shape[0] > data.shape[1] and (not n_expected or abs(data.shape[0] - n_expected) < abs(data.shape[1] - n_expected)):
        data = data.T
    if data.shape[0] > data.shape[1]:
        data = data.T
    if input_unit == "microvolts":
        data = data * 1e-6

    ch_names = normalize_channels(getattr(obj, "channels"), data.shape[0])
    data_length_sec = scalar_float(getattr(obj, "data_length_sec")) if hasattr(obj, "data_length_sec") else None
    sequence = scalar_int_or_none(getattr(obj, "sequence")) if hasattr(obj, "sequence") else None
    return MatPayload(
        variable_name=var_name,
        data=data,
        sfreq=sfreq,
        ch_names=ch_names,
        data_length_sec=data_length_sec,
        sequence=sequence,
    )


def process_segment_data(data: np.ndarray, sfreq: float, ch_names: List[str], args: argparse.Namespace) -> Dict:
    seg_samples = int(args.segment_sec * sfreq)
    win_samples = int(args.local_win_sec * sfreq)
    step_samples = int(args.local_step_sec * sfreq)
    n_segments = int(data.shape[1]) // seg_samples

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
        seg_raw = data[:, s:e].astype(float, copy=False)

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

        seg_corr_aux = mne.filter.filter_data(
            seg_notched,
            sfreq=sfreq,
            l_freq=1.0,
            h_freq=45.0,
            method="iir",
            verbose="ERROR",
        )
        corr_vals = siena_pre.max_abs_corr_per_channel(seg_corr_aux)
        corr_bad = corr_vals < args.corr_threshold
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
        "line_noise_mask": line_noise_mask,
        "corr_mask": corr_mask,
        "imp_mask": imp_mask,
        "local_bad_mask": local_bad_mask,
        "segment_level_mask": segment_level_mask,
        "interpolation_mask": interpolation_mask,
        "n_segments": n_segments,
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
        try:
            ica = ICA(n_components=n_comp, method="fastica", random_state=97, max_iter=800)
            ica.fit(raw_v2, verbose="ERROR")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if "sklearn" not in msg.lower() and "scikit" not in msg.lower():
                raise
            fallback_rule_used = True
            v2_method = "ica_skipped_missing_scikit_learn"
            ica_removal_log.append(f"FastICA skipped because scikit-learn is unavailable: {msg}")
            return {
                "raw_v2": raw_v2,
                "iclabel_available": iclabel_available,
                "fallback_rule_used": fallback_rule_used,
                "v2_method": v2_method,
                "removed_components": [],
                "ica_component_labels": [],
                "ica_removal_log": ica_removal_log,
            }
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
                remove = eye_prob >= siena_pre.PARAMS["ica_eye_threshold"] or muscle_prob >= siena_pre.PARAMS["ica_muscle_threshold"]
                if remove:
                    removed_components.append(i)
                ica_component_labels.append({"component": i, "label": str(lab), "eye_prob": eye_prob, "muscle_prob": muscle_prob})
        else:
            fallback_rule_used = True
            v2_method = "fallback_eog_emg_heuristic"
            fb = siena_pre.fallback_ica_labels(ica, raw_v2)
            for i, lab in enumerate(fb["labels"]):
                eye_prob = float(fb["probs"][i]["eye"])
                muscle_prob = float(fb["probs"][i]["muscle"])
                remove = eye_prob >= siena_pre.PARAMS["ica_eye_threshold"] or muscle_prob >= siena_pre.PARAMS["ica_muscle_threshold"]
                if remove:
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


def write_outputs(seg: KaggleSegment, payload: MatPayload, processed: Dict, args: argparse.Namespace) -> Dict:
    out_root = Path(args.output_root)
    record_id = siena_pre.sanitize_name(seg.path.stem)
    out_dir = out_root / seg.subject / record_id
    latest_summary = out_dir / "latest_summary.json"
    if latest_summary.exists() and not args.overwrite:
        return {
            "status": "skip",
            "reason": "already_processed",
            "file": str(seg.path),
            "out_dir": str(out_dir),
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    sfreq = payload.sfreq
    ch_names = payload.ch_names
    info = mne.create_info(ch_names=ch_names, sfreq=sfreq, ch_types="eeg")

    raw_v1 = mne.io.RawArray(processed["v1_data"], info, verbose="ERROR")
    raw_v1.set_montage("standard_1020", on_missing="ignore")
    v1_fif = out_dir / "preprocessed_eeg_v1.fif"
    v1_fif_saved = False
    if raw_v1.n_times > 0:
        raw_v1.save(v1_fif, overwrite=True, verbose="ERROR")
        v1_fif_saved = True

    v1_mapping = processed["mapping"]
    (out_dir / "preprocessed_eeg_v1.mapping.json").write_text(json.dumps(v1_mapping, indent=2), encoding="utf-8")

    ica_out = apply_ica(raw_v1, ch_names)
    raw_v2 = ica_out["raw_v2"]
    v2_fif = out_dir / "preprocessed_eeg_v2_ica.fif"
    v2_fif_saved = False
    if raw_v2.n_times > 0:
        raw_v2.save(v2_fif, overwrite=True, verbose="ERROR")
        v2_fif_saved = True

    v2_mapping = dict(v1_mapping)
    v2_mapping["processed_end_sec"] = float(raw_v2.n_times / sfreq)
    (out_dir / "preprocessed_eeg_v2_ica.mapping.json").write_text(json.dumps(v2_mapping, indent=2), encoding="utf-8")

    raw_v3 = mne.io.RawArray(payload.data, info, verbose="ERROR")
    raw_v3.set_montage("standard_1020", on_missing="ignore")
    v3_fif = out_dir / "preprocessed_eeg_v3_raw.fif"
    v3_fif_saved = False
    if raw_v3.n_times > 0:
        raw_v3.save(v3_fif, overwrite=True, verbose="ERROR")
        v3_fif_saved = True

    raw_duration_sec = float(raw_v3.n_times / sfreq)
    v3_mapping = {
        "kept_segments": [
            {
                "segment_index": 0,
                "original_start_sec": 0.0,
                "original_end_sec": raw_duration_sec,
                "processed_start_sec": 0.0,
                "processed_end_sec": raw_duration_sec,
            }
        ]
        if raw_v3.n_times > 0
        else [],
        "discarded_segments": [],
        "original_start_sec": 0.0,
        "original_end_sec": raw_duration_sec,
        "processed_start_sec": 0.0,
        "processed_end_sec": raw_duration_sec,
    }
    (out_dir / "preprocessed_eeg_v3_raw.mapping.json").write_text(json.dumps(v3_mapping, indent=2), encoding="utf-8")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    params_out = dict(siena_pre.PARAMS)
    params_out["corr_threshold"] = float(args.corr_threshold)
    params_out["max_segment_bad_channels"] = int(args.max_segment_bad_channels)
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "dataset": DATASET_NAME,
        "source_mat": str(seg.path),
        "subject": seg.subject,
        "recording_id": record_id,
        "mat_variable": payload.variable_name,
        "segment_kind": seg.segment_kind,
        "segment_number": seg.segment_number,
        "label": seg.label,
        "raw": {
            "sfreq": sfreq,
            "channels": ch_names,
            "data_shape": list(payload.data.shape),
            "data_unit_after_load": "volts",
            "input_unit_assumption": args.input_unit,
            "data_length_sec": payload.data_length_sec,
            "sequence": payload.sequence,
        },
        "params": params_out,
        "flow_order": [
            "load_kaggle_mat",
            "segment_60s",
            "line_noise_50hz_detection_on_unnotched",
            "notch_48_52",
            "segment_bad_detection",
            "local_1s_bad_detection",
            "segment_interp_then_local_interp_non_segment_bad_only",
            "final_bandpass_0p5_60",
            "v2_fastica",
        ],
        "impedance_unavailable": True,
        "line_noise_bad_channel_mask": processed["line_noise_mask"],
        "correlation_bad_channel_mask": processed["corr_mask"],
        "impedance_bad_channel_mask": processed["imp_mask"],
        "local_bad_window_mask": processed["local_bad_mask"],
        "segment_level_bad_channel_mask": processed["segment_level_mask"],
        "interpolation_mask": processed["interpolation_mask"],
        "discarded_segment_log": v1_mapping["discarded_segments"],
        "v1_output": {
            "fif": str(v1_fif),
            "fif_saved": v1_fif_saved,
            "mapping_json": str(out_dir / "preprocessed_eeg_v1.mapping.json"),
            **v1_mapping,
        },
        "v2_output": {
            "fif": str(v2_fif),
            "fif_saved": v2_fif_saved,
            "mapping_json": str(out_dir / "preprocessed_eeg_v2_ica.mapping.json"),
            **v2_mapping,
            "iclabel_available": ica_out["iclabel_available"],
            "fallback_rule_used": ica_out["fallback_rule_used"],
            "v2_method": ica_out["v2_method"],
            "removed_ica_components": ica_out["removed_components"],
            "ica_component_labels": ica_out["ica_component_labels"],
            "ica_removal_log": ica_out["ica_removal_log"],
        },
        "v3_output": {
            "fif": str(v3_fif),
            "fif_saved": v3_fif_saved,
            "mapping_json": str(out_dir / "preprocessed_eeg_v3_raw.mapping.json"),
            "raw_no_preprocess": True,
            "duration_sec": raw_duration_sec,
            "channels": ch_names,
            "label": seg.label,
            **v3_mapping,
        },
    }

    summary_path = out_dir / f"summary_{ts}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    latest_summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return {
        "status": "ok",
        "file": str(seg.path),
        "out_dir": str(out_dir),
        "summary": str(summary_path),
        "label": seg.label,
        "segment_kind": seg.segment_kind,
        "kept_segments": len(v1_mapping["kept_segments"]),
        "discarded_segments": len(v1_mapping["discarded_segments"]),
        "v1_fif_saved": v1_fif_saved,
        "v2_fif_saved": v2_fif_saved,
        "v3_fif_saved": v3_fif_saved,
    }


def process_one(seg: KaggleSegment, args: argparse.Namespace) -> Dict:
    payload = load_kaggle_mat(seg.path, args.input_unit)
    processed = process_segment_data(payload.data, payload.sfreq, payload.ch_names, args)
    return write_outputs(seg, payload, processed, args)


def main() -> int:
    args = parse_args()
    input_root = Path(args.input_root)
    output_root = Path(args.output_root)

    if not input_root.exists() or not input_root.is_dir():
        print(f"ERROR: input root not found: {input_root}", file=sys.stderr)
        return 2

    segments, discovery_skipped = discover_segments(args)
    if not segments:
        print(f"ERROR: no matching MAT files found under: {input_root}", file=sys.stderr)
        return 2
    if args.jobs < 1:
        print("ERROR: --jobs must be >= 1", file=sys.stderr)
        return 2

    run_dir = output_root / "_runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_path = run_dir / f"run_summary_{ts}.json"
    error_path = run_dir / f"errors_{ts}.jsonl"

    results = []
    ok = skipped = failed = 0
    if args.verbose:
        print(f"Input root : {input_root}")
        print(f"Output root: {output_root}")
        print(f"MAT files  : {len(segments)}")
        print(f"Jobs       : {args.jobs}")

    def handle_result(res: Dict, seg: KaggleSegment) -> None:
        nonlocal ok, skipped, failed
        results.append(res)
        if res["status"] == "ok":
            ok += 1
            if args.verbose:
                print(f"[OK] {seg.path.name}")
        elif res["status"] == "skip":
            skipped += 1
            if args.verbose:
                print(f"[SKIP] {seg.path.name}: {res.get('reason')}")
        else:
            failed += 1

    def handle_error(seg: KaggleSegment, e: Exception) -> None:
        nonlocal failed
        failed += 1
        err = {"file": str(seg.path), "error": f"{type(e).__name__}: {e}"}
        results.append({"status": "fail", **err})
        with error_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(err, ensure_ascii=False) + "\n")
        if args.verbose:
            print(f"[FAIL] {seg.path.name}: {err['error']}")

    if args.jobs == 1:
        iterator = enumerate(segments, start=1)
        if not args.no_progress:
            iterator = tqdm(iterator, total=len(segments), desc="kaggle-mat", unit="file")
        for i, seg in iterator:
            if args.verbose:
                print(f"[{i}/{len(segments)}] {seg.path}")
            try:
                handle_result(process_one(seg, args), seg)
            except Exception as e:
                handle_error(seg, e)
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            futures = {ex.submit(process_one, seg, args): seg for seg in segments}
            iterator = as_completed(futures)
            if not args.no_progress:
                iterator = tqdm(iterator, total=len(futures), desc=f"kaggle-mat x{args.jobs}", unit="file")
            for fut in iterator:
                seg = futures[fut]
                if args.verbose:
                    print(f"[DONE?] {seg.path}")
                try:
                    handle_result(fut.result(), seg)
                except Exception as e:
                    handle_error(seg, e)

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "script": "scripts/kaggle_mat_preprocess.py",
        "dataset": DATASET_NAME,
        "input_root": str(input_root),
        "output_root": str(output_root),
        "skip_test": bool(args.skip_test),
        "subjects": args.subjects,
        "max_files": int(args.max_files),
        "jobs": int(args.jobs),
        "counts": {
            "discovered_for_processing": len(segments),
            "discovery_skipped": len(discovery_skipped),
            "ok": ok,
            "skip": skipped,
            "fail": failed,
        },
        "discovery_skipped": discovery_skipped,
        "results": results,
        "error_file": str(error_path),
    }
    run_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Run summary: {run_path}")
    if failed:
        print(f"Completed with failures: ok={ok}, skip={skipped}, fail={failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
