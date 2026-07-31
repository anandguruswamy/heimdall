@echo off
setlocal EnableExtensions
if defined HEIMDALL_ZIG (
  set "ZIG=%HEIMDALL_ZIG%"
) else (
  set "ZIG=%~dp0..\..\tools\installers\windows-arm64\zig-aarch64-windows-0.15.2\zig.exe"
)
set "ARGS="
:collect
if "%~1"=="" goto run
if /I "%~1"=="--target=aarch64-pc-windows-gnu" (
  set "ARGS=%ARGS% --target=aarch64-windows-gnu"
) else (
  set "ARGS=%ARGS% "%~1""
)
shift
goto collect
:run
"%ZIG%" cc -target aarch64-windows-gnu %ARGS%
exit /b %ERRORLEVEL%
