#!/bin/sh
set -eu

if [ -r "$HOME/.config/heimdall-agent.env" ]; then
    . "$HOME/.config/heimdall-agent.env"
fi

target_for_connection() {
    case "$1" in
        Ullas) printf '%s\n' "${HEIMDALL_SERVER_ULLAS:-192.168.8.101:7878}" ;;
        Brahmand) printf '%s\n' "${HEIMDALL_SERVER_BRAHMAND:-192.168.137.1:7878}" ;;
        *) printf '%s\n' "${HEIMDALL_SERVER:-}" ;;
    esac
}

agent_pid=''
active_target=''
stop_agent() {
    if [ -n "$agent_pid" ] && kill -0 "$agent_pid" 2>/dev/null; then
        kill "$agent_pid"
        wait "$agent_pid" 2>/dev/null || true
    fi
    agent_pid=''
}
trap 'stop_agent; exit 0' INT TERM
trap 'stop_agent' EXIT

while :; do
    if [ -n "$agent_pid" ] && ! kill -0 "$agent_pid" 2>/dev/null; then
        wait "$agent_pid" 2>/dev/null || true
        agent_pid=''
        active_target=''
    fi

    ethernet_interface=$(ip -4 -o address show | awk '$4 == "192.168.250.2/30" { print $2; exit }')
    if [ -n "$ethernet_interface" ] && [ "$(cat "/sys/class/net/$ethernet_interface/carrier" 2>/dev/null || true)" = 1 ]; then
        connection='Direct Ethernet'
        target=${HEIMDALL_SERVER_ETHERNET:-192.168.250.1:7878}
    else
        connection=$(nmcli -g GENERAL.CONNECTION device show wlan0 2>/dev/null || true)
        target=$(target_for_connection "$connection")
    fi
    if [ -n "$target" ] && [ "$target" != "$active_target" ]; then
        stop_agent
        echo "Starting Heimdall agent for $connection -> $target"
        /home/arduino/.local/bin/heimdall-service agent \
            --device /dev/serial/by-id/usb-Open_UWB_Heimdall_Gateway_7556160612A31510-if00 \
            --target "$target" \
            --bind 0.0.0.0:8080 &
        agent_pid=$!
        active_target=$target
    fi
    sleep 2
done
