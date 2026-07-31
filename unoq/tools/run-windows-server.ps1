param(
    [string]$UdpBind = '0.0.0.0:7878',
    [string]$Bind = '0.0.0.0:8080',
    [string]$Data = 'data'
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot

# Match test-host.ps1 so native Windows builds can compile bundled SQLite and
# link Cargo build scripts without Visual Studio.
$env:CARGO_TARGET_AARCH64_PC_WINDOWS_GNULLVM_LINKER = Join-Path $PSScriptRoot 'zig-host-cc.cmd'
$env:CC_aarch64_pc_windows_gnullvm = Join-Path $PSScriptRoot 'zig-host-c-cc.cmd'
$env:AR_aarch64_pc_windows_gnullvm = Join-Path $PSScriptRoot 'zig-ar.cmd'

& rustup run 1.93.1-aarch64-pc-windows-gnullvm cargo run --locked --manifest-path (Join-Path $workspace 'Cargo.toml') --package heimdall-service -- server --udp-bind $UdpBind --bind $Bind --data $Data
exit $LASTEXITCODE
