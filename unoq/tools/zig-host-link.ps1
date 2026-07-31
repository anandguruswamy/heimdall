$ErrorActionPreference = 'Stop'
$zig = if ($env:HEIMDALL_ZIG) {
    $env:HEIMDALL_ZIG
} else {
    Join-Path $PSScriptRoot '..\..\tools\installers\windows-arm64\zig-aarch64-windows-0.15.2\zig.exe'
}
$rustupRoot = if ($env:RUSTUP_HOME) { $env:RUSTUP_HOME } else { Join-Path $HOME '.rustup' }
$hostLib = Join-Path $rustupRoot 'toolchains\1.93.1-aarch64-pc-windows-gnullvm\lib\rustlib\aarch64-pc-windows-gnullvm\lib\self-contained'
$linkArguments = $env:HEIMDALL_LINK_ARGUMENTS
if (-not $linkArguments) { throw 'missing linker arguments' }
$responseArgument = $linkArguments.Trim().Trim('"')
if ($responseArgument.StartsWith('@')) {
    $linkArguments = [IO.File]::ReadAllText($responseArgument.Substring(1))
}
$linkArguments = [regex]::Replace($linkArguments, '-Wl,[^"\r\n]*list\.def', '-Wl,--export-all-symbols')
$linkArguments = $linkArguments.Replace('-Wl,-Bdynamic', '-Wl,-Bstatic')
$response = Join-Path ([IO.Path]::GetTempPath()) ("heimdall-link-{0}.rsp" -f [guid]::NewGuid())
try {
    # Keep Cargo's quoting intact; passing its long argument list through
    # Start-Process would make PowerShell re-tokenize linker flags.
    [IO.File]::WriteAllText($response, $linkArguments)
    & $zig cc -target aarch64-windows-gnu "-L$hostLib" "@$response"
    exit $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $response -Force -ErrorAction SilentlyContinue
}
