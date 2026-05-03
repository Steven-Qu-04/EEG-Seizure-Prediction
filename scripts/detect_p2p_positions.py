#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import mne
import numpy as np
import re


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Detect per-channel peak-to-peak > threshold locations and draw channel timelines."
    )
    p.add_argument(
        "--input-root",
        default="data/raw/siena",
        help="Root directory containing EDF files (searched recursively).",
    )
    p.add_argument(
        "--output-root",
        default="outputs",
        help="Root output directory; a new subdirectory will be created under it.",
    )
    p.add_argument(
        "--threshold-uv",
        type=float,
        default=200.0,
        help="Peak-to-peak threshold in microvolts.",
    )
    p.add_argument(
        "--window-sec",
        type=float,
        default=1.0,
        help="Sliding window length in seconds for peak-to-peak computation.",
    )
    p.add_argument(
        "--step-sec",
        type=float,
        default=0.5,
        help="Sliding step in seconds.",
    )
    p.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Optional cap on number of EDF files to process (0 means no cap).",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print progress logs.",
    )
    return p.parse_args()


def find_edf_files(input_root: Path) -> List[Path]:
    return sorted(input_root.rglob("*.edf"))


def make_output_dir(output_root: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = output_root / f"p2p_timeline_{ts}"
    out.mkdir(parents=True, exist_ok=False)
    return out


def sanitize_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in name)


def is_eeg_channel_name(ch: str) -> bool:
    cu = ch.strip().upper()
    if cu.startswith("EEG "):
        return True
    # Accept common EEG naming without explicit "EEG " prefix.
    return re.match(r"^(FP|F|C|P|O|T|FC|CP)\d{0,2}$", cu) is not None or cu in {"FZ", "CZ", "PZ", "OZ"}


def detect_events_for_channel(
    x_uv: np.ndarray,
    sfreq: float,
    win_samples: int,
    step_samples: int,
    threshold_uv: float,
) -> Tuple[np.ndarray, np.ndarray]:
    n = len(x_uv)
    if n < win_samples:
        return np.array([], dtype=float), np.array([], dtype=float)
    starts = np.arange(0, n - win_samples + 1, step_samples, dtype=int)
    p2p_vals = np.empty(starts.shape[0], dtype=float)
    for i, s in enumerate(starts):
        seg = x_uv[s : s + win_samples]
        p2p_vals[i] = float(np.max(seg) - np.min(seg))
    hit = p2p_vals > threshold_uv
    centers_sec = (starts + (win_samples / 2.0)) / sfreq
    return centers_sec[hit], p2p_vals[hit]


def detect_file_events(
    edf_path: Path,
    threshold_uv: float,
    window_sec: float,
    step_sec: float,
    verbose: bool = False,
) -> Dict:
    raw = mne.io.read_raw_edf(str(edf_path), preload=True, verbose="ERROR")
    sfreq = float(raw.info["sfreq"])
    win_samples = max(1, int(round(window_sec * sfreq)))
    step_samples = max(1, int(round(step_sec * sfreq)))
    duration_sec = float(raw.n_times / sfreq)

    file_rows: List[Dict] = []
    per_channel_events: Dict[str, np.ndarray] = {}

    eeg_channels = [ch for ch in raw.ch_names if is_eeg_channel_name(ch)]
    for ch in eeg_channels:
        x_v = raw.get_data(picks=[ch])[0]
        x_uv = x_v * 1e6
        hit_times, hit_p2p = detect_events_for_channel(
            x_uv=x_uv,
            sfreq=sfreq,
            win_samples=win_samples,
            step_samples=step_samples,
            threshold_uv=threshold_uv,
        )
        per_channel_events[ch] = hit_times
        for t, v in zip(hit_times, hit_p2p):
            file_rows.append(
                {
                    "file": str(edf_path),
                    "channel": ch,
                    "time_sec": float(t),
                    "time_min": float(t / 60.0),
                    "p2p_uv": float(v),
                }
            )
    if verbose:
        print(f"[OK] {edf_path.name}: {len(file_rows)} events")
    return {
        "file": str(edf_path),
        "duration_sec": duration_sec,
        "sfreq": sfreq,
        "window_sec": window_sec,
        "step_sec": step_sec,
        "threshold_uv": threshold_uv,
        "events": file_rows,
        "channel_event_times": per_channel_events,
        "channels": eeg_channels,
    }


def save_events_csv(events: List[Dict], out_csv: Path) -> None:
    import csv

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "channel", "time_sec", "time_min", "p2p_uv"])
        writer.writeheader()
        for r in events:
            writer.writerow(r)


def plot_timeline(
    file_name: str,
    channels: List[str],
    channel_event_times: Dict[str, np.ndarray],
    duration_sec: float,
    out_png: Path,
) -> None:
    import matplotlib.pyplot as plt

    y_pos = np.arange(len(channels))
    fig_h = max(6.0, min(0.25 * len(channels) + 2.0, 30.0))
    fig, ax = plt.subplots(figsize=(16, fig_h))
    ax.hlines(y=y_pos, xmin=0.0, xmax=duration_sec, color="#D0D0D0", linewidth=0.6, alpha=0.8)

    for i, ch in enumerate(channels):
        times = channel_event_times.get(ch, np.array([], dtype=float))
        if times.size > 0:
            # short line marks on the timeline
            ax.vlines(times, i - 0.25, i + 0.25, color="#D62728", linewidth=0.6, alpha=0.85)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(channels, fontsize=8)
    ax.set_xlim(0.0, duration_sec)
    ax.set_xlabel("Time (sec)")
    ax.set_title(f"P2P > 200 uV positions by channel: {file_name}")
    ax.grid(axis="x", linestyle="--", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_png, dpi=160)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    input_root = Path(args.input_root)
    output_root = Path(args.output_root)
    out_dir = make_output_dir(output_root)
    files = find_edf_files(input_root)
    if args.max_files and args.max_files > 0:
        files = files[: args.max_files]

    if not files:
        raise RuntimeError(f"No EDF files found under: {input_root}")

    all_events: List[Dict] = []
    summary_rows: List[Dict] = []

    for edf in files:
        result = detect_file_events(
            edf_path=edf,
            threshold_uv=args.threshold_uv,
            window_sec=args.window_sec,
            step_sec=args.step_sec,
            verbose=args.verbose,
        )
        all_events.extend(result["events"])

        rel_parent = edf.relative_to(input_root).parent
        target_dir = out_dir / rel_parent
        target_dir.mkdir(parents=True, exist_ok=True)

        stem = sanitize_name(edf.stem)
        per_file_csv = target_dir / f"{stem}_events.csv"
        save_events_csv(result["events"], per_file_csv)
        plot_timeline(
            file_name=edf.name,
            channels=result["channels"],
            channel_event_times=result["channel_event_times"],
            duration_sec=result["duration_sec"],
            out_png=target_dir / f"{stem}_timeline.png",
        )
        summary_rows.append(
            {
                "file": str(edf),
                "relative_output_dir": str(rel_parent).replace("\\", "/"),
                "events_count": len(result["events"]),
                "duration_sec": result["duration_sec"],
                "sfreq": result["sfreq"],
            }
        )

    save_events_csv(all_events, out_dir / "all_events.csv")
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "input_root": str(input_root),
                "files_processed": len(files),
                "threshold_uv": args.threshold_uv,
                "window_sec": args.window_sec,
                "step_sec": args.step_sec,
                "total_events": len(all_events),
                "per_file": summary_rows,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"Done. Output directory: {out_dir}")


if __name__ == "__main__":
    main()
