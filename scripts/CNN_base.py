"""
Usage example:

from model import EEGCNN

model = EEGCNN(
    input_channels=30,
    num_classes=2,
    temporal_out_channels=(4, 16, 16),
    temporal_kernel_widths=(8, 16, 8),
    temporal_pool_widths=(8, 4, 4),
    spatial_out_channels=(16, 16),
    spatial_kernel_heights=(16, 16),
    spatial_pool_heights=(4, 4),
    use_batch_norm=True,
    activation="relu",
    pool_type="max",
    conv_bias=False,
    return_probabilities=False,
)

Input tensor shape:
    x.shape = (batch_size, input_channels, T)

Forward:
    logits = model(x)

Notes:
    - T is variable because the model uses global average pooling.
    - All architecture hyperparameters must be passed from the training script.
    - The model returns logits by default.
    - Set return_probabilities=True only if softmax probabilities are required.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


class Conv2dSame(nn.Module):
    """
    2D convolution with dynamic SAME padding.
    The convolution itself does not reduce feature-map size.
    """

    def __init__(self, in_channels, out_channels, kernel_size, bias):
        super().__init__()
        self.kernel_size = kernel_size
        self.conv = nn.Conv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=1,
            padding=0,
            bias=bias,
        )

    def forward(self, x):
        kh, kw = self.kernel_size

        pad_h = kh - 1
        pad_w = kw - 1

        pad_top = pad_h // 2
        pad_bottom = pad_h - pad_top
        pad_left = pad_w // 2
        pad_right = pad_w - pad_left

        x = F.pad(x, (pad_left, pad_right, pad_top, pad_bottom))
        return self.conv(x)


class ConvBlock(nn.Module):
    """
    Convolution + optional BatchNorm + activation + pooling block.
    """

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size,
        pool_size,
        use_batch_norm,
        activation,
        pool_type,
        conv_bias,
    ):
        super().__init__()

        layers = [
            Conv2dSame(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=kernel_size,
                bias=conv_bias,
            )
        ]

        if use_batch_norm:
            layers.append(nn.BatchNorm2d(out_channels))

        if activation == "relu":
            layers.append(nn.ReLU(inplace=True))
        elif activation == "tanh":
            layers.append(nn.Tanh())
        elif activation == "none":
            pass
        else:
            raise ValueError(f"Unsupported activation: {activation}")

        if pool_type == "max":
            layers.append(nn.MaxPool2d(kernel_size=pool_size))
        elif pool_type == "avg":
            layers.append(nn.AvgPool2d(kernel_size=pool_size))
        elif pool_type == "none":
            pass
        else:
            raise ValueError(f"Unsupported pool_type: {pool_type}")

        self.block = nn.Sequential(*layers)

    def forward(self, x):
        return self.block(x)


class EEGCNN(nn.Module):
    """
    Non-quantized CNN model.

    Input shape:
        x: (batch_size, C, T)

    The temporal and spatial hyperparameters are passed from
    an external training/config script.
    """

    def __init__(
        self,
        input_channels,
        num_classes,
        temporal_out_channels,
        temporal_kernel_widths,
        temporal_pool_widths,
        spatial_out_channels,
        spatial_kernel_heights,
        spatial_pool_heights,
        use_batch_norm,
        activation,
        pool_type,
        conv_bias,
        return_probabilities,
    ):
        super().__init__()

        self.input_channels = input_channels
        self.num_classes = num_classes
        self.return_probabilities = return_probabilities

        if not (
            len(temporal_out_channels)
            == len(temporal_kernel_widths)
            == len(temporal_pool_widths)
        ):
            raise ValueError("Temporal hyperparameter lists must have the same length.")

        if not (
            len(spatial_out_channels)
            == len(spatial_kernel_heights)
            == len(spatial_pool_heights)
        ):
            raise ValueError("Spatial hyperparameter lists must have the same length.")

        blocks = []
        in_ch = 1

        # Temporal blocks: convolution kernel shape is 1 x k.
        for out_ch, kernel_w, pool_w in zip(
            temporal_out_channels,
            temporal_kernel_widths,
            temporal_pool_widths,
        ):
            blocks.append(
                ConvBlock(
                    in_channels=in_ch,
                    out_channels=out_ch,
                    kernel_size=(1, kernel_w),
                    pool_size=(1, pool_w),
                    use_batch_norm=use_batch_norm,
                    activation=activation,
                    pool_type=pool_type,
                    conv_bias=conv_bias,
                )
            )
            in_ch = out_ch

        # Spatial blocks: convolution kernel shape is k x 1.
        for out_ch, kernel_h, pool_h in zip(
            spatial_out_channels,
            spatial_kernel_heights,
            spatial_pool_heights,
        ):
            blocks.append(
                ConvBlock(
                    in_channels=in_ch,
                    out_channels=out_ch,
                    kernel_size=(kernel_h, 1),
                    pool_size=(pool_h, 1),
                    use_batch_norm=use_batch_norm,
                    activation=activation,
                    pool_type=pool_type,
                    conv_bias=conv_bias,
                )
            )
            in_ch = out_ch

        self.feature_extractor = nn.Sequential(*blocks)

        # Global average pooling allows variable-length T.
        self.global_pool = nn.AdaptiveAvgPool2d((1, 1))

        self.classifier = nn.Linear(in_ch, num_classes)

    def forward(self, x):
        """
        Forward pass.

        Args:
            x: Tensor with shape (batch_size, C, T)

        Returns:
            logits or probabilities with shape (batch_size, num_classes)
        """

        # Convert to Conv2D input format: (B, 1, C, T)
        x = x.unsqueeze(1)

        x = self.feature_extractor(x)
        x = self.global_pool(x)
        x = torch.flatten(x, start_dim=1)

        logits = self.classifier(x)

        if self.return_probabilities:
            return torch.softmax(logits, dim=1)

        return logits
