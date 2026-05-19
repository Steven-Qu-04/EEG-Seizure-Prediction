from __future__ import annotations

import argparse
from pathlib import Path

from deploy_executorch.tools.common import DEFAULT_OUTPUT_DIR, require_torch, save_tensor_bin


def make_golden(output_dir: Path) -> None:
    torch = require_torch()
    golden = torch.load(output_dir / "golden.pt", map_location="cpu", weights_only=False)
    save_tensor_bin(golden["input"], output_dir / "golden_input.bin")
    save_tensor_bin(golden["output"], output_dir / "golden_output.bin")


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert golden.pt to flat float32 binary files.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main() -> None:
    try:
        args = build_argparser().parse_args()
        make_golden(args.output_dir)
    except Exception as exc:
        raise SystemExit(f"Golden export failed: {exc}") from exc
    print(f"Wrote golden_input.bin and golden_output.bin under {args.output_dir}")


if __name__ == "__main__":
    main()
