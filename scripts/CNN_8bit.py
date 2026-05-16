import torch
import torch.nn as nn
import torch.nn.functional as F


def quantize_ste(x: torch.Tensor, bits: int = 8) -> torch.Tensor:
    """
    Signed fixed-point quantizer with straight-through estimator.

    Forward:
        clip -> round to fixed-point grid

    Backward:
        gradients flow through the clipped tensor.
    """
    scale = 2 ** (bits - 1)
    qmin = -1.0
    qmax = 1.0 - 1.0 / scale

    x_clip = torch.clamp(x, qmin, qmax)
    x_q = torch.round(x_clip * scale) / scale
    return x_clip + (x_q - x_clip).detach()


class QuantConv2d(nn.Conv2d):
    def __init__(self, *args, bits: int = 8, **kwargs):
        super().__init__(*args, **kwargs)
        self.bits = bits

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        w_q = quantize_ste(self.weight, bits=self.bits)
        return F.conv2d(
            x,
            w_q,
            self.bias,
            stride=self.stride,
            padding=self.padding,
            dilation=self.dilation,
            groups=self.groups,
        )


class QuantLinear(nn.Linear):
    def __init__(self, *args, bits: int = 8, **kwargs):
        super().__init__(*args, **kwargs)
        self.bits = bits

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        w_q = quantize_ste(self.weight, bits=self.bits)
        return F.linear(x, w_q, self.bias)


class QuantAct(nn.Module):
    def __init__(self, bits: int = 8):
        super().__init__()
        self.bits = bits

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return quantize_ste(x, bits=self.bits)


class QuantConv2dSame(nn.Module):
    """
    2D convolution with dynamic SAME padding and quantized weights.
    """

    def __init__(self, in_channels, out_channels, kernel_size, bias, bits: int = 8):
        super().__init__()
        self.kernel_size = kernel_size
        self.conv = QuantConv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=1,
            padding=0,
            bias=bias,
            bits=bits,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        kh, kw = self.kernel_size

        pad_h = kh - 1
        pad_w = kw - 1

        pad_top = pad_h // 2
        pad_bottom = pad_h - pad_top
        pad_left = pad_w // 2
        pad_right = pad_w - pad_left

        x = F.pad(x, (pad_left, pad_right, pad_top, pad_bottom))
        return self.conv(x)


class QuantConvBlock(nn.Module):
    """
    Quantized convolution + optional BatchNorm + activation + QuantAct + pooling.
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
        bits: int = 8,
    ):
        super().__init__()

        layers = [
            QuantConv2dSame(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=kernel_size,
                bias=conv_bias,
                bits=bits,
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

        layers.append(QuantAct(bits=bits))

        if pool_type == "max":
            layers.append(nn.MaxPool2d(kernel_size=pool_size))
        elif pool_type == "avg":
            layers.append(nn.AvgPool2d(kernel_size=pool_size))
        elif pool_type == "none":
            pass
        else:
            raise ValueError(f"Unsupported pool_type: {pool_type}")

        self.block = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.block(x)


class EEGCNN8bit(nn.Module):
    """
    NAS-compatible 8-bit QAT CNN.

    Input shape:
        x: (batch_size, C, T)
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
        bits: int = 8,
    ):
        super().__init__()

        self.input_channels = input_channels
        self.num_classes = num_classes
        self.return_probabilities = return_probabilities
        self.bits = bits
        self.input_quant = QuantAct(bits=bits)

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

        for out_ch, kernel_w, pool_w in zip(
            temporal_out_channels,
            temporal_kernel_widths,
            temporal_pool_widths,
        ):
            blocks.append(
                QuantConvBlock(
                    in_channels=in_ch,
                    out_channels=out_ch,
                    kernel_size=(1, kernel_w),
                    pool_size=(1, pool_w),
                    use_batch_norm=use_batch_norm,
                    activation=activation,
                    pool_type=pool_type,
                    conv_bias=conv_bias,
                    bits=bits,
                )
            )
            in_ch = out_ch

        for out_ch, kernel_h, pool_h in zip(
            spatial_out_channels,
            spatial_kernel_heights,
            spatial_pool_heights,
        ):
            blocks.append(
                QuantConvBlock(
                    in_channels=in_ch,
                    out_channels=out_ch,
                    kernel_size=(kernel_h, 1),
                    pool_size=(pool_h, 1),
                    use_batch_norm=use_batch_norm,
                    activation=activation,
                    pool_type=pool_type,
                    conv_bias=conv_bias,
                    bits=bits,
                )
            )
            in_ch = out_ch

        self.feature_extractor = nn.Sequential(*blocks)
        self.global_pool = nn.AdaptiveAvgPool2d((1, 1))
        self.classifier = QuantLinear(in_ch, num_classes, bits=bits)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.unsqueeze(1)
        x = self.input_quant(x)
        x = self.feature_extractor(x)
        x = self.global_pool(x)
        x = torch.flatten(x, start_dim=1)

        logits = self.classifier(x)
        if self.return_probabilities:
            return torch.softmax(logits, dim=1)
        return logits


def export_int8_weight(w: torch.Tensor, bits: int = 8) -> torch.Tensor:
    scale = 2 ** (bits - 1)
    z = torch.round(w * scale)
    z = torch.clamp(z, -scale, scale - 1)
    return z.to(torch.int8)


def export_int8_model(model: nn.Module, bits: int = 8):
    int8_state = {}

    for name, module in model.named_modules():
        if isinstance(module, (QuantConv2d, QuantLinear)):
            int8_state[name + ".weight_int8"] = export_int8_weight(
                module.weight.detach().cpu(),
                bits=bits,
            )
            if module.bias is not None:
                int8_state[name + ".bias_fp32"] = module.bias.detach().cpu()

        if isinstance(module, nn.BatchNorm2d):
            int8_state[name + ".bn_weight_fp32"] = module.weight.detach().cpu()
            int8_state[name + ".bn_bias_fp32"] = module.bias.detach().cpu()
            int8_state[name + ".bn_mean_fp32"] = module.running_mean.detach().cpu()
            int8_state[name + ".bn_var_fp32"] = module.running_var.detach().cpu()

    return int8_state
