from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from deploy_executorch.tools.common import (
    DEFAULT_ARCH_PATH,
    DEFAULT_CHECKPOINT,
    DEFAULT_EXAMPLE_SHAPE,
    DEFAULT_ORIG_ARCH_PATH,
    DEFAULT_ORIG_CHECKPOINT,
    DEFAULT_OUTPUT_DIR,
    load_training_checkpoint,
    parse_example_shape,
    require_torch,
    save_tensor_bin,
)


def _load_executorch_export_stack():
    try:
        from executorch.backends.xnnpack.partition.xnnpack_partitioner import XnnpackPartitioner
        from executorch.exir import to_edge_transform_and_lower
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "ExecuTorch export dependencies are missing. Install executorch in the Python export environment."
        ) from exc
    return XnnpackPartitioner, to_edge_transform_and_lower


def _load_quantization_stack():
    require_torch()
    try:
        from executorch.backends.xnnpack.quantizer.xnnpack_quantizer import (
            XNNPACKQuantizer,
            get_symmetric_quantization_config,
        )
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "ExecuTorch XNNPACK quantizer modules are missing. "
            "Use an environment that includes the ExecuTorch Python tooling."
        ) from exc

    try:
        from torchao.quantization.pt2e.quantize_pt2e import convert_pt2e, prepare_pt2e
    except ModuleNotFoundError as exc:
        raise RuntimeError("torchao PT2E quantization support is required for int8 export.") from exc
    return XNNPACKQuantizer, get_symmetric_quantization_config, prepare_pt2e, convert_pt2e


def _write_pte(program, path: Path) -> None:
    with path.open("wb") as f:
        program.write_to_file(f)


def _build_example_input(example_shape: tuple[int, int, int]):
    torch = require_torch()
    generator = torch.Generator().manual_seed(0)
    return torch.randn(*example_shape, generator=generator, dtype=torch.float32)


def _export_fp32_program(model, example_input, XnnpackPartitioner, to_edge_transform_and_lower):
    torch = require_torch()
    exported_program = torch.export.export(model, (example_input,), strict=False)
    edge_program = to_edge_transform_and_lower(
        exported_program,
        partitioner=[XnnpackPartitioner()],
    )
    return edge_program.to_executorch()


def _export_int8_program(
    model,
    example_input,
    XNNPACKQuantizer,
    get_symmetric_quantization_config,
    prepare_pt2e,
    convert_pt2e,
    XnnpackPartitioner,
    to_edge_transform_and_lower,
):
    torch = require_torch()
    aten_model = torch.export.export(model, (example_input,), strict=False).module()
    quantizer = XNNPACKQuantizer()
    quantizer.set_global(get_symmetric_quantization_config(is_per_channel=True))
    prepared = prepare_pt2e(aten_model, quantizer)
    with torch.inference_mode():
        prepared(example_input)
    quantized_model = convert_pt2e(prepared)
    quantized_export = torch.export.export(quantized_model, (example_input,), strict=False)
    edge_program = to_edge_transform_and_lower(
        quantized_export,
        partitioner=[XnnpackPartitioner()],
    )
    return edge_program.to_executorch()


def export_fp32_artifact(
    checkpoint_path: Path,
    arch_path: Path,
    output_path: Path,
    example_shape: tuple[int, int, int] = DEFAULT_EXAMPLE_SHAPE,
    reference_path: Path | None = None,
) -> Path:
    torch = require_torch()
    XnnpackPartitioner, to_edge_transform_and_lower = _load_executorch_export_stack()

    model = load_training_checkpoint(checkpoint_path=checkpoint_path, arch_path=arch_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    example_input = _build_example_input(example_shape)

    fp32_program = _export_fp32_program(
        model=model,
        example_input=example_input,
        XnnpackPartitioner=XnnpackPartitioner,
        to_edge_transform_and_lower=to_edge_transform_and_lower,
    )
    _write_pte(fp32_program, output_path)

    if reference_path is not None:
        torch.save(
            {
                "checkpoint_path": str(checkpoint_path),
                "arch_path": str(arch_path),
                "example_shape": example_shape,
                "state_dict": model.state_dict(),
            },
            reference_path,
        )
    return output_path


def export_to_pte(
    checkpoint_path: Path,
    arch_path: Path,
    output_dir: Path,
    example_shape: tuple[int, int, int] = DEFAULT_EXAMPLE_SHAPE,
) -> Path:
    torch = require_torch()
    XNNPACKQuantizer, get_symmetric_quantization_config, prepare_pt2e, convert_pt2e = (
        _load_quantization_stack()
    )
    XnnpackPartitioner, to_edge_transform_and_lower = _load_executorch_export_stack()

    model = load_training_checkpoint(checkpoint_path=checkpoint_path, arch_path=arch_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    example_input = _build_example_input(example_shape)
    with torch.inference_mode():
        eager_output = model(example_input)

    fp32_program = _export_fp32_program(
        model=model,
        example_input=example_input,
        XnnpackPartitioner=XnnpackPartitioner,
        to_edge_transform_and_lower=to_edge_transform_and_lower,
    )
    int8_program = _export_int8_program(
        model=model,
        example_input=example_input,
        XNNPACKQuantizer=XNNPACKQuantizer,
        get_symmetric_quantization_config=get_symmetric_quantization_config,
        prepare_pt2e=prepare_pt2e,
        convert_pt2e=convert_pt2e,
        XnnpackPartitioner=XnnpackPartitioner,
        to_edge_transform_and_lower=to_edge_transform_and_lower,
    )

    fp32_model_path = output_dir / "model_fp32.pte"
    int8_model_path = output_dir / "model_int8.pte"
    model_path = output_dir / "model.pte"
    _write_pte(fp32_program, fp32_model_path)
    _write_pte(int8_program, int8_model_path)
    shutil.copyfile(int8_model_path, model_path)

    golden_path = output_dir / "golden.pt"
    torch.save({"input": example_input, "output": eager_output}, golden_path)
    torch.save(
        {
            "checkpoint_path": str(checkpoint_path),
            "arch_path": str(arch_path),
            "example_shape": example_shape,
            "state_dict": model.state_dict(),
        },
        output_dir / "model_fp32_ref.pt",
    )
    save_tensor_bin(example_input, output_dir / "golden_input.bin")
    save_tensor_bin(eager_output, output_dir / "golden_output.bin")
    return model_path


def export_default_artifacts(
    output_dir: Path,
    example_shape: tuple[int, int, int] = DEFAULT_EXAMPLE_SHAPE,
    current_checkpoint: Path = DEFAULT_CHECKPOINT,
    current_arch: Path = DEFAULT_ARCH_PATH,
    orig_checkpoint: Path = DEFAULT_ORIG_CHECKPOINT,
    orig_arch: Path = DEFAULT_ORIG_ARCH_PATH,
) -> dict[str, Path]:
    default_model_path = export_to_pte(
        checkpoint_path=current_checkpoint,
        arch_path=current_arch,
        output_dir=output_dir,
        example_shape=example_shape,
    )
    orig_fp32_path = export_fp32_artifact(
        checkpoint_path=orig_checkpoint,
        arch_path=orig_arch,
        output_path=output_dir / "model_orig_fp32.pte",
        example_shape=example_shape,
        reference_path=output_dir / "model_orig_fp32_ref.pt",
    )
    return {
        "curr_int8": default_model_path,
        "curr_fp32": output_dir / "model_fp32.pte",
        "orig_fp32": orig_fp32_path,
        "golden_input": output_dir / "golden_input.bin",
        "golden_output": output_dir / "golden_output.bin",
    }


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export the fixed-shape EEG CNN to ExecuTorch .pte")
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--arch", type=Path, default=DEFAULT_ARCH_PATH)
    parser.add_argument("--orig-checkpoint", type=Path, default=DEFAULT_ORIG_CHECKPOINT)
    parser.add_argument("--orig-arch", type=Path, default=DEFAULT_ORIG_ARCH_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--example-shape", default="1,31,5120")
    return parser


def main() -> None:
    try:
        args = build_argparser().parse_args()
        example_shape = parse_example_shape(args.example_shape)
        artifact_paths = export_default_artifacts(
            output_dir=args.output_dir,
            example_shape=example_shape,
            current_checkpoint=args.checkpoint,
            current_arch=args.arch,
            orig_checkpoint=args.orig_checkpoint,
            orig_arch=args.orig_arch,
        )
    except Exception as exc:
        raise SystemExit(f"Export failed: {exc}") from exc
    for name, path in artifact_paths.items():
        print(f"{name}={path}")


if __name__ == "__main__":
    main()
