# ExecuTorch Deployment Scaffold

This directory contains the first-version deployment path for the fixed-shape EEG CNN:

- `deploy_model.py`: deployment-only model definition with static padding
- `tools/export_to_pte.py`: export `best_1.pt` to `dist/model.pte`
- `tools/validate_export.py`: compare eager output with ExecuTorch runtime output
- `tools/make_golden.py`: regenerate flat float32 binary fixtures
- `cpp/main.cpp`: C++ inference entrypoint
- `CMakeLists.txt`: ExecuTorch runtime build

Expected fixed ABI for v1:

- input: `float32`, shape `[1, 31, 5120]`
- output: `float32`, shape `[1, 2]`

Typical workflow:

```powershell
python -m deploy_executorch.tools.export_to_pte
python -m deploy_executorch.tools.validate_export
cmake -S . -B build -G Ninja -DCMAKE_C_COMPILER=clang-cl -DCMAKE_CXX_COMPILER=clang-cl
cmake --build build --config Release
```

