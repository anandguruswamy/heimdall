$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot

# Rust build scripts and proc macros are Windows ARM64 executables. This wrapper
# lets the pinned GNU Windows toolchain link them with the cached Zig compiler.
$env:CARGO_TARGET_AARCH64_PC_WINDOWS_GNULLVM_LINKER = Join-Path $PSScriptRoot 'zig-host-cc.cmd'
$env:CC_aarch64_pc_windows_gnullvm = Join-Path $PSScriptRoot 'zig-host-c-cc.cmd'
$env:AR_aarch64_pc_windows_gnullvm = Join-Path $PSScriptRoot 'zig-ar.cmd'

& rustup run 1.93.1-aarch64-pc-windows-gnullvm cargo test --locked --manifest-path (Join-Path $workspace 'Cargo.toml') @args
exit $LASTEXITCODE
