#!/bin/sh
set -eu

interface=${1:-}
connection=${HEIMDALL_ETHERNET_CONNECTION:-Heimdall Direct Ethernet}
address=${HEIMDALL_ETHERNET_ADDRESS:-192.168.250.2/30}

if [ -z "$interface" ]; then
    interface=$(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2 == "ethernet" { print $1; exit }')
fi
if [ -z "$interface" ]; then
    echo 'No Ethernet interface found' >&2
    exit 1
fi

if nmcli -g NAME connection show | grep -Fxq "$connection"; then
    nmcli connection modify "$connection" connection.interface-name "$interface"
else
    nmcli connection add type ethernet ifname "$interface" con-name "$connection"
fi

nmcli connection modify "$connection" \
    connection.autoconnect yes \
    connection.autoconnect-priority 100 \
    ipv4.method manual \
    ipv4.addresses "$address" \
    ipv4.never-default yes \
    ipv4.gateway '' \
    ipv4.dns '' \
    ipv6.method disabled
nmcli connection up "$connection"

printf '%s\n' "Configured $interface as $address using '$connection'"
