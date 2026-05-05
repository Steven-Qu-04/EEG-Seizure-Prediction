#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import torch
from torch.utils.data import Dataset


@dataclass
class RecordItem:
    subject: str
    record: str
    version: str
    x_path: Path
    y_path: Path
    meta_path: Path


def discover_record_items(
    root: Path,
    window: str,
    version: str,
    include_subjects: Optional[Sequence[str]] = None,
    exclude_subjects: Optional[Sequence[str]] = None,
    include_records: Optional[Sequence[str]] = None,
    exclude_records: Optional[Sequence[str]] = None,
) -> List[RecordItem]:
    include_subjects = set(include_subjects or [])
    exclude_subjects = set(exclude_subjects or [])
    include_records = set(include_records or [])
    exclude_records = set(exclude_records or [])

    base = root / "SPH5m_PIL30m" / window
    if not base.exists():
        raise FileNotFoundError(f"Window path not found: {base}")

    out: List[RecordItem] = []
    for subject_dir in sorted(base.iterdir()):
        if not subject_dir.is_dir():
            continue
        subject = subject_dir.name
        if include_subjects and subject not in include_subjects:
            continue
        if subject in exclude_subjects:
            continue
        for record_dir in sorted(subject_dir.iterdir()):
            if not record_dir.is_dir():
                continue
            record = record_dir.name
            if include_records and record not in include_records:
                continue
            if record in exclude_records:
                continue
            vdir = record_dir / version
            x_path = vdir / "X_all.npy"
            y_path = vdir / "y_all.npy"
            meta_path = vdir / "meta_windows.json"
            if x_path.exists() and y_path.exists() and meta_path.exists():
                out.append(
                    RecordItem(
                        subject=subject,
                        record=record,
                        version=version,
                        x_path=x_path,
                        y_path=y_path,
                        meta_path=meta_path,
                    )
                )
    return out


class SienaWindowDataset(Dataset):
    """Window-level dataset merged from multiple record folders."""

    def __init__(self, items: List[RecordItem]):
        self.items = items
        self.X = []
        self.y = []
        self.index_map: List[Dict] = []
        self.record_to_range: Dict[str, Dict] = {}
        cursor = 0

        for it in self.items:
            x = np.load(it.x_path)
            y = np.load(it.y_path)
            if x.shape[0] != y.shape[0]:
                raise ValueError(f"X/Y mismatch for {it.subject}/{it.record}")
            self.X.append(x.astype(np.float32, copy=False))
            self.y.append(y.astype(np.int64, copy=False))
            n = int(y.shape[0])
            key = f"{it.subject}/{it.record}"
            self.record_to_range[key] = {"start": cursor, "end": cursor + n}
            for i in range(n):
                self.index_map.append({"subject": it.subject, "record": it.record, "local_index": i})
            cursor += n

        if self.X:
            # Different records can have different channel counts (e.g. 31 vs 32).
            # Align channels to the minimum common count so arrays can be merged.
            min_c = min(int(x.shape[1]) for x in self.X)
            seq_lens = {int(x.shape[2]) for x in self.X}
            if len(seq_lens) != 1:
                raise ValueError(f"Inconsistent sequence length across records: {sorted(seq_lens)}")
            self.X = [x[:, :min_c, :] for x in self.X]
            self.X = np.concatenate(self.X, axis=0)
            self.y = np.concatenate(self.y, axis=0)
        else:
            self.X = np.zeros((0, 1, 1), dtype=np.float32)
            self.y = np.zeros((0,), dtype=np.int64)

    def __len__(self) -> int:
        return int(self.y.shape[0])

    def __getitem__(self, idx: int):
        x = torch.from_numpy(self.X[idx])
        y = torch.tensor(self.y[idx], dtype=torch.long)
        return x, y

    def num_channels(self) -> int:
        return int(self.X.shape[1])

    def seq_len(self) -> int:
        return int(self.X.shape[2])

    def class_counts(self) -> Dict[str, int]:
        if len(self.y) == 0:
            return {"neg": 0, "pos": 0}
        pos = int((self.y == 1).sum())
        neg = int((self.y == 0).sum())
        return {"neg": neg, "pos": pos}
