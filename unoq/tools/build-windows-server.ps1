$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot

$env:CARGO_TARGET_AARCH64_PC_WINDOWS_GNULLVM_LINKER = Join-Path $PSScriptRoot 'zig-host-cc.cmd'
$env:CC_aarch64_pc_windows_gnullvm = Join-Path $PSScriptRoot 'zig-host-c-cc.cmd'
$env:AR_aarch64_pc_windows_gnullvm = Join-Path $PSScriptRoot 'zig-ar.cmd'
$env:CARGO_TARGET_DIR = Join-Path $workspace 'target\windows-build'

& rustup run 1.93.1-aarch64-pc-windows-gnullvm cargo build --release --locked --manifest-path (Join-Path $workspace 'Cargo.toml') --package heimdall-service
exit $LASTEXITCODE
