param(
    [switch]$Release
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$env:CC_aarch64_unknown_linux_gnu = Join-Path $PSScriptRoot 'zig-cc.cmd'
$env:AR_aarch64_unknown_linux_gnu = Join-Path $PSScriptRoot 'zig-ar.cmd'
$env:CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER = Join-Path $PSScriptRoot 'zig-cc.cmd'
$env:CARGO_TARGET_AARCH64_PC_WINDOWS_GNULLVM_LINKER = Join-Path $PSScriptRoot 'zig-host-cc.cmd'

$arguments = @(
    'build',
    '--locked',
    '--target', 'aarch64-unknown-linux-gnu',
    '--package', 'heimdall-service',
    '--manifest-path', (Join-Path $workspace 'Cargo.toml')
)
if ($Release) {
    $arguments += '--release'
}

& rustup run 1.93.1-aarch64-pc-windows-gnullvm cargo @arguments
exit $LASTEXITCODE
