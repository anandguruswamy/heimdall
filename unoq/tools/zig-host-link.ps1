param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$LinkArguments
)

$ErrorActionPreference = 'Stop'
$zig = if ($env:HEIMDALL_ZIG) {
    $env:HEIMDALL_ZIG
} else {
    Join-Path $PSScriptRoot '..\..\tools\installers\windows-arm64\zig-aarch64-windows-0.15.2\zig.exe'
}
$rustupRoot = if ($env:RUSTUP_HOME) { $env:RUSTUP_HOME } else { Join-Path $HOME '.rustup' }
$hostLib = Join-Path $rustupRoot 'toolchains\1.93.1-aarch64-pc-windows-gnullvm\lib\rustlib\aarch64-pc-windows-gnullvm\lib\self-contained'
$temporary = $null

try {
    if ($LinkArguments.Count -eq 1 -and $LinkArguments[0].StartsWith('@')) {
        $source = $LinkArguments[0].Substring(1)
        $content = [IO.File]::ReadAllText($source)
        $content = [regex]::Replace($content, '-Wl,[^"\r\n]*list\.def', '-Wl,--export-all-symbols')
        $content = $content.Replace('-Wl,-Bdynamic', '-Wl,-Bstatic')
        $temporary = Join-Path ([IO.Path]::GetTempPath()) ("heimdall-link-{0}.rsp" -f [guid]::NewGuid())
        [IO.File]::WriteAllText($temporary, $content)
        $LinkArguments = @("@$temporary")
    } else {
        $LinkArguments = $LinkArguments | ForEach-Object {
            if ($_ -match '^-Wl,.*list\.def$') { '-Wl,--export-all-symbols' }
            elseif ($_ -eq '-Wl,-Bdynamic') { '-Wl,-Bstatic' }
            else { $_ }
        }
    }
    & $zig cc -target aarch64-windows-gnu "-L$hostLib" @LinkArguments
    exit $LASTEXITCODE
} finally {
    if ($temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
}
