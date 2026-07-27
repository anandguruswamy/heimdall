#!/bin/sh
set -eu

exec /home/arduino/.local/bin/heimdall-service serve \
    --device /dev/serial/by-id/usb-Open_UWB_Heimdall_Gateway_7556160612A31510-if00 \
    --data /home/arduino/heimdall-data \
    --bind 0.0.0.0:8080
