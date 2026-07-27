#!/bin/bash
# Install systemd service for Hanstock
set -e

SERVICE_FILE="/etc/systemd/system/hanstock.service"
SRC_FILE="$(dirname "$0")/hanstock.service"

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo)"
  exit 1
fi

echo "Copying service file..."
cp "$SRC_FILE" "$SERVICE_FILE"

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling hanstock service..."
systemctl enable hanstock

echo "Starting hanstock service..."
systemctl start hanstock

echo "Status:"
systemctl status hanstock --no-pager
