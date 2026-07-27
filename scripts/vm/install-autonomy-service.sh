#!/usr/bin/env bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo)"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
install -m 0644 \
    "$SCRIPT_DIR/hanstock-autonomy.service" \
    /etc/systemd/system/hanstock-autonomy.service
systemctl daemon-reload
systemctl enable hanstock-autonomy.service
systemctl restart hanstock-autonomy.service
systemctl status hanstock-autonomy.service --no-pager
