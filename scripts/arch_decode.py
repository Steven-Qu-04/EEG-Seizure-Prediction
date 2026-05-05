#!/usr/bin/env python
# -*- coding: utf-8 -*-

from typing import Dict, List, Tuple


def get_default_search_space() -> List[List[int]]:
    """Search space aligned with decode_arch field order."""
    return [
        [4, 8, 16],         # t_out_1
        [8, 16, 32],        # t_out_2
        [8, 16, 32],        # t_out_3
        [4, 8, 16],         # t_k_1
        [4, 8, 16],         # t_k_2
        [4, 8, 16],         # t_k_3
        [1, 2, 4],          # t_pool_1
        [1, 2, 4],          # t_pool_2
        [1, 2, 4],          # t_pool_3
        [8, 16, 32],        # s_out_1
        [8, 16, 32],        # s_out_2
        [4, 8, 16],         # s_k_1
        [4, 8, 16],         # s_k_2
        [1, 2, 4],          # s_pool_1
        [1, 2, 4],          # s_pool_2
        [0, 1],             # use_batch_norm
        [0, 1],             # activation: 0->relu, 1->tanh
        [0, 1],             # pool_type: 0->max, 1->avg
        [0, 1],             # conv_bias
    ]


def decode_arch(sampled_tokens: List[int], input_channels: int = 30, num_classes: int = 2) -> Dict:
    """
    Decode sampled architecture tokens to EEGCNN kwargs.
    sampled_tokens is expected to already contain concrete values from search_space.
    """
    if len(sampled_tokens) != 19:
        raise ValueError(f"Expected 19 tokens, got {len(sampled_tokens)}")

    return {
        "input_channels": int(input_channels),
        "num_classes": int(num_classes),
        "temporal_out_channels": (int(sampled_tokens[0]), int(sampled_tokens[1]), int(sampled_tokens[2])),
        "temporal_kernel_widths": (int(sampled_tokens[3]), int(sampled_tokens[4]), int(sampled_tokens[5])),
        "temporal_pool_widths": (int(sampled_tokens[6]), int(sampled_tokens[7]), int(sampled_tokens[8])),
        "spatial_out_channels": (int(sampled_tokens[9]), int(sampled_tokens[10])),
        "spatial_kernel_heights": (int(sampled_tokens[11]), int(sampled_tokens[12])),
        "spatial_pool_heights": (int(sampled_tokens[13]), int(sampled_tokens[14])),
        "use_batch_norm": bool(sampled_tokens[15]),
        "activation": "relu" if int(sampled_tokens[16]) == 0 else "tanh",
        "pool_type": "max" if int(sampled_tokens[17]) == 0 else "avg",
        "conv_bias": bool(sampled_tokens[18]),
        "return_probabilities": False,
    }


def validate_arch_kwargs(kwargs: Dict) -> Tuple[bool, str]:
    """Validate decoded EEGCNN kwargs before model construction/training."""
    try:
        if kwargs["input_channels"] <= 0:
            return False, "input_channels must be > 0"
        if kwargs["num_classes"] < 2:
            return False, "num_classes must be >= 2"

        for name in [
            "temporal_out_channels",
            "temporal_kernel_widths",
            "temporal_pool_widths",
            "spatial_out_channels",
            "spatial_kernel_heights",
            "spatial_pool_heights",
        ]:
            values = kwargs[name]
            if not isinstance(values, tuple):
                return False, f"{name} must be tuple"
            if any(int(v) <= 0 for v in values):
                return False, f"{name} contains non-positive values"

        if kwargs["activation"] not in {"relu", "tanh"}:
            return False, "activation must be relu or tanh"
        if kwargs["pool_type"] not in {"max", "avg"}:
            return False, "pool_type must be max or avg"
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
