$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot

# Cross-built test executables cannot run on Windows; this verifies they compile
# and link for the deployed UNO Q architecture.
$env:CC_aarch64_unknown_linux_gnu = Join-Path $PSScriptRoot 'zig-cc.cmd'
$env:AR_aarch64_unknown_linux_gnu = Join-Path $PSScriptRoot 'zig-ar.cmd'
$env:CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER = Join-Path $PSScriptRoot 'zig-cc.cmd'
$env:CARGO_TARGET_AARCH64_PC_WINDOWS_GNULLVM_LINKER = Join-Path $PSScriptRoot 'zig-host-cc.cmd'

& rustup run 1.93.1-aarch64-pc-windows-gnullvm cargo test --locked --no-run --target aarch64-unknown-linux-gnu --manifest-path (Join-Path $workspace 'Cargo.toml') @args
exit $LASTEXITCODE
