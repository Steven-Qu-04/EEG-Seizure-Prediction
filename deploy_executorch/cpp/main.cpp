#include <executorch/extension/module/module.h>
#include <executorch/extension/tensor/tensor.h>

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <string>
#include <vector>

using executorch::extension::Module;
using executorch::extension::from_blob;

namespace {

constexpr int64_t kBatch = 1;
constexpr int64_t kChannels = 31;
constexpr int64_t kTimesteps = 5120;
constexpr std::size_t kInputSize =
    static_cast<std::size_t>(kBatch * kChannels * kTimesteps);

bool read_binary_f32(
    const std::string& path,
    std::vector<float>& data) {
  std::ifstream file(path, std::ios::binary);
  if (!file) {
    std::cerr << "Failed to open binary file: " << path << std::endl;
    return false;
  }

  file.read(
      reinterpret_cast<char*>(data.data()),
      static_cast<std::streamsize>(data.size() * sizeof(float)));
  if (!file) {
    std::cerr << "Failed to read expected float count from: " << path << std::endl;
    return false;
  }
  return true;
}

std::vector<float> load_output_tensor(const executorch::aten::Tensor& tensor) {
  const auto numel = static_cast<std::size_t>(tensor.numel());
  const float* ptr = tensor.const_data_ptr<float>();
  return std::vector<float>(ptr, ptr + numel);
}

float compute_max_abs_err(
    const std::vector<float>& output,
    const std::vector<float>& golden) {
  const std::size_t count = std::min(output.size(), golden.size());
  float max_abs_err = 0.0f;
  for (std::size_t i = 0; i < count; ++i) {
    max_abs_err = std::max(max_abs_err, std::fabs(output[i] - golden[i]));
  }
  return max_abs_err;
}

bool outputs_allclose(
    const std::vector<float>& output,
    const std::vector<float>& golden,
    float rtol,
    float atol) {
  if (output.size() != golden.size()) {
    return false;
  }

  for (std::size_t i = 0; i < output.size(); ++i) {
    const float diff = std::fabs(output[i] - golden[i]);
    const float limit = atol + rtol * std::fabs(golden[i]);
    if (diff > limit) {
      return false;
    }
  }
  return true;
}

}  // namespace

int main(int argc, char** argv) {
  const std::string model_path = argc > 1 ? argv[1] : "model.pte";
  const std::string input_path = argc > 2 ? argv[2] : "golden_input.bin";
  const std::string golden_output_path = argc > 3 ? argv[3] : "";

  std::vector<float> input(kInputSize);
  if (!read_binary_f32(input_path, input)) {
    return 1;
  }

  Module module(model_path);
  auto input_tensor = from_blob(input.data(), {kBatch, kChannels, kTimesteps});
  const auto result = module.forward(input_tensor);
  if (!result.ok()) {
    std::cerr << "ExecuTorch forward failed for model: " << model_path << std::endl;
    return 2;
  }

  const auto output_tensor = result->at(0).toTensor();
  const auto output = load_output_tensor(output_tensor);

  std::cout << "Inference success." << std::endl;
  std::cout << "Output numel: " << output.size() << std::endl;
  for (std::size_t i = 0; i < std::min<std::size_t>(output.size(), 8); ++i) {
    std::cout << "output[" << i << "] = " << output[i] << std::endl;
  }

  if (!golden_output_path.empty()) {
    std::ifstream golden_file(golden_output_path, std::ios::binary);
    if (!golden_file) {
      std::cerr << "Failed to open golden output file: " << golden_output_path << std::endl;
      return 3;
    }

    golden_file.seekg(0, std::ios::end);
    const auto bytes = static_cast<std::size_t>(golden_file.tellg());
    golden_file.seekg(0, std::ios::beg);
    if (bytes % sizeof(float) != 0) {
      std::cerr << "Invalid golden output file size: " << golden_output_path << std::endl;
      return 3;
    }

    std::vector<float> golden(bytes / sizeof(float));
    golden_file.read(
        reinterpret_cast<char*>(golden.data()),
        static_cast<std::streamsize>(bytes));
    if (!golden_file) {
      std::cerr << "Failed to read golden output file: " << golden_output_path << std::endl;
      return 3;
    }

    constexpr float kRtol = 1e-3f;
    constexpr float kAtol = 1e-4f;
    const float max_abs_err = compute_max_abs_err(output, golden);
    const bool allclose = outputs_allclose(output, golden, kRtol, kAtol);
    std::cout << "max_abs_err = " << max_abs_err << std::endl;
    std::cout << "allclose = " << (allclose ? "true" : "false") << std::endl;
    if (!allclose) {
      std::cerr << "Validation failed: output is not within rtol/atol tolerance" << std::endl;
      return 3;
    }
    std::cout << "Validation passed." << std::endl;
  } else {
    std::cout << "Golden output not provided; skipping numeric validation." << std::endl;
  }

  return 0;
}
