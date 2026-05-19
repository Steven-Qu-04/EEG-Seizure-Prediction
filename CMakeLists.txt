cmake_minimum_required(VERSION 3.20)

project(nuro_work LANGUAGES CXX)

option(NURO_BUILD_EXECUTORCH_DEPLOY "Build the ExecuTorch deployment target." ON)

if(NURO_BUILD_EXECUTORCH_DEPLOY)
    add_subdirectory(deploy_executorch)
endif()
