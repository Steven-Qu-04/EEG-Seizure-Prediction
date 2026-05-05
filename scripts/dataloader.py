#!/usr/bin/env python
# -*- coding: utf-8 -*-

import random
from pathlib import Path
from typing import Dict, List, Tuple

import torch
from torch.utils.data import DataLoader

from dataset import SienaWindowDataset, discover_record_items


PERM_VAL = {"PN06-1", "PN14-4"}


def _record_pos_ratio(item) -> float:
    ds = SienaWindowDataset([item])
    cc = ds.class_counts()
    total = cc["neg"] + cc["pos"]
    if total == 0:
        return 0.0
    return cc["pos"] / total


def _split_remaining_records(remaining_items: List, seed: int, test_ratio: float) -> Tuple[List, List]:
    rng = random.Random(seed)
    pairs = [(it, _record_pos_ratio(it)) for it in remaining_items]
    pairs.sort(key=lambda x: x[1], reverse=True)
    bins = [pairs[0::2], pairs[1::2]]
    train_items, test_items = [], []
    for b in bins:
        rng.shuffle(b)
        n_test = max(1, int(round(len(b) * test_ratio))) if len(b) > 1 else 0
        test_items.extend([x[0] for x in b[:n_test]])
        train_items.extend([x[0] for x in b[n_test:]])
    return train_items, test_items


def build_dataloaders(
    data_root: str,
    window: str,
    version: str,
    batch_size: int,
    num_workers: int,
    seed: int,
    test_ratio: float = 0.2,
) -> Dict:
    root = Path(data_root)
    items = discover_record_items(root=root, window=window, version=version)
    if not items:
        raise RuntimeError(f"No dataset items for window={window}, version={version}")

    val_items = [it for it in items if it.record in PERM_VAL]
    rem_items = [it for it in items if it.record not in PERM_VAL]
    if len(val_items) != 2:
        raise RuntimeError(f"Permanent validation requires PN06-1 and PN14-4, found {len(val_items)}")
    if len(rem_items) < 2:
        raise RuntimeError("Not enough remaining records to split train/test.")

    train_items, test_items = _split_remaining_records(rem_items, seed=seed, test_ratio=test_ratio)
    if len(train_items) == 0 or len(test_items) == 0:
        raise RuntimeError("Train/test split failed: empty subset.")

    train_ds = SienaWindowDataset(train_items)
    val_ds = SienaWindowDataset(val_items)
    test_ds = SienaWindowDataset(test_items)

    g = torch.Generator()
    g.manual_seed(seed)

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True, num_workers=num_workers, generator=g)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False, num_workers=num_workers)
    test_loader = DataLoader(test_ds, batch_size=batch_size, shuffle=False, num_workers=num_workers)

    manifest = {
        "window": window,
        "version": version,
        "permanent_val_records": sorted([f"{it.subject}/{it.record}" for it in val_items]),
        "train_records": sorted([f"{it.subject}/{it.record}" for it in train_items]),
        "test_records": sorted([f"{it.subject}/{it.record}" for it in test_items]),
        "counts": {
            "train_samples": len(train_ds),
            "val_samples": len(val_ds),
            "test_samples": len(test_ds),
        },
    }
    return {
        "train_loader": train_loader,
        "val_loader": val_loader,
        "test_loader": test_loader,
        "train_ds": train_ds,
        "val_ds": val_ds,
        "test_ds": test_ds,
        "manifest": manifest,
    }
