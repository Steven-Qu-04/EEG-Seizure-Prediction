#include <executorch/extension/module/module.h>
#include <executorch/extension/tensor/tensor.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <fstream>
#include <iostream>
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
constexpr float kRtol = 1e-3f;
constexpr float kAtol = 1e-4f;

struct Options {
  std::string model_path = "model.pte";
  std::string input_path = "golden_input.bin";
  std::string golden_output_path;
  std::size_t repeat = 1;
  std::size_t warmup = 0;
  bool quiet = false;
  bool timing_only = false;
};

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

bool parse_size_t_arg(
    const std::string& text,
    const char* flag_name,
    std::size_t& value) {
  try {
    value = static_cast<std::size_t>(std::stoull(text));
  } catch (const std::exception&) {
    std::cerr << "Invalid value for " << flag_name << ": " << text << std::endl;
    return false;
  }
  return true;
}

bool parse_args(int argc, char** argv, Options& options) {
  std::vector<std::string> positional;
  positional.reserve(3);

  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--repeat") {
      if (i + 1 >= argc || !parse_size_t_arg(argv[++i], "--repeat", options.repeat)) {
        return false;
      }
      continue;
    }
    if (arg == "--warmup") {
      if (i + 1 >= argc || !parse_size_t_arg(argv[++i], "--warmup", options.warmup)) {
        return false;
      }
      continue;
    }
    if (arg == "--quiet") {
      options.quiet = true;
      continue;
    }
    if (arg == "--timing-only") {
      options.timing_only = true;
      options.quiet = true;
      continue;
    }
    positional.push_back(arg);
  }

  if (!positional.empty()) {
    options.model_path = positional[0];
  }
  if (positional.size() >= 2) {
    options.input_path = positional[1];
  }
  if (positional.size() >= 3) {
    options.golden_output_path = positional[2];
  }
  if (positional.size() > 3) {
    std::cerr << "Unexpected extra positional arguments." << std::endl;
    return false;
  }
  if (options.repeat == 0) {
    std::cerr << "--repeat must be at least 1." << std::endl;
    return false;
  }
  return true;
}

bool load_golden_output(
    const std::string& golden_output_path,
    std::vector<float>& golden) {
  std::ifstream golden_file(golden_output_path, std::ios::binary);
  if (!golden_file) {
    std::cerr << "Failed to open golden output file: " << golden_output_path << std::endl;
    return false;
  }

  golden_file.seekg(0, std::ios::end);
  const auto bytes = static_cast<std::size_t>(golden_file.tellg());
  golden_file.seekg(0, std::ios::beg);
  if (bytes % sizeof(float) != 0) {
    std::cerr << "Invalid golden output file size: " << golden_output_path << std::endl;
    return false;
  }

  golden.resize(bytes / sizeof(float));
  golden_file.read(
      reinterpret_cast<char*>(golden.data()),
      static_cast<std::streamsize>(bytes));
  if (!golden_file) {
    std::cerr << "Failed to read golden output file: " << golden_output_path << std::endl;
    return false;
  }
  return true;
}

}  // namespace

int main(int argc, char** argv) {
  Options options;
  if (!parse_args(argc, argv, options)) {
    return 1;
  }

  std::vector<float> input(kInputSize);
  if (!read_binary_f32(options.input_path, input)) {
    return 1;
  }

  Module module(options.model_path);
  auto input_tensor = from_blob(input.data(), {kBatch, kChannels, kTimesteps});

  for (std::size_t i = 0; i < options.warmup; ++i) {
    const auto warmup_result = module.forward(input_tensor);
    if (!warmup_result.ok()) {
      std::cerr << "ExecuTorch forward failed during warmup for model: " << options.model_path
                << std::endl;
      return 2;
    }
  }

  std::vector<float> output;
  const auto start = std::chrono::steady_clock::now();
  for (std::size_t i = 0; i < options.repeat; ++i) {
    const auto result = module.forward(input_tensor);
    if (!result.ok()) {
      std::cerr << "ExecuTorch forward failed for model: " << options.model_path << std::endl;
      return 2;
    }
    if (i + 1 == options.repeat) {
      const auto output_tensor = result->at(0).toTensor();
      output = load_output_tensor(output_tensor);
    }
  }
  const auto end = std::chrono::steady_clock::now();
  const double total_ms = std::chrono::duration<double, std::milli>(end - start).count();
  const double mean_ms = total_ms / static_cast<double>(options.repeat);
  const double throughput = 1000.0 / mean_ms;

  if (!options.quiet) {
    std::cout << "Inference success." << std::endl;
    std::cout << "Output numel: " << output.size() << std::endl;
    for (std::size_t i = 0; i < std::min<std::size_t>(output.size(), 8); ++i) {
      std::cout << "output[" << i << "] = " << output[i] << std::endl;
    }
  }

  if (options.timing_only) {
    std::cout << "timing_repeat=" << options.repeat << std::endl;
    std::cout << "timing_warmup=" << options.warmup << std::endl;
    std::cout << "timing_total_ms=" << total_ms << std::endl;
    std::cout << "timing_mean_ms=" << mean_ms << std::endl;
    std::cout << "timing_throughput_inf_per_s=" << throughput << std::endl;
  } else {
    std::cout << "timing_repeat = " << options.repeat << std::endl;
    std::cout << "timing_warmup = " << options.warmup << std::endl;
    std::cout << "timing_total_ms = " << total_ms << std::endl;
    std::cout << "timing_mean_ms = " << mean_ms << std::endl;
    std::cout << "timing_throughput_inf_per_s = " << throughput << std::endl;
  }

  if (!options.golden_output_path.empty()) {
    std::vector<float> golden;
    if (!load_golden_output(options.golden_output_path, golden)) {
      return 3;
    }

    const float max_abs_err = compute_max_abs_err(output, golden);
    const bool allclose = outputs_allclose(output, golden, kRtol, kAtol);
    std::cout << "max_abs_err = " << max_abs_err << std::endl;
    std::cout << "allclose = " << (allclose ? "true" : "false") << std::endl;
    if (!allclose) {
      std::cerr << "Validation failed: output is not within rtol/atol tolerance" << std::endl;
      return 3;
    }
    std::cout << "Validation passed." << std::endl;
  } else if (!options.timing_only) {
    std::cout << "Golden output not provided; skipping numeric validation." << std::endl;
  }

  return 0;
}
