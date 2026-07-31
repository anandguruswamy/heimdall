#!/bin/sh
set -eu

if [ -z "${HEIMDALL_SERVER:-}" ] && [ -r "$HOME/.config/heimdall-agent.env" ]; then
    . "$HOME/.config/heimdall-agent.env"
fi

exec /home/arduino/.local/bin/heimdall-service agent \
    --device /dev/serial/by-id/usb-Open_UWB_Heimdall_Gateway_7556160612A31510-if00 \
    --target "${HEIMDALL_SERVER:?set HEIMDALL_SERVER to the Windows server UDP address}" \
    --bind 0.0.0.0:8080
