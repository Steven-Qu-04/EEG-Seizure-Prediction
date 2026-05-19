from __future__ import annotations

import argparse
from pathlib import Path

from deploy_executorch.tools.common import (
    DEFAULT_ARCH_PATH,
    DEFAULT_CHECKPOINT,
    DEFAULT_OUTPUT_DIR,
    load_training_checkpoint,
    require_torch,
)


def _load_executorch_runtime():
    try:
        from executorch.runtime import Runtime
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "ExecuTorch runtime Python bindings are missing. Install executorch in the export environment."
        ) from exc
    return Runtime


def validate_export(checkpoint_path: Path, arch_path: Path, output_dir: Path) -> dict[str, object]:
    torch = require_torch()
    Runtime = _load_executorch_runtime()

    model = load_training_checkpoint(checkpoint_path=checkpoint_path, arch_path=arch_path)
    golden = torch.load(output_dir / "golden.pt", map_location="cpu", weights_only=False)
    input_tensor = golden["input"]

    runtime = Runtime.get()
    program = runtime.load_program(str(output_dir / "model.pte"))
    method = program.load_method("forward")
    et_output = method.execute([input_tensor])[0]

    with torch.inference_mode():
        eager_output = model(input_tensor)

    max_abs_err = torch.max(torch.abs(et_output - eager_output)).item()
    allclose = torch.allclose(et_output, eager_output, rtol=1e-3, atol=1e-4)
    same_argmax = torch.equal(torch.argmax(et_output, dim=1), torch.argmax(eager_output, dim=1))

    return {
        "eager_shape": tuple(eager_output.shape),
        "executorch_shape": tuple(et_output.shape),
        "allclose": bool(allclose),
        "same_argmax": bool(same_argmax),
        "max_abs_err": float(max_abs_err),
    }


def validate_model_path(
    checkpoint_path: Path,
    arch_path: Path,
    model_path: Path,
    golden_path: Path,
) -> dict[str, object]:
    torch = require_torch()
    Runtime = _load_executorch_runtime()

    model = load_training_checkpoint(checkpoint_path=checkpoint_path, arch_path=arch_path)
    golden = torch.load(golden_path, map_location="cpu", weights_only=False)
    input_tensor = golden["input"]

    runtime = Runtime.get()
    program = runtime.load_program(str(model_path))
    method = program.load_method("forward")
    et_output = method.execute([input_tensor])[0]

    with torch.inference_mode():
        eager_output = model(input_tensor)

    max_abs_err = torch.max(torch.abs(et_output - eager_output)).item()
    allclose = torch.allclose(et_output, eager_output, rtol=1e-3, atol=1e-4)
    same_argmax = torch.equal(torch.argmax(et_output, dim=1), torch.argmax(eager_output, dim=1))

    return {
        "model_path": str(model_path),
        "eager_shape": tuple(eager_output.shape),
        "executorch_shape": tuple(et_output.shape),
        "allclose": bool(allclose),
        "same_argmax": bool(same_argmax),
        "max_abs_err": float(max_abs_err),
    }


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate exported ExecuTorch model against eager output.")
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--arch", type=Path, default=DEFAULT_ARCH_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--model", type=Path, default=None)
    return parser


def main() -> None:
    try:
        args = build_argparser().parse_args()
        if args.model is not None:
            stats = validate_model_path(
                checkpoint_path=args.checkpoint,
                arch_path=args.arch,
                model_path=args.model,
                golden_path=args.output_dir / "golden.pt",
            )
        else:
            stats = validate_export(args.checkpoint, args.arch, args.output_dir)
    except Exception as exc:
        raise SystemExit(f"Validation failed: {exc}") from exc
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
