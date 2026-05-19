from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

DEFAULT_CURRENT_CHECKPOINT = (
    REPO_ROOT / "outputs" / "nas_cnn_8bit_runs" / "20260516_190459_win10s_v1" / "best_1.pt"
)
DEFAULT_CURRENT_ARCH_PATH = (
    REPO_ROOT / "outputs" / "nas_cnn_8bit_runs" / "20260516_190459_win10s_v1" / "best_1_arch.json"
)
DEFAULT_ORIG_CHECKPOINT = (
    REPO_ROOT / "outputs" / "nas_cnn_runs" / "20260505_123644_win10s_v1" / "best_1.pt"
)
DEFAULT_ORIG_ARCH_PATH = (
    REPO_ROOT / "outputs" / "nas_cnn_runs" / "20260505_123644_win10s_v1" / "best_1_arch.json"
)
DEFAULT_CHECKPOINT = DEFAULT_CURRENT_CHECKPOINT
DEFAULT_ARCH_PATH = DEFAULT_CURRENT_ARCH_PATH
DEFAULT_OUTPUT_DIR = REPO_ROOT / "deploy_executorch" / "dist"
DEFAULT_EXAMPLE_SHAPE = (1, 31, 5120)
FIXED_DEPLOYMENT_FIELDS = {
    "input_channels": 31,
    "num_classes": 2,
    "return_probabilities": False,
}
REQUIRED_ARCH_KEYS = (
    "input_channels",
    "num_classes",
    "temporal_out_channels",
    "temporal_kernel_widths",
    "temporal_pool_widths",
    "spatial_out_channels",
    "spatial_kernel_heights",
    "spatial_pool_heights",
    "use_batch_norm",
    "activation",
    "pool_type",
    "conv_bias",
)


def require_torch():
    try:
        import torch
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyTorch is required for export/validation. Install the environment from env.yml "
            "or activate a Python environment that contains torch."
        ) from exc
    return torch


def require_numpy():
    try:
        import numpy as np
    except ModuleNotFoundError as exc:
        raise RuntimeError("NumPy is required to write golden binary files.") from exc
    return np


def read_arch_config(arch_path: Path) -> dict[str, Any]:
    arch = json.loads(arch_path.read_text(encoding="utf-8"))
    missing_keys = [key for key in REQUIRED_ARCH_KEYS if key not in arch]
    if missing_keys:
        raise ValueError(f"Architecture file is missing required keys: {missing_keys}")

    arch.setdefault("return_probabilities", False)
    for key, expected_value in FIXED_DEPLOYMENT_FIELDS.items():
        actual_value = arch.get(key)
        if actual_value != expected_value:
            raise ValueError(
                f"Architecture mismatch for {key}: expected {expected_value!r}, got {actual_value!r}"
            )
    return arch


def load_training_checkpoint(checkpoint_path: Path, arch_path: Path):
    torch = require_torch()
    from deploy_executorch.deploy_model import build_deploy_model

    if not checkpoint_path.exists():
        raise FileNotFoundError(f"Checkpoint not found: {checkpoint_path}")
    if not arch_path.exists():
        raise FileNotFoundError(f"Architecture file not found: {arch_path}")

    arch_config = read_arch_config(arch_path)
    model = build_deploy_model(arch_config)
    state_dict = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    model.load_state_dict(state_dict, strict=True)
    model.eval()
    return model


def parse_example_shape(shape_text: str) -> tuple[int, int, int]:
    parts = tuple(int(part) for part in shape_text.split(","))
    if len(parts) != 3:
        raise ValueError(f"Expected example shape as B,C,T, got: {shape_text}")
    if parts != DEFAULT_EXAMPLE_SHAPE:
        raise ValueError(
            f"First version only supports fixed shape {DEFAULT_EXAMPLE_SHAPE}, got {parts}"
        )
    return parts


def save_tensor_bin(tensor, path: Path) -> None:
    np = require_numpy()
    array = tensor.detach().cpu().contiguous().numpy().astype(np.float32, copy=False)
    array.tofile(path)
