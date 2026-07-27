@echo off
setlocal EnableExtensions
if defined HEIMDALL_ZIG (
  set "ZIG=%HEIMDALL_ZIG%"
) else (
  set "ZIG=%~dp0..\..\tools\installers\windows-arm64\zig-aarch64-windows-0.15.2\zig.exe"
)
"%ZIG%" ar %*
