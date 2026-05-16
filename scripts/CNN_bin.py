import torch
import torch.nn as nn
import torch.nn.functional as F


def binarize_ste(x: torch.Tensor) -> torch.Tensor:
    """
    Binary quantizer with straight-through estimator.

    Forward:
        sign(x) -> {-1, +1}

    Backward:
        gradients flow through the clipped tensor.
    """
    x_clip = torch.clamp(x, -1.0, 1.0)
    x_b = torch.where(
        x_clip >= 0,
        torch.ones_like(x_clip),
        -torch.ones_like(x_clip),
    )
    return x_clip + (x_b - x_clip).detach()


class BinaryConv2d(nn.Conv2d):
    def __init__(
        self,
        *args,
        binarize_input: bool = True,
        binarize_weight: bool = True,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.binarize_input = binarize_input
        self.binarize_weight = binarize_weight

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.binarize_input:
            x = binarize_ste(x)

        if self.binarize_weight:
            w = binarize_ste(self.weight)
        else:
            w = self.weight

        return F.conv2d(
            x,
            w,
            self.bias,
            stride=self.stride,
            padding=self.padding,
            dilation=self.dilation,
            groups=self.groups,
        )


class BinaryLinear(nn.Linear):
    def __init__(
        self,
        *args,
        binarize_input: bool = True,
        binarize_weight: bool = True,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.binarize_input = binarize_input
        self.binarize_weight = binarize_weight

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.binarize_input:
            x = binarize_ste(x)

        if self.binarize_weight:
            w = binarize_ste(self.weight)
        else:
            w = self.weight

        return F.linear(x, w, self.bias)


class BinaryAct(nn.Module):
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return binarize_ste(x)


class BinaryConv2dSame(nn.Module):
    """
    2D convolution with dynamic SAME padding and optional binary weight/input.
    """

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size,
        bias,
        binarize_input: bool = True,
        binarize_weight: bool = True,
    ):
        super().__init__()
        self.kernel_size = kernel_size
        self.conv = BinaryConv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=1,
            padding=0,
            bias=bias,
            binarize_input=binarize_input,
            binarize_weight=binarize_weight,
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


class BinaryConvBlock(nn.Module):
    """
    Binary block with FP32 first-layer option.
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
        first_block: bool = False,
    ):
        super().__init__()
        self.first_block = first_block
        self.activation = activation

        layers = [
            BinaryConv2dSame(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=kernel_size,
                bias=conv_bias,
                binarize_input=not first_block,
                binarize_weight=not first_block,
            )
        ]

        if use_batch_norm:
            layers.append(nn.BatchNorm2d(out_channels))

        self.pre_pool = nn.Sequential(*layers)
        self.binary_act = BinaryAct()

        if pool_type == "max":
            self.pool = nn.MaxPool2d(kernel_size=pool_size)
        elif pool_type == "avg":
            self.pool = nn.AvgPool2d(kernel_size=pool_size)
        elif pool_type == "none":
            self.pool = nn.Identity()
        else:
            raise ValueError(f"Unsupported pool_type: {pool_type}")

    def _apply_first_block_activation(self, x: torch.Tensor) -> torch.Tensor:
        if self.activation == "relu":
            return F.relu(x, inplace=True)
        if self.activation == "tanh":
            return torch.tanh(x)
        if self.activation == "none":
            return x
        raise ValueError(f"Unsupported activation: {self.activation}")

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.pre_pool(x)
        if self.first_block:
            x = self._apply_first_block_activation(x)
        else:
            x = self.binary_act(x)
        x = self.pool(x)
        return x


class EEGCNNBinary(nn.Module):
    """
    NAS-compatible binary QAT CNN with FP32 first block and FP32 classifier.

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

        temporal_specs = list(
            zip(
                temporal_out_channels,
                temporal_kernel_widths,
                temporal_pool_widths,
            )
        )
        spatial_specs = list(
            zip(
                spatial_out_channels,
                spatial_kernel_heights,
                spatial_pool_heights,
            )
        )

        for idx, (out_ch, kernel_w, pool_w) in enumerate(temporal_specs):
            blocks.append(
                BinaryConvBlock(
                    in_channels=in_ch,
                    out_channels=out_ch,
                    kernel_size=(1, kernel_w),
                    pool_size=(1, pool_w),
                    use_batch_norm=use_batch_norm,
                    activation=activation,
                    pool_type=pool_type,
                    conv_bias=conv_bias,
                    first_block=(idx == 0),
                )
            )
            in_ch = out_ch

        for out_ch, kernel_h, pool_h in spatial_specs:
            blocks.append(
                BinaryConvBlock(
                    in_channels=in_ch,
                    out_channels=out_ch,
                    kernel_size=(kernel_h, 1),
                    pool_size=(pool_h, 1),
                    use_batch_norm=use_batch_norm,
                    activation=activation,
                    pool_type=pool_type,
                    conv_bias=conv_bias,
                    first_block=False,
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


def export_binary_weight(w: torch.Tensor) -> torch.Tensor:
    return (w >= 0).to(torch.uint8)


def export_binary_model(model: nn.Module):
    binary_state = {}

    for name, module in model.named_modules():
        if isinstance(module, BinaryConv2d) and module.binarize_weight:
            binary_state[name + ".weight_bin"] = export_binary_weight(module.weight.detach().cpu())
            if module.bias is not None:
                binary_state[name + ".bias_fp32"] = module.bias.detach().cpu()

        if isinstance(module, BinaryLinear) and module.binarize_weight:
            binary_state[name + ".weight_bin"] = export_binary_weight(module.weight.detach().cpu())
            if module.bias is not None:
                binary_state[name + ".bias_fp32"] = module.bias.detach().cpu()

        if isinstance(module, nn.BatchNorm2d):
            binary_state[name + ".bn_weight_fp32"] = module.weight.detach().cpu()
            binary_state[name + ".bn_bias_fp32"] = module.bias.detach().cpu()
            binary_state[name + ".bn_mean_fp32"] = module.running_mean.detach().cpu()
            binary_state[name + ".bn_var_fp32"] = module.running_var.detach().cpu()

    return binary_state
