cmake_minimum_required(VERSION 3.20)

project(et_infer_demo CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(
    EXECUTORCH_SOURCE_DIR
    "${CMAKE_CURRENT_SOURCE_DIR}/external/executorch"
    CACHE PATH
    "Path to the vendored ExecuTorch source tree"
)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

set(EXECUTORCH_BUILD_EXTENSION_MODULE ON CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_EXTENSION_DATA_LOADER ON CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_EXTENSION_FLAT_TENSOR ON CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_EXTENSION_NAMED_DATA_MAP ON CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_EXTENSION_TENSOR ON CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_EXECUTOR_RUNNER OFF CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_EXTENSION_EVALUE_UTIL OFF CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_EXTENSION_RUNNER_UTIL OFF CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_KERNELS_PORTABLE ON CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_KERNELS_OPTIMIZED OFF CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_XNNPACK ON CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(EXECUTORCH_BUILD_PYBIND OFF CACHE BOOL "" FORCE)

if(NOT EXISTS "${EXECUTORCH_SOURCE_DIR}/CMakeLists.txt")
    message(FATAL_ERROR "ExecuTorch source not found at ${EXECUTORCH_SOURCE_DIR}")
endif()

add_subdirectory("${EXECUTORCH_SOURCE_DIR}" executorch_src)

add_executable(infer cpp/main.cpp)

target_link_libraries(
    infer
    PRIVATE
        executorch
        extension_module_static
        extension_tensor
        extension_data_loader
)

if(TARGET optimized_native_cpu_ops_lib)
    target_link_libraries(infer PRIVATE optimized_native_cpu_ops_lib)
endif()

if(TARGET portable_ops_lib)
    target_link_libraries(infer PRIVATE portable_ops_lib)
endif()

if(TARGET executorch_backends)
    target_link_libraries(infer PRIVATE executorch_backends)
endif()

if(TARGET xnnpack_backend)
    target_link_libraries(infer PRIVATE xnnpack_backend)
endif()
