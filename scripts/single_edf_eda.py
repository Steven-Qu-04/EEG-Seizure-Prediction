#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy.signal import welch


THRESHOLDS = {
    "flatline_eps": 1e-12,
    "flatline_min_run_sec": 1.0,
    "saturation_pct": 0.995,
    "robust_z_thresh": 8.0,
    "robust_z_bad_ratio": 0.05,
    "low_variance_quantile": 0.05,
    "high_variance_quantile": 0.95,
    "extreme_p2p_quantile": 0.98,
    "high_corr_threshold": 0.9,
    "low_mean_abs_corr_threshold": 0.2,
}

BANDS = {
    "delta": (0.5, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 45.0),
}


@dataclass
class ErrorEvent:
    stage: str
    error_type: str
    message: str
    details: Dict


class ErrorLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def log(self, evt: ErrorEvent):
        payload = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "stage": evt.stage,
            "error_type": evt.error_type,
            "message": evt.message,
            "details": evt.details,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_args():
    p = argparse.ArgumentParser(description="Single EDF EDA/QC analyzer")
    p.add_argument("--edf", required=True, help="Path to one EDF file")
    p.add_argument("--out", default=None, help="Output directory")
    p.add_argument("--seizure-list", default=None, help="Optional seizure list txt")
    p.add_argument("--plot-seconds", type=float, default=30.0)
    p.add_argument("--psd-max-minutes", type=float, default=10.0)
    p.add_argument("--corr-max-minutes", type=float, default=5.0)
    p.add_argument("--max-plot-channels-per-page", type=int, default=24)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--skip-plots", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def infer_channel_type(name: str) -> str:
    n = (name or "").strip().lower()
    if not n:
        return "unknown"
    if any(k in n for k in ["mark", "status", "trigger", "annot"]):
        return "marker"
    if any(k in n for k in ["eog", "heog", "veog"]):
        return "eog"
    if any(k in n for k in ["emg", "chin", "muscle"]):
        return "emg"
    if any(k in n for k in ["ecg", "ekg"]):
        return "ecg"
    if any(k in n for k in ["resp", "thor", "abdo", "airflow"]):
        return "resp"
    if any(k in n for k in ["spo2", "sao2", "pulseox"]):
        return "spo2"
    if re.match(r"^(fp|f|c|p|o|t|fz|cz|pz|fc|cp)\d*", n):
        return "eeg"
    return "unknown"


def normalize_unit(unit: str) -> str:
    u = (unit or "").strip().replace("μ", "u").replace("µ", "u").lower()
    mapper = {"uv": "uV", "mv": "mV", "v": "V", "": "unknown"}
    return mapper.get(u, unit if unit else "unknown")


def setup_outdir(edf_path: Path, out_arg: Optional[str], overwrite: bool) -> Path:
    out = Path(out_arg) if out_arg else Path("outputs") / "single_edf_eda" / edf_path.stem
    if out.exists() and overwrite:
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plots").mkdir(exist_ok=True)
    (out / "tables").mkdir(exist_ok=True)
    return out


def read_pyedflib_header(edf_path: Path, elog: ErrorLogger):
    try:
        import pyedflib

        with pyedflib.EdfReader(str(edf_path)) as r:
            n = r.signals_in_file
            labels = [r.getLabel(i).strip() for i in range(n)]
            sfs = [float(r.getSampleFrequency(i)) for i in range(n)]
            phys_min = [float(r.getPhysicalMinimum(i)) for i in range(n)]
            phys_max = [float(r.getPhysicalMaximum(i)) for i in range(n)]
            dig_min = [float(r.getDigitalMinimum(i)) for i in range(n)]
            dig_max = [float(r.getDigitalMaximum(i)) for i in range(n)]
            units = [r.getPhysicalDimension(i).strip() for i in range(n)]
            start = r.getStartdatetime().isoformat() if r.getStartdatetime() else None
            duration = float(r.file_duration)
            ann_onsets, ann_durations, ann_desc = r.readAnnotations()
            anns = []
            for a, b, c in zip(ann_onsets, ann_durations, ann_desc):
                anns.append({"onset": float(a), "duration": float(b), "description": str(c)})
            return {
                "ok": True,
                "n_channels": n,
                "ch_names": labels,
                "sample_rates": sfs,
                "phys_min": phys_min,
                "phys_max": phys_max,
                "dig_min": dig_min,
                "dig_max": dig_max,
                "units": units,
                "start_time": start,
                "duration_sec": duration,
                "annotations": anns,
            }
    except Exception as e:
        elog.log(ErrorEvent("read_pyedflib", type(e).__name__, str(e), {"edf": str(edf_path)}))
        return {"ok": False, "error": str(e)}


def read_mne_raw(edf_path: Path, elog: ErrorLogger):
    try:
        import mne

        raw = mne.io.read_raw_edf(str(edf_path), preload=True, verbose="ERROR")
        ann = []
        for onset, duration, desc in zip(raw.annotations.onset, raw.annotations.duration, raw.annotations.description):
            ann.append({"onset": float(onset), "duration": float(duration), "description": str(desc)})
        sfs = [raw.info["sfreq"] for _ in raw.ch_names]
        try:
            per_ch_sfreq = [raw.info["chs"][i].get("sfreq", raw.info["sfreq"]) for i in range(len(raw.ch_names))]
            if any(v is not None for v in per_ch_sfreq):
                sfs = [float(v if v else raw.info["sfreq"]) for v in per_ch_sfreq]
        except Exception:
            pass
        return {
            "ok": True,
            "raw": raw,
            "n_channels": len(raw.ch_names),
            "ch_names": list(raw.ch_names),
            "sample_rates": sfs,
            "duration_sec": float(raw.n_times / raw.info["sfreq"]),
            "start_time": str(raw.info.get("meas_date")),
            "line_freq": raw.info.get("line_freq"),
            "annotations": ann,
            "recording_type": "EDF",
        }
    except Exception as e:
        elog.log(ErrorEvent("read_mne", type(e).__name__, str(e), {"edf": str(edf_path)}))
        return {"ok": False, "error": str(e)}


def compare_readers(pyh: Dict, mneh: Dict, elog: ErrorLogger):
    if not (pyh.get("ok") and mneh.get("ok")):
        return
    diffs = {}
    if pyh["n_channels"] != mneh["n_channels"]:
        diffs["n_channels"] = [pyh["n_channels"], mneh["n_channels"]]
    if abs(pyh["duration_sec"] - mneh["duration_sec"]) > 1.0:
        diffs["duration_sec"] = [pyh["duration_sec"], mneh["duration_sec"]]
    if pyh["ch_names"] != mneh["ch_names"]:
        diffs["channel_names_mismatch"] = True
    if diffs:
        elog.log(ErrorEvent("reader_compare", "reader_discrepancy", "pyedflib and mne differ", diffs))


def compute_flatline_stats(x: np.ndarray, sf: float, eps: float, min_run_sec: float):
    if x.size < 2:
        return 0.0, 0.0, 0.0
    dx = np.abs(np.diff(x))
    flat = dx <= eps
    min_run = max(1, int(min_run_sec * sf))
    runs = []
    run = 0
    for v in flat:
        if v:
            run += 1
        else:
            if run >= min_run:
                runs.append(run)
            run = 0
    if run >= min_run:
        runs.append(run)
    total_flat_samples = int(np.sum(runs))
    total_flat_sec = total_flat_samples / sf
    longest_flat_sec = (max(runs) / sf) if runs else 0.0
    flat_ratio = total_flat_samples / max(1, len(flat))
    return total_flat_sec, flat_ratio, longest_flat_sec


def robust_z_ratio(x: np.ndarray, thresh: float):
    med = np.nanmedian(x)
    mad = np.nanmedian(np.abs(x - med))
    if not np.isfinite(mad) or mad == 0:
        return 0.0
    z = 0.6745 * (x - med) / mad
    return float(np.mean(np.abs(z) > thresh))


def analyze_channels(raw, meta_df: pd.DataFrame, elog: ErrorLogger) -> pd.DataFrame:
    rows = []
    good = meta_df[~meta_df["channel_type"].isin(["marker"])]
    for _, r in good.iterrows():
        ch = r["channel"]
        try:
            x = raw.get_data(picks=[ch])[0]
            sf = float(r["sfreq"])
            finite = np.isfinite(x)
            xf = x[finite] if finite.any() else np.array([], dtype=float)
            nan_cnt = int(np.isnan(x).sum())
            inf_cnt = int(np.isinf(x).sum())
            if xf.size == 0:
                row = {
                    "channel": ch, "mean": np.nan, "std": np.nan, "min": np.nan, "max": np.nan, "peak_to_peak": np.nan,
                    "median": np.nan, "q01": np.nan, "q05": np.nan, "q25": np.nan, "q75": np.nan, "q95": np.nan,
                    "q99": np.nan, "iqr": np.nan, "mad": np.nan, "nan_count": nan_cnt, "inf_count": inf_cnt,
                    "nan_inf_ratio": 1.0, "flatline_total_sec": 0.0, "flatline_ratio": 0.0, "flatline_longest_sec": 0.0,
                    "saturation_ratio": 0.0, "robust_z_outlier_ratio": np.nan,
                }
                rows.append(row)
                continue

            q = np.quantile(xf, [0.01, 0.05, 0.25, 0.75, 0.95, 0.99])
            pmin = r.get("phys_min", np.nan)
            pmax = r.get("phys_max", np.nan)
            sat_ratio = 0.0
            if np.isfinite(pmin) and np.isfinite(pmax) and pmax > pmin:
                low = pmin + (1 - THRESHOLDS["saturation_pct"]) * (pmax - pmin)
                high = pmin + THRESHOLDS["saturation_pct"] * (pmax - pmin)
                sat_ratio = float(np.mean((xf <= low) | (xf >= high)))

            fsec, fratio, flong = compute_flatline_stats(
                xf, sf, THRESHOLDS["flatline_eps"], THRESHOLDS["flatline_min_run_sec"]
            )
            rows.append(
                {
                    "channel": ch,
                    "mean": float(np.mean(xf)),
                    "std": float(np.std(xf)),
                    "min": float(np.min(xf)),
                    "max": float(np.max(xf)),
                    "peak_to_peak": float(np.ptp(xf)),
                    "median": float(np.median(xf)),
                    "q01": float(q[0]),
                    "q05": float(q[1]),
                    "q25": float(q[2]),
                    "q75": float(q[3]),
                    "q95": float(q[4]),
                    "q99": float(q[5]),
                    "iqr": float(q[3] - q[2]),
                    "mad": float(np.median(np.abs(xf - np.median(xf)))),
                    "nan_count": nan_cnt,
                    "inf_count": inf_cnt,
                    "nan_inf_ratio": float((nan_cnt + inf_cnt) / len(x)),
                    "flatline_total_sec": float(fsec),
                    "flatline_ratio": float(fratio),
                    "flatline_longest_sec": float(flong),
                    "saturation_ratio": float(sat_ratio),
                    "robust_z_outlier_ratio": robust_z_ratio(xf, THRESHOLDS["robust_z_thresh"]),
                }
            )
        except Exception as e:
            elog.log(ErrorEvent("channel_stats", type(e).__name__, str(e), {"channel": ch}))

    df = pd.DataFrame(rows)
    if df.empty:
        cols = [
            "channel", "mean", "std", "min", "max", "peak_to_peak", "median", "q01", "q05", "q25", "q75",
            "q95", "q99", "iqr", "mad", "nan_count", "inf_count", "nan_inf_ratio", "flatline_total_sec",
            "flatline_ratio", "flatline_longest_sec", "saturation_ratio", "robust_z_outlier_ratio",
            "low_variance_flag", "high_variance_flag", "extreme_peak_to_peak_flag",
        ]
        return pd.DataFrame(columns=cols)

    s_q_low = df["std"].quantile(THRESHOLDS["low_variance_quantile"])
    s_q_high = df["std"].quantile(THRESHOLDS["high_variance_quantile"])
    p_q_ext = df["peak_to_peak"].quantile(THRESHOLDS["extreme_p2p_quantile"])
    df["low_variance_flag"] = df["std"] <= s_q_low
    df["high_variance_flag"] = df["std"] >= s_q_high
    df["extreme_peak_to_peak_flag"] = df["peak_to_peak"] >= p_q_ext
    return df


def analyze_psd(raw, meta_df: pd.DataFrame, max_minutes: float, elog: ErrorLogger) -> pd.DataFrame:
    rows = []
    good = meta_df[~meta_df["channel_type"].isin(["marker"])]
    for _, r in good.iterrows():
        ch = r["channel"]
        try:
            sf = float(r["sfreq"])
            x = raw.get_data(picks=[ch])[0]
            max_samples = int(max_minutes * 60 * sf)
            x = x[:max_samples] if len(x) > max_samples else x
            if len(x) < int(sf * 4):
                continue
            nperseg = min(int(sf * 4), len(x))
            f, pxx = welch(x, fs=sf, nperseg=nperseg)
            total_power = float(np.trapz(pxx, f)) if len(f) > 1 else np.nan
            out = {"channel": ch, "total_power": total_power}
            for band, (lo, hi) in BANDS.items():
                m = (f >= lo) & (f < hi)
                bp = float(np.trapz(pxx[m], f[m])) if m.any() else 0.0
                out[f"{band}_power"] = bp
                out[f"{band}_ratio"] = (bp / total_power) if total_power and total_power > 0 else np.nan

            lf = (f >= 0.1) & (f < 1.0)
            hf = (f >= 30.0) & (f <= min(100.0, sf / 2.0))
            out["low_freq_drift_ratio"] = float(np.trapz(pxx[lf], f[lf]) / total_power) if lf.any() and total_power > 0 else np.nan
            out["high_freq_noise_ratio"] = float(np.trapz(pxx[hf], f[hf]) / total_power) if hf.any() and total_power > 0 else np.nan

            def line_ratio(center):
                bw = 1.0
                mm = (f >= center - bw) & (f <= center + bw)
                return float(np.trapz(pxx[mm], f[mm]) / total_power) if mm.any() and total_power > 0 else np.nan

            out["line_50hz_ratio"] = line_ratio(50.0)
            out["line_60hz_ratio"] = line_ratio(60.0)
            rows.append(out)
        except Exception as e:
            elog.log(ErrorEvent("psd", type(e).__name__, str(e), {"channel": ch}))
    cols = [
        "channel", "total_power", "delta_power", "delta_ratio", "theta_power", "theta_ratio", "alpha_power", "alpha_ratio",
        "beta_power", "beta_ratio", "gamma_power", "gamma_ratio", "low_freq_drift_ratio", "high_freq_noise_ratio", "line_50hz_ratio", "line_60hz_ratio",
    ]
    return pd.DataFrame(rows, columns=cols)


def analyze_corr(raw, meta_df: pd.DataFrame, max_minutes: float, elog: ErrorLogger):
    groups = {}
    for _, r in meta_df[~meta_df["channel_type"].isin(["marker"])].iterrows():
        groups.setdefault(float(r["sfreq"]), []).append(r["channel"])

    rows = []
    pairs = []
    for sf, chs in groups.items():
        if len(chs) < 2:
            continue
        try:
            data = raw.get_data(picks=chs)
            max_samples = int(max_minutes * 60 * sf)
            data = data[:, :max_samples] if data.shape[1] > max_samples else data
            target = min(256.0, sf)
            decim = max(1, int(sf / target))
            if decim > 1:
                data = data[:, ::decim]
            # Remove channels with non-finite or near-zero variance before corrcoef
            finite_mask = np.all(np.isfinite(data), axis=1)
            var = np.var(data, axis=1)
            var_mask = var > 1e-20
            keep = finite_mask & var_mask
            if np.sum(keep) < 2:
                rows.append(
                    {
                        "sfreq": sf,
                        "n_channels": len(chs),
                        "mean_abs_corr": np.nan,
                        "max_corr": np.nan,
                        "min_corr": np.nan,
                        "low_corr_flag": False,
                    }
                )
                continue
            kept_chs = [cname for cname, k in zip(chs, keep) if k]
            data = data[keep]
            c = np.corrcoef(data)
            iu = np.triu_indices_from(c, k=1)
            vals = c[iu]
            vals = vals[np.isfinite(vals)]
            if vals.size == 0:
                rows.append(
                    {
                        "sfreq": sf,
                        "n_channels": len(chs),
                        "mean_abs_corr": np.nan,
                        "max_corr": np.nan,
                        "min_corr": np.nan,
                        "low_corr_flag": False,
                    }
                )
                continue
            rows.append(
                {
                    "sfreq": sf,
                    "n_channels": len(chs),
                    "mean_abs_corr": float(np.mean(np.abs(vals))),
                    "max_corr": float(np.max(vals)),
                    "min_corr": float(np.min(vals)),
                    "low_corr_flag": float(np.mean(np.abs(vals))) < THRESHOLDS["low_mean_abs_corr_threshold"],
                }
            )
            for i, j in zip(iu[0], iu[1]):
                if np.isfinite(c[i, j]) and abs(c[i, j]) >= THRESHOLDS["high_corr_threshold"]:
                    pairs.append({"sfreq": sf, "ch_a": kept_chs[i], "ch_b": kept_chs[j], "corr": float(c[i, j])})
        except Exception as e:
            elog.log(ErrorEvent("corr", type(e).__name__, str(e), {"sfreq": sf, "channels": chs}))
    return pd.DataFrame(rows), pd.DataFrame(pairs)


def parse_clock_hms(s: str) -> Optional[int]:
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{1,2})", s or "")
    if not m:
        return None
    h, mi, sec = map(int, m.groups())
    return h * 3600 + mi * 60 + sec


def normalize_edf_token(name: str) -> str:
    n = (name or "").strip().upper().replace("PNO", "PN0")
    return n.replace(" ", "")


def parse_seizure_list(path: Path, edf_stem: str, duration_sec: Optional[float], elog: ErrorLogger):
    if not path or not path.exists():
        return pd.DataFrame(columns=["file", "start_sec", "end_sec", "duration_sec", "in_range"])
    text = path.read_text(encoding="utf-8", errors="ignore")
    blocks = [b for b in re.split(r"\n\s*\n", text) if "File name:" in b]
    out = []
    target = normalize_edf_token(edf_stem + ".EDF")
    for b in blocks:
        try:
            fname = re.search(r"File name:\s*(.+)", b, re.IGNORECASE)
            reg_s = re.search(r"Registration start time:\s*(.+)", b, re.IGNORECASE)
            reg_e = re.search(r"Registration end time:\s*(.+)", b, re.IGNORECASE)
            sez_s = re.search(r"Seizure start time:\s*(.+)", b, re.IGNORECASE)
            sez_e = re.search(r"Seizure end time:\s*(.+)", b, re.IGNORECASE)
            if not fname or not sez_s or not sez_e:
                continue
            ftoken = normalize_edf_token(fname.group(1))
            if ftoken != target:
                continue
            rs = parse_clock_hms(reg_s.group(1) if reg_s else "")
            re_ = parse_clock_hms(reg_e.group(1) if reg_e else "")
            ss = parse_clock_hms(sez_s.group(1))
            se = parse_clock_hms(sez_e.group(1))
            if None in [rs, re_, ss, se]:
                continue
            if re_ < rs:
                re_ += 24 * 3600
            if ss < rs:
                ss += 24 * 3600
            if se < ss:
                se += 24 * 3600
            start_rel = ss - rs
            end_rel = se - rs
            in_range = True if duration_sec is None else (start_rel >= 0 and end_rel <= duration_sec + 1)
            out.append(
                {
                    "file": fname.group(1).strip(),
                    "start_sec": float(start_rel),
                    "end_sec": float(end_rel),
                    "duration_sec": float(end_rel - start_rel),
                    "in_range": bool(in_range),
                }
            )
        except Exception as e:
            elog.log(ErrorEvent("seizure_parse", type(e).__name__, str(e), {"block": b[:200]}))
    cols = ["file", "start_sec", "end_sec", "duration_sec", "in_range"]
    return pd.DataFrame(out, columns=cols)


def analyze_annotations(anns: List[Dict], duration_sec: Optional[float]):
    rows = []
    for a in anns:
        desc = str(a.get("description", "")).strip() or "<empty>"
        onset = float(a.get("onset", 0.0))
        dur = float(a.get("duration", 0.0))
        end = onset + dur
        out_of_bounds = bool(duration_sec is not None and (onset < 0 or end > duration_sec + 1e-6))
        rows.append({"description": desc, "onset": onset, "duration": dur, "end": end, "out_of_bounds": out_of_bounds})
    event_df = pd.DataFrame(rows, columns=["description", "onset", "duration", "end", "out_of_bounds"])
    if event_df.empty:
        return event_df, pd.DataFrame(columns=["description", "count", "total_duration", "duration_ratio"])
    g = event_df.groupby("description", as_index=False)["duration"].agg(["count", "sum"]).reset_index()
    g.columns = ["description", "count", "total_duration"]
    g["duration_ratio"] = g["total_duration"] / duration_sec if duration_sec and duration_sec > 0 else np.nan
    return event_df, g


def make_plots(raw, outdir: Path, ch_stats: pd.DataFrame, psd_df: pd.DataFrame, corr_group_df: pd.DataFrame, ann_df: pd.DataFrame, seizure_df: pd.DataFrame, args, elog: ErrorLogger):
    try:
        import matplotlib.pyplot as plt
    except Exception as e:
        elog.log(ErrorEvent("plot_import", type(e).__name__, str(e), {}))
        return
    plot_dir = outdir / "plots"

    try:
        ch_names = raw.ch_names
        npp = max(1, args.max_plot_channels_per_page)
        max_samples = int(args.plot_seconds * raw.info["sfreq"])
        data = raw.get_data()[:, :max_samples]
        t = np.arange(data.shape[1]) / raw.info["sfreq"]
        for i in range(0, len(ch_names), npp):
            fig, ax = plt.subplots(figsize=(16, 9))
            idx = list(range(i, min(i + npp, len(ch_names))))
            amp = np.nanmax(np.abs(data[idx])) if len(idx) else 1.0
            offsets = np.arange(len(idx)) * (amp if amp > 0 else 1.0) * 2.2
            for k, ch_idx in enumerate(idx):
                ax.plot(t, data[ch_idx] + offsets[k], lw=0.6)
            ax.set_yticks(offsets)
            ax.set_yticklabels([ch_names[j] for j in idx])
            ax.set_title(f"Raw preview {i+1}-{i+len(idx)}")
            ax.set_xlabel("sec")
            fig.tight_layout()
            fig.savefig(plot_dir / f"raw_preview_page_{i//npp+1:02d}.png", dpi=140)
            plt.close(fig)
    except Exception as e:
        elog.log(ErrorEvent("plot_raw", type(e).__name__, str(e), {}))

    try:
        if not psd_df.empty:
            fig, ax = plt.subplots(figsize=(12, 6))
            for _, r in psd_df.head(20).iterrows():
                vals = [r.get(f"{b}_ratio", np.nan) for b in ["delta", "theta", "alpha", "beta", "gamma"]]
                ax.plot(["delta", "theta", "alpha", "beta", "gamma"], vals, alpha=0.5)
            ax.set_title("PSD band ratios overview (first 20 channels)")
            fig.tight_layout()
            fig.savefig(plot_dir / "psd_overview.png", dpi=140)
            plt.close(fig)
    except Exception as e:
        elog.log(ErrorEvent("plot_psd", type(e).__name__, str(e), {}))

    try:
        if not ch_stats.empty:
            fig, ax = plt.subplots(figsize=(14, 5))
            ax.bar(ch_stats["channel"], ch_stats["std"], alpha=0.8, label="std")
            ax.plot(ch_stats["channel"], ch_stats["peak_to_peak"], color="tab:red", marker=".", lw=1, label="p2p")
            ax.tick_params(axis="x", rotation=90)
            ax.legend()
            ax.set_title("Channel std / peak-to-peak")
            fig.tight_layout()
            fig.savefig(plot_dir / "channel_std_p2p.png", dpi=140)
            plt.close(fig)

            fig, ax = plt.subplots(figsize=(14, 5))
            ax.bar(ch_stats["channel"], ch_stats["flatline_ratio"], alpha=0.8, label="flatline_ratio")
            ax.plot(ch_stats["channel"], ch_stats["saturation_ratio"], color="tab:orange", marker=".", lw=1, label="saturation_ratio")
            ax.tick_params(axis="x", rotation=90)
            ax.legend()
            ax.set_title("Flatline ratio / saturation ratio")
            fig.tight_layout()
            fig.savefig(plot_dir / "flatline_saturation.png", dpi=140)
            plt.close(fig)
    except Exception as e:
        elog.log(ErrorEvent("plot_channel_stats", type(e).__name__, str(e), {}))

    try:
        if not corr_group_df.empty:
            fig, ax = plt.subplots(figsize=(8, 4))
            ax.bar(corr_group_df["sfreq"].astype(str), corr_group_df["mean_abs_corr"])
            ax.set_title("Mean absolute correlation by sfreq group")
            fig.tight_layout()
            fig.savefig(plot_dir / "corr_group.png", dpi=140)
            plt.close(fig)
    except Exception as e:
        elog.log(ErrorEvent("plot_corr", type(e).__name__, str(e), {}))

    try:
        if not ann_df.empty:
            fig, ax = plt.subplots(figsize=(12, 4))
            ax.scatter(ann_df["onset"], ann_df["description"], s=12)
            ax.set_xlabel("onset sec")
            ax.set_title("Annotation distribution")
            fig.tight_layout()
            fig.savefig(plot_dir / "annotation_distribution.png", dpi=140)
            plt.close(fig)
        else:
            fig, ax = plt.subplots(figsize=(10, 3))
            ax.axis("off")
            ax.text(0.5, 0.5, "No annotations found in EDF", ha="center", va="center")
            fig.tight_layout()
            fig.savefig(plot_dir / "annotation_distribution.png", dpi=140)
            plt.close(fig)
    except Exception as e:
        elog.log(ErrorEvent("plot_ann", type(e).__name__, str(e), {}))

    try:
        if seizure_df is not None and not seizure_df.empty:
            fig, ax = plt.subplots(figsize=(12, 2.5))
            for _, r in seizure_df.iterrows():
                ax.plot([r["start_sec"], r["end_sec"]], [1, 1], lw=8)
            ax.set_yticks([1])
            ax.set_yticklabels(["seizure"])
            ax.set_xlabel("sec")
            ax.set_title("Seizure timeline")
            fig.tight_layout()
            fig.savefig(plot_dir / "seizure_timeline.png", dpi=140)
            plt.close(fig)
    except Exception as e:
        elog.log(ErrorEvent("plot_seizure", type(e).__name__, str(e), {}))


def df_to_text(df: pd.DataFrame, max_rows: int = 50) -> str:
    if df is None or df.empty:
        return "<empty>"
    return df.head(max_rows).to_string(index=False)


def save_csvs(outdir: Path, tables: Dict[str, pd.DataFrame]):
    for name, df in tables.items():
        path = outdir / "tables" / f"{name}.csv"
        if df is None:
            pd.DataFrame().to_csv(path, index=False)
        else:
            df.to_csv(path, index=False)


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    edf_path = Path(args.edf)
    outdir = setup_outdir(edf_path, args.out, args.overwrite)
    elog = ErrorLogger(outdir / "errors.jsonl")

    file_flags = []
    if not edf_path.exists():
        elog.log(ErrorEvent("input", "FileNotFoundError", "EDF path does not exist", {"edf": str(edf_path)}))
        file_flags.append("file_not_found")
    if edf_path.exists() and edf_path.stat().st_size == 0:
        elog.log(ErrorEvent("input", "EmptyFile", "EDF file is zero bytes", {"edf": str(edf_path)}))
        file_flags.append("file_empty")

    pyh = read_pyedflib_header(edf_path, elog) if edf_path.exists() else {"ok": False}
    mneh = read_mne_raw(edf_path, elog) if edf_path.exists() else {"ok": False}
    compare_readers(pyh, mneh, elog)

    raw = mneh.get("raw") if mneh.get("ok") else None
    duration_sec = mneh.get("duration_sec") if mneh.get("ok") else pyh.get("duration_sec") if pyh.get("ok") else None

    ch_meta_rows = []
    source = pyh if pyh.get("ok") else mneh
    if source and source.get("ok"):
        ch_names = source.get("ch_names", [])
        sfs = source.get("sample_rates", [])
        units = source.get("units", [""] * len(ch_names))
        pmin = source.get("phys_min", [np.nan] * len(ch_names))
        pmax = source.get("phys_max", [np.nan] * len(ch_names))
        dmin = source.get("dig_min", [np.nan] * len(ch_names))
        dmax = source.get("dig_max", [np.nan] * len(ch_names))
        for i, ch in enumerate(ch_names):
            nm = (ch or "").strip()
            ch_meta_rows.append(
                {
                    "channel": nm,
                    "channel_type": infer_channel_type(nm),
                    "unit_raw": units[i] if i < len(units) else "",
                    "unit_norm": normalize_unit(units[i] if i < len(units) else ""),
                    "sfreq": float(sfs[i]) if i < len(sfs) else np.nan,
                    "phys_min": pmin[i] if i < len(pmin) else np.nan,
                    "phys_max": pmax[i] if i < len(pmax) else np.nan,
                    "dig_min": dmin[i] if i < len(dmin) else np.nan,
                    "dig_max": dmax[i] if i < len(dmax) else np.nan,
                    "empty_name": nm == "",
                }
            )
    ch_meta_df = pd.DataFrame(ch_meta_rows)
    if ch_meta_df.empty:
        ch_meta_df = pd.DataFrame(columns=["channel", "channel_type", "unit_raw", "unit_norm", "sfreq", "phys_min", "phys_max", "dig_min", "dig_max", "empty_name"])

    ch_meta_df["duplicated_name"] = ch_meta_df["channel"].duplicated(keep=False) if not ch_meta_df.empty else pd.Series(dtype=bool)
    ch_meta_df["phys_range_invalid"] = ch_meta_df["phys_max"] <= ch_meta_df["phys_min"] if not ch_meta_df.empty else pd.Series(dtype=bool)
    ch_meta_df["dig_range_invalid"] = ch_meta_df["dig_max"] <= ch_meta_df["dig_min"] if not ch_meta_df.empty else pd.Series(dtype=bool)
    multi_sfreq = int(ch_meta_df["sfreq"].nunique(dropna=True)) > 1 if not ch_meta_df.empty else False

    ann_source = mneh.get("annotations", []) if mneh.get("ok") else pyh.get("annotations", []) if pyh.get("ok") else []
    ann_df, ann_summary_df = analyze_annotations(ann_source, duration_sec)
    seizure_df = parse_seizure_list(Path(args.seizure_list), edf_path.stem, duration_sec, elog) if args.seizure_list else pd.DataFrame(columns=["file", "start_sec", "end_sec", "duration_sec", "in_range"])

    consistency = {}
    if not ann_df.empty and not seizure_df.empty:
        ann_seiz = ann_df[ann_df["description"].str.lower().str.contains("seiz", na=False)]
        consistency = {"ann_seiz_count": int(len(ann_seiz)), "list_seiz_count": int(len(seizure_df)), "count_match": int(len(ann_seiz)) == int(len(seizure_df))}

    ch_stats_df = pd.DataFrame()
    psd_df = pd.DataFrame()
    corr_group_df = pd.DataFrame()
    corr_pairs_df = pd.DataFrame()
    if raw is not None and not ch_meta_df.empty:
        ch_stats_df = analyze_channels(raw, ch_meta_df, elog)
        psd_df = analyze_psd(raw, ch_meta_df, args.psd_max_minutes, elog)
        corr_group_df, corr_pairs_df = analyze_corr(raw, ch_meta_df, args.corr_max_minutes, elog)

    if not args.skip_plots and raw is not None:
        make_plots(raw, outdir, ch_stats_df, psd_df, corr_group_df, ann_df, seizure_df, args, elog)

    tables = {
        "channel_metadata": ch_meta_df,
        "channel_stats": ch_stats_df,
        "psd_features": psd_df,
        "corr_groups": corr_group_df,
        "corr_high_pairs": corr_pairs_df,
        "annotations": ann_df,
        "annotation_summary": ann_summary_df,
        "seizure_list_events": seizure_df,
    }
    save_csvs(outdir, tables)

    metadata = {
        "edf": str(edf_path),
        "size_bytes": edf_path.stat().st_size if edf_path.exists() else None,
        "readable": edf_path.exists(),
        "duration_sec": duration_sec,
        "n_channels": int(ch_meta_df.shape[0]) if not ch_meta_df.empty else 0,
        "multi_sampling_rate": multi_sfreq,
        "line_freq": mneh.get("line_freq") if mneh.get("ok") else None,
        "recording_type": mneh.get("recording_type") if mneh.get("ok") else None,
        "start_time_pyedflib": pyh.get("start_time") if pyh.get("ok") else None,
        "start_time_mne": mneh.get("start_time") if mneh.get("ok") else None,
    }

    channel_flags = []
    if not ch_stats_df.empty:
        for _, r in ch_stats_df.iterrows():
            flags = []
            if bool(r.get("low_variance_flag", False)):
                flags.append("low_variance")
            if bool(r.get("high_variance_flag", False)):
                flags.append("high_variance")
            if bool(r.get("extreme_peak_to_peak_flag", False)):
                flags.append("extreme_p2p")
            if float(r.get("robust_z_outlier_ratio", 0) or 0) >= THRESHOLDS["robust_z_bad_ratio"]:
                flags.append("high_robust_outlier_ratio")
            if float(r.get("flatline_ratio", 0) or 0) > 0.1:
                flags.append("high_flatline_ratio")
            if float(r.get("saturation_ratio", 0) or 0) > 0.05:
                flags.append("high_saturation_ratio")
            if flags:
                channel_flags.append({"channel": r["channel"], "flags": flags})

    with (outdir / "ana.txt").open("w", encoding="utf-8") as f:
        f.write("# FILE_METADATA\n")
        f.write(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n\n")
        f.write("# QC_THRESHOLDS\n")
        f.write(json.dumps(THRESHOLDS, ensure_ascii=False, indent=2) + "\n\n")
        f.write("# FILE_FLAGS\n")
        f.write(json.dumps(file_flags, ensure_ascii=False, indent=2) + "\n\n")
        f.write("# CHANNEL_METADATA\n")
        f.write(df_to_text(ch_meta_df, max_rows=200) + "\n\n")
        f.write("# CHANNEL_QC_STATS\n")
        f.write(df_to_text(ch_stats_df, max_rows=200) + "\n\n")
        f.write("# CHANNEL_FLAGS\n")
        f.write(json.dumps(channel_flags, ensure_ascii=False, indent=2) + "\n\n")
        f.write("# PSD_FEATURES\n")
        f.write(df_to_text(psd_df, max_rows=200) + "\n\n")
        f.write("# CORRELATION_SUMMARY\n")
        f.write(df_to_text(corr_group_df, max_rows=200) + "\n\n")
        f.write("# HIGH_CORRELATION_PAIRS\n")
        f.write(df_to_text(corr_pairs_df, max_rows=200) + "\n\n")
        f.write("# ANNOTATION_EVENTS\n")
        f.write(df_to_text(ann_df, max_rows=200) + "\n\n")
        f.write("# ANNOTATION_SUMMARY\n")
        f.write(df_to_text(ann_summary_df, max_rows=200) + "\n\n")
        f.write("# SEIZURE_LIST_EVENTS\n")
        f.write(df_to_text(seizure_df, max_rows=200) + "\n\n")
        f.write("# ANNOTATION_SEIZURE_CONSISTENCY\n")
        f.write(json.dumps(consistency, ensure_ascii=False, indent=2) + "\n\n")
        plot_files = sorted([p.name for p in (outdir / "plots").glob("*.png")])
        f.write("# PLOT_FILES\n")
        f.write(json.dumps(plot_files, ensure_ascii=False, indent=2) + "\n")

    logging.info("Done. Output: %s", outdir)


if __name__ == "__main__":
    main()
