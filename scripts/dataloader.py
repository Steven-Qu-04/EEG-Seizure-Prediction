#!/usr/bin/env python
# -*- coding: utf-8 -*-

import random
from pathlib import Path
from typing import Dict, List, Tuple

import torch
from torch.utils.data import DataLoader, SubsetRandomSampler, WeightedRandomSampler

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


def _split_record_items_stratified(items: List, seed: int, val_ratio: float, test_ratio: float) -> Tuple[List, List, List]:
    rng = random.Random(seed)
    pairs = [(it, _record_pos_ratio(it)) for it in items]
    pairs.sort(key=lambda x: x[1], reverse=True)
    bins = [pairs[0::2], pairs[1::2]]
    train_items, val_items, test_items = [], [], []
    for b in bins:
        rng.shuffle(b)
        n = len(b)
        n_val = max(1, int(round(n * val_ratio))) if n >= 3 and val_ratio > 0 else 0
        n_test = max(1, int(round(n * test_ratio))) if n - n_val >= 2 and test_ratio > 0 else 0
        val_items.extend([x[0] for x in b[:n_val]])
        test_items.extend([x[0] for x in b[n_val : n_val + n_test]])
        train_items.extend([x[0] for x in b[n_val + n_test :]])

    if not train_items or not val_items or not test_items:
        shuffled = [x[0] for x in pairs]
        rng.shuffle(shuffled)
        if len(shuffled) < 3:
            raise RuntimeError("Need at least 3 records for Kaggle train/val/test split.")
        n_val = max(1, int(round(len(shuffled) * val_ratio)))
        n_test = max(1, int(round(len(shuffled) * test_ratio)))
        if n_val + n_test >= len(shuffled):
            n_val = 1
            n_test = 1
        val_items = shuffled[:n_val]
        test_items = shuffled[n_val : n_val + n_test]
        train_items = shuffled[n_val + n_test :]
    return train_items, val_items, test_items


def _build_train_sampler(train_ds: SienaWindowDataset, train_sampler: str, seed: int):
    counts = train_ds.class_counts()
    neg = int(counts["neg"])
    pos = int(counts["pos"])
    total = neg + pos
    info = {
        "strategy": train_sampler,
        "raw_class_counts": {"neg": neg, "pos": pos},
        "epoch_samples": len(train_ds),
    }

    if train_sampler == "none":
        return None, True, info
    if neg <= 0 or pos <= 0:
        raise RuntimeError(f"Balanced sampler requires both classes, got neg={neg}, pos={pos}")

    labels = [int(y) for y in train_ds.y.tolist()]
    g = torch.Generator()
    g.manual_seed(seed)

    if train_sampler == "balanced-over":
        class_weights = {0: 1.0 / neg, 1: 1.0 / pos}
        sample_weights = torch.tensor([class_weights[y] for y in labels], dtype=torch.double)
        num_samples = 2 * max(neg, pos)
        info.update(
            {
                "replacement": True,
                "epoch_samples": num_samples,
                "target_class_counts": {"neg": num_samples // 2, "pos": num_samples // 2},
            }
        )
        return WeightedRandomSampler(sample_weights, num_samples=num_samples, replacement=True, generator=g), False, info

    if train_sampler == "balanced-under":
        rng = random.Random(seed)
        neg_indices = [i for i, y in enumerate(labels) if y == 0]
        pos_indices = [i for i, y in enumerate(labels) if y == 1]
        n_each = min(len(neg_indices), len(pos_indices))
        selected = rng.sample(neg_indices, n_each) + rng.sample(pos_indices, n_each)
        rng.shuffle(selected)
        info.update(
            {
                "replacement": False,
                "epoch_samples": len(selected),
                "target_class_counts": {"neg": n_each, "pos": n_each},
            }
        )
        return SubsetRandomSampler(selected, generator=g), False, info

    raise ValueError(f"Unknown train_sampler={train_sampler}")


def build_dataloaders(
    data_root: str,
    window: str,
    version: str,
    batch_size: int,
    num_workers: int,
    seed: int,
    test_ratio: float = 0.2,
    train_sampler: str = "balanced-over",
    layout: str = "siena",
    val_ratio: float = 0.2,
) -> Dict:
    root = Path(data_root)
    items = discover_record_items(root=root, window=window, version=version, layout=layout)
    if not items:
        raise RuntimeError(f"No dataset items for window={window}, version={version}")

    if layout == "siena":
        val_items = [it for it in items if it.record in PERM_VAL]
        rem_items = [it for it in items if it.record not in PERM_VAL]
        if len(val_items) != 2:
            raise RuntimeError(f"Permanent validation requires PN06-1 and PN14-4, found {len(val_items)}")
        if len(rem_items) < 2:
            raise RuntimeError("Not enough remaining records to split train/test.")
        train_items, test_items = _split_remaining_records(rem_items, seed=seed, test_ratio=test_ratio)
    elif layout == "kaggle":
        train_items, val_items, test_items = _split_record_items_stratified(
            items,
            seed=seed,
            val_ratio=val_ratio,
            test_ratio=test_ratio,
        )
    else:
        raise ValueError(f"Unknown dataset layout: {layout}")

    if len(train_items) == 0 or len(test_items) == 0:
        raise RuntimeError("Train/test split failed: empty subset.")
    if len(val_items) == 0:
        raise RuntimeError("Validation split failed: empty subset.")

    train_ds = SienaWindowDataset(train_items)
    val_ds = SienaWindowDataset(val_items)
    test_ds = SienaWindowDataset(test_items)

    g = torch.Generator()
    g.manual_seed(seed)

    sampler, shuffle_train, sampler_info = _build_train_sampler(train_ds, train_sampler=train_sampler, seed=seed)

    train_loader = DataLoader(
        train_ds,
        batch_size=batch_size,
        shuffle=shuffle_train,
        sampler=sampler,
        num_workers=num_workers,
        generator=g,
    )
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False, num_workers=num_workers)
    test_loader = DataLoader(test_ds, batch_size=batch_size, shuffle=False, num_workers=num_workers)

    manifest = {
        "layout": layout,
        "window": window,
        "version": version,
        "train_sampler": sampler_info,
        "permanent_val_records": sorted([f"{it.subject}/{it.record}" for it in val_items]) if layout == "siena" else [],
        "val_records": sorted([f"{it.subject}/{it.record}" for it in val_items]),
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
        "train_sampler_info": sampler_info,
        "manifest": manifest,
    }
