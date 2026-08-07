param(
    [string]$UdpBind = '0.0.0.0:7878',
    [string]$Bind = '0.0.0.0:8080',
    [string]$Data = 'data',
    [string]$CameraDevice,
    [string]$Ffmpeg = 'ffmpeg'
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$binary = Join-Path $workspace 'target\windows-server\heimdall-service.exe'
$dataPath = if ([IO.Path]::IsPathRooted($Data)) { $Data } else { Join-Path $workspace $Data }

if (-not (Test-Path -LiteralPath $binary)) {
    throw "Build the Windows server first with .\tools\build-windows-server.ps1"
}

$serverArgs = @('server', '--udp-bind', $UdpBind, '--bind', $Bind, '--data', $dataPath)
if (-not [string]::IsNullOrWhiteSpace($CameraDevice)) {
    $serverArgs += @('--camera-device', $CameraDevice, '--ffmpeg', $Ffmpeg)
}

& $binary @serverArgs
exit $LASTEXITCODE
