param(
    [string]$TunnelName = 'ullas-full',
    [string]$PhysicalInterfaceAlias,
    [string]$HotspotSubnet = '192.168.137.0/24'
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this helper from an elevated PowerShell session.'
}

$wg = Get-Command wg.exe -ErrorAction Stop
$endpointOutput = @(& $wg.Source show $TunnelName endpoints 2>&1)
if ($LASTEXITCODE -ne 0 -or $endpointOutput.Count -eq 0) {
    throw "Unable to read endpoint for WireGuard tunnel '$TunnelName': $endpointOutput"
}

$endpoint = ($endpointOutput[0].ToString().Trim() -split '\s+')[-1]
if ($endpoint -match '^\[(.+)\]:(\d+)$') {
    $endpointHost = $Matches[1]
} elseif ($endpoint -match '^(.+):(\d+)$') {
    $endpointHost = $Matches[1]
} else {
    throw "Unexpected WireGuard endpoint '$endpoint'."
}

$endpointAddress = $null
if (-not [Net.IPAddress]::TryParse($endpointHost, [ref]$endpointAddress)) {
    $endpointAddress = Resolve-DnsName -Name $endpointHost -Type A |
        Where-Object IPAddress |
        Select-Object -First 1 -ExpandProperty IPAddress
    if (-not $endpointAddress) {
        throw "Unable to resolve WireGuard endpoint '$endpointHost'."
    }
    $endpointAddress = [Net.IPAddress]::Parse($endpointAddress)
}
if ($endpointAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
    throw 'This helper currently requires an IPv4 WireGuard endpoint.'
}

$tunnelInterface = Get-NetIPInterface -InterfaceAlias $TunnelName -AddressFamily IPv4
if ($PhysicalInterfaceAlias) {
    $physicalInterface = Get-NetIPInterface -InterfaceAlias $PhysicalInterfaceAlias -AddressFamily IPv4
    $defaultRoute = Get-NetRoute -InterfaceIndex $physicalInterface.InterfaceIndex -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
        Where-Object NextHop -ne '0.0.0.0' |
        Sort-Object RouteMetric |
        Select-Object -First 1
} else {
    $defaultRoute = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
        Where-Object { $_.NextHop -ne '0.0.0.0' -and $_.InterfaceIndex -ne $tunnelInterface.InterfaceIndex } |
        Sort-Object @{ Expression = { $_.RouteMetric + $_.InterfaceMetric } } |
        Select-Object -First 1
}
if (-not $defaultRoute) {
    throw 'No non-WireGuard IPv4 default route is available.'
}

$prefix = "$($endpointAddress.IPAddressToString)/32"
$existing = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix -ErrorAction SilentlyContinue)
$correct = $existing | Where-Object {
    $_.InterfaceIndex -eq $defaultRoute.InterfaceIndex -and $_.NextHop -eq $defaultRoute.NextHop
} | Select-Object -First 1

$routeChanged = $false
if (-not $correct) {
    $existing | Remove-NetRoute -Confirm:$false
    $correct = New-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix `
        -InterfaceIndex $defaultRoute.InterfaceIndex -NextHop $defaultRoute.NextHop `
        -RouteMetric 1 -PolicyStore ActiveStore
    $routeChanged = $true
}

$handshakeOutput = @(& $wg.Source show $TunnelName latest-handshakes 2>$null)
$handshakeEpoch = if ($handshakeOutput.Count) {
    [long](($handshakeOutput[0].ToString().Trim() -split '\s+')[-1])
} else { 0 }
$handshakeAge = if ($handshakeEpoch -gt 0) {
    [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $handshakeEpoch
} else { $null }
$hotspotRoute = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $HotspotSubnet -ErrorAction SilentlyContinue

[pscustomobject]@{
    Tunnel = $TunnelName
    Endpoint = $endpoint
    EndpointRoute = $prefix
    PhysicalInterface = $defaultRoute.InterfaceAlias
    PhysicalGateway = $defaultRoute.NextHop
    RouteChanged = $routeChanged
    HandshakeAgeSeconds = $handshakeAge
    HotspotRouteActive = [bool]$hotspotRoute
}
