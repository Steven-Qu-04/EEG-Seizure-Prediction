@echo off
setlocal

call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\LaunchDevCmd.bat" -host_arch=amd64 -arch=amd64
if errorlevel 1 exit /b %errorlevel%

set "DEBUG=0"
set "BUCK2=E:\BaiduSyncdisk\nuro_work\deploy_executorch\tools\bin\buck2.exe"
set "EXECUTORCH_BUILD_PYBIND=ON"
set "EXECUTORCH_BUILD_KERNELS_CUSTOM_AOT=OFF"
set "CMAKE_BUILD_PARALLEL_LEVEL=4"
set "CC=C:\PROGRA~2\MICROS~2\2022\BUILDT~1\VC\Tools\Llvm\x64\bin\clang-cl.exe"
set "CXX=C:\PROGRA~2\MICROS~2\2022\BUILDT~1\VC\Tools\Llvm\x64\bin\clang-cl.exe"
set "CMAKE_ARGS=-G Ninja -DEXECUTORCH_BUILD_XNNPACK=ON -DCMAKE_RC_COMPILER=C:/PROGRA~2/WI3CF2~1/10/bin/10.0.18362.0/x64/rc.exe -DCMAKE_MT=C:/PROGRA~2/WI3CF2~1/10/bin/10.0.18362.0/x64/mt.exe"

echo DEBUG=%DEBUG%
echo BUCK2=%BUCK2%
echo EXECUTORCH_BUILD_KERNELS_CUSTOM_AOT=%EXECUTORCH_BUILD_KERNELS_CUSTOM_AOT%
echo CMAKE_BUILD_PARALLEL_LEVEL=%CMAKE_BUILD_PARALLEL_LEVEL%

E:\conda_envs\nuro-pt\python.exe -m pip install . --no-build-isolation --no-deps
exit /b %errorlevel%
