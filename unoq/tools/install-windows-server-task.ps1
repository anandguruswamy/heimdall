param(
    [string]$TaskName = 'Heimdall Live Server',
    [string]$UdpBind = '0.0.0.0:7878',
    [string]$Bind = '0.0.0.0:8080'
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$built = Join-Path $workspace 'target\windows-build\release\heimdall-service.exe'
$deployDirectory = Join-Path $workspace 'target\windows-server'
$binary = Join-Path $deployDirectory 'heimdall-service.exe'
$data = Join-Path $workspace 'data'
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path -LiteralPath $built)) {
    throw "Build the Windows server first with .\tools\build-windows-server.ps1"
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if ((Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State -ne 'Running') { break }
    Start-Sleep -Milliseconds 100
}
$listeners = @(
    Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
    Get-NetUDPEndpoint -LocalPort 7878 -ErrorAction SilentlyContinue
)
$listeners | Where-Object OwningProcess | Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if (-not (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) -and
        -not (Get-NetUDPEndpoint -LocalPort 7878 -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 100
}
New-Item -ItemType Directory -Path $deployDirectory -Force | Out-Null
Copy-Item -LiteralPath $built -Destination $binary -Force

$action = New-ScheduledTaskAction -Execute $binary -Argument "server --udp-bind $UdpBind --bind $Bind --data `"$data`"" -WorkingDirectory $workspace
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
