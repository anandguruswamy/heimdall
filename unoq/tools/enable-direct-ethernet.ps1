param(
    [string]$InterfaceAlias,
    [string]$LocalAddress = '192.168.250.1',
    [int]$PrefixLength = 30,
    [string]$PeerAddress = '192.168.250.2'
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this helper from an elevated PowerShell session.'
}

if ($InterfaceAlias) {
    $adapter = Get-NetAdapter -Name $InterfaceAlias
} else {
    $candidates = @(Get-NetAdapter -Physical | Where-Object {
        $_.Status -eq 'Up' -and $_.InterfaceDescription -notmatch 'Wi-?Fi|Wireless' -and
        -not (Get-NetRoute -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
    })
    if ($candidates.Count -ne 1) {
        $names = ($candidates | ForEach-Object Name) -join ', '
        throw "Expected one connected Ethernet adapter without a default route; found $($candidates.Count): $names. Pass -InterfaceAlias explicitly."
    }
    $adapter = $candidates[0]
}

Set-NetIPInterface -InterfaceIndex $adapter.InterfaceIndex -AddressFamily IPv4 -Dhcp Disabled
$addresses = @(Get-NetIPAddress -InterfaceIndex $adapter.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue)
$correct = $addresses | Where-Object { $_.IPAddress -eq $LocalAddress -and $_.PrefixLength -eq $PrefixLength }
if (-not $correct) {
    $addresses | Remove-NetIPAddress -Confirm:$false
    New-NetIPAddress -InterfaceIndex $adapter.InterfaceIndex -IPAddress $LocalAddress -PrefixLength $PrefixLength | Out-Null
}

$profile = Get-NetConnectionProfile -InterfaceIndex $adapter.InterfaceIndex -ErrorAction SilentlyContinue
if ($profile -and $profile.NetworkCategory -ne 'Private') {
    Set-NetConnectionProfile -InterfaceIndex $adapter.InterfaceIndex -NetworkCategory Private
}

$reachable = Test-Connection -ComputerName $PeerAddress -Count 2 -Quiet
[pscustomobject]@{
    InterfaceAlias = $adapter.Name
    InterfaceDescription = $adapter.InterfaceDescription
    LocalAddress = "$LocalAddress/$PrefixLength"
    PeerAddress = $PeerAddress
    PeerReachable = $reachable
}
