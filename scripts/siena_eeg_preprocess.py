#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import mne
import numpy as np
from mne.preprocessing import ICA
from scipy.signal import welch


PARAMS = {
    "segment_sec": 60,
    "local_win_sec": 1,
    "local_step_sec": 1,
    "line_freq": 50,
    "line_noise_sd_threshold": 4,
    "notch_band_hz": [48, 52],
    "corr_threshold": 0.70,
    "impedance_threshold_ohm": 200000,
    "flat_sd_threshold_uv": 1,
    "p2p_threshold_uv": 200,
    "local_bad_duration_upgrade_sec": 10,
    "upgrade_rule": ">10s",
    "max_segment_bad_channels": 5,
    "bandpass_hz": [0.5, 60],
    "interpolation": "spherical_spline",
    "ica_algorithm": "FastICA",
    "ica_eye_threshold": 0.90,
    "ica_muscle_threshold": 0.90,
}


@dataclass
class SegmentMap:
    segment_index: int
    original_start_sec: float
    original_end_sec: float
    processed_start_sec: float
    processed_end_sec: float


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SIENA EEG preprocess (V1 + V2 ICA)")
    p.add_argument("--input-root", default="data/raw/siena/PN06")
    p.add_argument("--output-root", default="data/processed")
    p.add_argument("--segment-sec", type=int, default=PARAMS["segment_sec"])
    p.add_argument("--local-win-sec", type=int, default=PARAMS["local_win_sec"])
    p.add_argument("--local-step-sec", type=int, default=PARAMS["local_step_sec"])
    p.add_argument("--corr-threshold", type=float, default=PARAMS["corr_threshold"])
    p.add_argument("--max-segment-bad-channels", type=int, default=PARAMS["max_segment_bad_channels"])
    p.add_argument("--max-files", type=int, default=0)
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def is_eeg_channel_name(ch: str) -> bool:
    cu = ch.strip().upper()
    if cu.startswith("EEG "):
        cu = cu[4:].strip()
    return re.match(r"^(FP|F|C|P|O|T|FC|CP|AF|PO)\d{0,2}$", cu) is not None or cu in {
        "FZ",
        "CZ",
        "PZ",
        "OZ",
    }


def sanitize_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in name)


def line_noise_power_50hz(x: np.ndarray, sfreq: float) -> float:
    freqs, pxx = welch(x, fs=sfreq, nperseg=min(len(x), int(sfreq * 4)))
    band = (freqs >= 49.0) & (freqs <= 51.0)
    if not np.any(band):
        return 0.0
    return float(np.mean(pxx[band]))


def max_abs_corr_per_channel(seg_data: np.ndarray) -> np.ndarray:
    if seg_data.shape[0] <= 1:
        return np.ones(seg_data.shape[0], dtype=float)
    c = np.corrcoef(seg_data)
    c = np.nan_to_num(c, nan=0.0, posinf=0.0, neginf=0.0)
    out = np.zeros(seg_data.shape[0], dtype=float)
    for i in range(seg_data.shape[0]):
        vals = np.abs(np.delete(c[i], i))
        out[i] = float(np.max(vals)) if vals.size else 1.0
    return out


def window_bad_mask(seg_data: np.ndarray, sfreq: float, win_sec: int, step_sec: int) -> Tuple[np.ndarray, int]:
    n_ch, n_t = seg_data.shape
    win = int(round(win_sec * sfreq))
    step = int(round(step_sec * sfreq))
    starts = np.arange(0, n_t - win + 1, step, dtype=int)
    mask = np.zeros((n_ch, len(starts)), dtype=bool)
    for wi, s in enumerate(starts):
        w = seg_data[:, s : s + win] * 1e6
        std_uv = np.std(w, axis=1)
        p2p_uv = np.ptp(w, axis=1)
        mask[:, wi] = (std_uv < PARAMS["flat_sd_threshold_uv"]) | (p2p_uv > PARAMS["p2p_threshold_uv"])
    return mask, win


def spherical_interpolate_segment(seg_data: np.ndarray, ch_names: List[str], sfreq: float, bad_idx: np.ndarray) -> np.ndarray:
    if bad_idx.size == 0:
        return seg_data.copy()
    info = mne.create_info(ch_names=ch_names, sfreq=sfreq, ch_types="eeg")
    raw_seg = mne.io.RawArray(seg_data.copy(), info, verbose="ERROR")
    raw_seg.set_montage("standard_1020", on_missing="ignore")
    raw_seg.info["bads"] = [ch_names[i] for i in bad_idx]
    try:
        raw_seg.interpolate_bads(reset_bads=True, mode="accurate", method={"eeg": "spline"}, verbose="ERROR")
        return raw_seg.get_data()
    except Exception:
        good_idx = np.array([i for i in range(seg_data.shape[0]) if i not in set(bad_idx.tolist())], dtype=int)
        out = seg_data.copy()
        if good_idx.size == 0:
            return out
        mean_good = np.mean(out[good_idx], axis=0)
        out[bad_idx] = mean_good
        return out


def local_window_interpolate(seg_data: np.ndarray, bad_window_mask: np.ndarray, win_samples: int, step_samples: int, exclude_channels: np.ndarray) -> np.ndarray:
    out = seg_data.copy()
    n_ch, n_w = bad_window_mask.shape
    exclude = set(exclude_channels.tolist())
    for ch in range(n_ch):
        if ch in exclude:
            continue
        for wi in range(n_w):
            if not bad_window_mask[ch, wi]:
                continue
            s = wi * step_samples
            e = s + win_samples
            peers = [i for i in range(n_ch) if i != ch and i not in exclude]
            if not peers:
                continue
            out[ch, s:e] = np.mean(out[peers, s:e], axis=0)
    return out


def detect_iclabel(ica: ICA, raw_v1: mne.io.BaseRaw):
    try:
        from mne_icalabel import label_components

        labels = label_components(raw_v1, ica, method="iclabel")
        return True, labels
    except Exception:
        return False, None


def fallback_ica_labels(ica: ICA, raw_v1: mne.io.BaseRaw) -> Dict:
    src = ica.get_sources(raw_v1).get_data()
    sf = raw_v1.info["sfreq"]
    ch_names = raw_v1.ch_names
    fp_candidates = [c for c in ch_names if c.upper().replace("EEG ", "") in {"FP1", "FP2", "AF3", "AF4"}]
    if fp_candidates:
        eog_proxy = np.mean(raw_v1.get_data(picks=fp_candidates), axis=0)
    else:
        eog_proxy = np.mean(raw_v1.get_data(), axis=0)
    probs = []
    labels = []
    for i in range(src.shape[0]):
        sig = src[i]
        corr = np.corrcoef(sig, eog_proxy)[0, 1]
        corr = float(np.nan_to_num(np.abs(corr), nan=0.0))
        f, p = welch(sig, fs=sf, nperseg=min(len(sig), int(sf * 4)))
        band_all = (f >= 1) & (f <= 45)
        band_muscle = (f >= 20) & (f <= 45)
        all_power = float(np.sum(p[band_all])) + 1e-12
        muscle_ratio = float(np.sum(p[band_muscle]) / all_power)
        eye_p = min(1.0, corr)
        muscle_p = min(1.0, muscle_ratio * 2.0)
        if eye_p >= PARAMS["ica_eye_threshold"]:
            lab = "eye"
        elif muscle_p >= PARAMS["ica_muscle_threshold"]:
            lab = "muscle"
        else:
            lab = "brain_or_other"
        labels.append(lab)
        probs.append({"eye": eye_p, "muscle": muscle_p})
    return {"labels": labels, "probs": probs}


def process_one_file(edf_path: Path, input_root: Path, output_root: Path, args: argparse.Namespace) -> Dict:
    raw = mne.io.read_raw_edf(str(edf_path), preload=False, verbose="ERROR")
    sfreq = float(raw.info["sfreq"])
    eeg_channels = [ch for ch in raw.ch_names if is_eeg_channel_name(ch)]
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

        # 50Hz line-noise power on UN-NOTCHED segment
        line_powers = np.array([line_noise_power_50hz(seg_raw[c], sfreq) for c in range(seg_raw.shape[0])], dtype=float)
        thr = float(np.mean(line_powers) + PARAMS["line_noise_sd_threshold"] * np.std(line_powers))
        line_bad = line_powers > thr
        line_noise_mask.append(line_bad.tolist())

        # notch AFTER line-noise detection
        seg_notched = mne.filter.notch_filter(
            seg_raw,
            Fs=sfreq,
            freqs=[PARAMS["line_freq"]],
            notch_widths=4.0,
            method="fir",
            phase="zero",
            verbose="ERROR",
        )

        # auxiliary copy for correlation metric only; does NOT alter main flow order
        seg_corr_aux = mne.filter.filter_data(
            seg_notched,
            sfreq=sfreq,
            l_freq=1.0,
            h_freq=45.0,
            method="iir",
            verbose="ERROR",
        )
        corr_vals = max_abs_corr_per_channel(seg_corr_aux)
        corr_bad = corr_vals < args.corr_threshold
        corr_mask.append(corr_bad.tolist())

        imp_bad = np.zeros(seg_notched.shape[0], dtype=bool)
        imp_mask.append(imp_bad.tolist())

        bad_w, _ = window_bad_mask(seg_notched, sfreq, args.local_win_sec, args.local_step_sec)
        local_bad_mask.append(bad_w.tolist())
        bad_duration_sec = np.sum(bad_w, axis=1) * args.local_step_sec
        upgraded = bad_duration_sec > PARAMS["local_bad_duration_upgrade_sec"]

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
        seg_interp = spherical_interpolate_segment(seg_notched, ch_names, sfreq, seg_bad_idx)

        # local interpolation only for channels NOT upgraded to segment-level
        seg_local_interp = local_window_interpolate(
            seg_interp,
            bad_w,
            win_samples=win_samples,
            step_samples=step_samples,
            exclude_channels=seg_bad_idx,
        )

        kept_data.append(seg_local_interp)
        seg_len_sec = args.segment_sec
        kept_segments.append(
            SegmentMap(
                segment_index=seg_i,
                original_start_sec=float(s / sfreq),
                original_end_sec=float(e / sfreq),
                processed_start_sec=float(proc_cursor_sec),
                processed_end_sec=float(proc_cursor_sec + seg_len_sec),
            ).__dict__
        )
        proc_cursor_sec += seg_len_sec

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

    rel_parent = edf_path.relative_to(input_root).parent
    if str(rel_parent) == ".":
        rel_parent = Path(edf_path.parent.name)
    record_id = sanitize_name(edf_path.stem)
    out_dir = output_root / "siena" / rel_parent / record_id
    out_dir.mkdir(parents=True, exist_ok=True)

    if kept_data:
        v1_data = np.concatenate(kept_data, axis=1)
    else:
        v1_data = np.zeros((len(ch_names), 0), dtype=float)
    # bandpass is LAST filter step in V1 main flow
    if v1_data.shape[1] > 0:
        v1_data = mne.filter.filter_data(
            v1_data,
            sfreq=sfreq,
            l_freq=PARAMS["bandpass_hz"][0],
            h_freq=PARAMS["bandpass_hz"][1],
            method="iir",
            verbose="ERROR",
        )
    info = mne.create_info(ch_names=ch_names, sfreq=sfreq, ch_types="eeg")
    v1_fif = out_dir / "preprocessed_eeg_v1.fif"
    v1_fif_saved = False
    raw_v1 = mne.io.RawArray(v1_data, info, verbose="ERROR")
    raw_v1.set_montage("standard_1020", on_missing="ignore")
    if raw_v1.n_times > 0:
        raw_v1.save(v1_fif, overwrite=True, verbose="ERROR")
        v1_fif_saved = True

    v1_mapping = {
        "kept_segments": kept_segments,
        "discarded_segments": discarded_segments,
        "original_start_sec": 0.0,
        "original_end_sec": float(n_segments * args.segment_sec),
        "processed_start_sec": 0.0,
        "processed_end_sec": float(raw_v1.n_times / sfreq),
    }
    (out_dir / "preprocessed_eeg_v1.mapping.json").write_text(json.dumps(v1_mapping, indent=2), encoding="utf-8")

    # V2 ICA
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
        iclabel_available, iclabel_result = detect_iclabel(ica, raw_v2)
        if iclabel_available:
            labels = iclabel_result.get("labels", [])
            y_pred = iclabel_result.get("y_pred_proba", None)
            for i, lab in enumerate(labels):
                eye_prob = 0.0
                muscle_prob = 0.0
                if isinstance(y_pred, np.ndarray) and y_pred.ndim == 2 and y_pred.shape[0] > i:
                    # mne-icalabel canonical order includes "eye", "muscle"
                    classes = iclabel_result.get("classes", [])
                    if "eye" in classes:
                        eye_prob = float(y_pred[i, classes.index("eye")])
                    if "muscle" in classes:
                        muscle_prob = float(y_pred[i, classes.index("muscle")])
                remove = eye_prob >= PARAMS["ica_eye_threshold"] or muscle_prob >= PARAMS["ica_muscle_threshold"]
                if remove:
                    removed_components.append(i)
                ica_component_labels.append({"component": i, "label": str(lab), "eye_prob": eye_prob, "muscle_prob": muscle_prob})
        else:
            fallback_rule_used = True
            v2_method = "fallback_eog_emg_heuristic"
            fb = fallback_ica_labels(ica, raw_v2)
            for i, lab in enumerate(fb["labels"]):
                eye_prob = float(fb["probs"][i]["eye"])
                muscle_prob = float(fb["probs"][i]["muscle"])
                remove = eye_prob >= PARAMS["ica_eye_threshold"] or muscle_prob >= PARAMS["ica_muscle_threshold"]
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

    v2_fif = out_dir / "preprocessed_eeg_v2_ica.fif"
    v2_fif_saved = False
    if raw_v2.n_times > 0:
        raw_v2.save(v2_fif, overwrite=True, verbose="ERROR")
        v2_fif_saved = True
    v2_mapping = {
        "kept_segments": kept_segments,
        "discarded_segments": discarded_segments,
        "original_start_sec": 0.0,
        "original_end_sec": float(n_segments * args.segment_sec),
        "processed_start_sec": 0.0,
        "processed_end_sec": float(raw_v2.n_times / sfreq),
    }
    (out_dir / "preprocessed_eeg_v2_ica.mapping.json").write_text(json.dumps(v2_mapping, indent=2), encoding="utf-8")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    params_out = dict(PARAMS)
    params_out["corr_threshold"] = float(args.corr_threshold)
    params_out["max_segment_bad_channels"] = int(args.max_segment_bad_channels)
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_edf": str(edf_path),
        "recording_id": record_id,
        "params": params_out,
        "flow_order": [
            "step0_drop_non_eeg",
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
        "line_noise_bad_channel_mask": line_noise_mask,
        "correlation_bad_channel_mask": corr_mask,
        "impedance_bad_channel_mask": imp_mask,
        "local_bad_window_mask": local_bad_mask,
        "segment_level_bad_channel_mask": segment_level_mask,
        "interpolation_mask": interpolation_mask,
        "discarded_segment_log": discarded_segments,
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
            "iclabel_available": iclabel_available,
            "fallback_rule_used": fallback_rule_used,
            "v2_method": v2_method,
            "removed_ica_components": sorted(set(removed_components)),
            "ica_component_labels": ica_component_labels,
            "ica_removal_log": ica_removal_log,
        },
    }
    summary_path = out_dir / f"summary_{ts}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "latest_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return {
        "file": str(edf_path),
        "out_dir": str(out_dir),
        "summary": str(summary_path),
        "kept_segments": len(kept_segments),
        "discarded_segments": len(discarded_segments),
    }


def main():
    args = parse_args()
    input_root = Path(args.input_root)
    output_root = Path(args.output_root)
    files = sorted(input_root.rglob("*.edf"))
    if args.max_files and args.max_files > 0:
        files = files[: args.max_files]
    if not files:
        raise RuntimeError(f"No EDF files found under {input_root}")

    results = []
    for i, f in enumerate(files, start=1):
        if args.verbose:
            print(f"[{i}/{len(files)}] processing {f}")
        results.append(process_one_file(f, input_root, output_root, args))
        if args.verbose:
            print(f"[OK] {f.name}")

    run_summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "script": "scripts/siena_eeg_preprocess.py",
        "input_root": str(input_root),
        "output_root": str(output_root),
        "files_processed": len(results),
        "results": results,
    }
    run_dir = output_root / "siena" / "_runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_path = run_dir / f"run_summary_{run_ts}.json"
    run_path.write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    print(f"Run summary: {run_path}")


if __name__ == "__main__":
    main()
