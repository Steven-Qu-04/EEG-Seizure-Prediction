from __future__ import annotations

from typing import Iterable, Sequence

import torch
import torch.nn as nn


DEFAULT_ARCH = {
    "input_channels": 31,
    "num_classes": 2,
    "temporal_out_channels": [4, 8, 8],
    "temporal_kernel_widths": [4, 8, 16],
    "temporal_pool_widths": [2, 1, 2],
    "spatial_out_channels": [8, 8],
    "spatial_kernel_heights": [8, 16],
    "spatial_pool_heights": [4, 1],
    "use_batch_norm": True,
    "activation": "relu",
    "pool_type": "avg",
    "conv_bias": False,
    "return_probabilities": False,
}


def _ensure_same_length(*groups: Iterable[object]) -> None:
    lengths = {len(group) for group in groups}
    if len(lengths) != 1:
        raise ValueError(f"Expected matching lengths, got {sorted(lengths)}")


def _same_pad(kernel_size: tuple[int, int]) -> tuple[int, int, int, int]:
    kh, kw = kernel_size
    pad_h = kh - 1
    pad_w = kw - 1
    pad_top = pad_h // 2
    pad_bottom = pad_h - pad_top
    pad_left = pad_w // 2
    pad_right = pad_w - pad_left
    return (pad_left, pad_right, pad_top, pad_bottom)


class FixedSamePadConv2d(nn.Module):
    """Convolution with explicit static SAME-style padding."""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: tuple[int, int],
        pad: tuple[int, int, int, int],
        bias: bool,
    ) -> None:
        super().__init__()
        self.pad = nn.ConstantPad2d(pad, 0.0)
        self.conv = nn.Conv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=1,
            padding=0,
            bias=bias,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.pad(x)
        return self.conv(x)


class DeployConvBlock(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: tuple[int, int],
        pad: tuple[int, int, int, int],
        pool_size: tuple[int, int],
        use_batch_norm: bool,
        activation: str,
        pool_type: str,
        conv_bias: bool,
    ) -> None:
        super().__init__()

        layers: list[nn.Module] = [
            FixedSamePadConv2d(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=kernel_size,
                pad=pad,
                bias=conv_bias,
            )
        ]

        if use_batch_norm:
            layers.append(nn.BatchNorm2d(out_channels))

        if activation == "relu":
            layers.append(nn.ReLU(inplace=False))
        elif activation == "tanh":
            layers.append(nn.Tanh())
        elif activation != "none":
            raise ValueError(f"Unsupported activation: {activation}")

        if pool_type == "max":
            layers.append(nn.MaxPool2d(kernel_size=pool_size))
        elif pool_type == "avg":
            layers.append(nn.AvgPool2d(kernel_size=pool_size))
        elif pool_type != "none":
            raise ValueError(f"Unsupported pool_type: {pool_type}")

        self.block = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.block(x)


class EEGCNNDeploy(nn.Module):
    """Deployment-only CNN with explicit static padding."""

    def __init__(
        self,
        input_channels: int,
        num_classes: int,
        temporal_out_channels: Sequence[int],
        temporal_kernel_widths: Sequence[int],
        temporal_pool_widths: Sequence[int],
        spatial_out_channels: Sequence[int],
        spatial_kernel_heights: Sequence[int],
        spatial_pool_heights: Sequence[int],
        use_batch_norm: bool,
        activation: str,
        pool_type: str,
        conv_bias: bool,
        return_probabilities: bool,
    ) -> None:
        super().__init__()
        self.input_channels = input_channels
        self.num_classes = num_classes
        self.return_probabilities = return_probabilities

        _ensure_same_length(
            temporal_out_channels,
            temporal_kernel_widths,
            temporal_pool_widths,
        )
        _ensure_same_length(
            spatial_out_channels,
            spatial_kernel_heights,
            spatial_pool_heights,
        )

        blocks: list[nn.Module] = []
        in_ch = 1

        for out_ch, kernel_w, pool_w in zip(
            temporal_out_channels,
            temporal_kernel_widths,
            temporal_pool_widths,
        ):
            blocks.append(
                DeployConvBlock(
                    in_channels=in_ch,
                    out_channels=out_ch,
                    kernel_size=(1, kernel_w),
                    pad=_same_pad((1, kernel_w)),
                    pool_size=(1, pool_w),
                    use_batch_norm=use_batch_norm,
                    activation=activation,
                    pool_type=pool_type,
                    conv_bias=conv_bias,
                )
            )
            in_ch = out_ch

        for out_ch, kernel_h, pool_h in zip(
            spatial_out_channels,
            spatial_kernel_heights,
            spatial_pool_heights,
        ):
            blocks.append(
                DeployConvBlock(
                    in_channels=in_ch,
                    out_channels=out_ch,
                    kernel_size=(kernel_h, 1),
                    pad=_same_pad((kernel_h, 1)),
                    pool_size=(pool_h, 1),
                    use_batch_norm=use_batch_norm,
                    activation=activation,
                    pool_type=pool_type,
                    conv_bias=conv_bias,
                )
            )
            in_ch = out_ch

        self.feature_extractor = nn.Sequential(*blocks)
        self.global_pool = nn.AdaptiveAvgPool2d((1, 1))
        self.classifier = nn.Linear(in_ch, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.unsqueeze(1)
        x = self.feature_extractor(x)
        x = self.global_pool(x)
        x = torch.flatten(x, start_dim=1)
        logits = self.classifier(x)
        if self.return_probabilities:
            return torch.softmax(logits, dim=1)
        return logits


def build_deploy_model(arch_config: dict) -> nn.Module:
    expected = DEFAULT_ARCH.copy()
    expected.update(arch_config)
    return EEGCNNDeploy(**expected)
