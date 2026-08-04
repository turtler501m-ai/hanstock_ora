#!/usr/bin/env bash
set -euo pipefail

BRANCH="${1:-main}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

echo "[update] repo: $ROOT_DIR"
echo "[update] branch: $BRANCH"

if [ ! -f ".env" ]; then
    echo "[update] missing .env. Create it from .env.example and set VM secrets first." >&2
    exit 1
fi

git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

if [ ! -x ".venv/bin/python" ]; then
    echo "[update] creating .venv"
    python3 -m venv .venv
fi

PYTHON="$ROOT_DIR/.venv/bin/python"

"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Hanstock VM requires Python 3.10+"'

echo "[update] installing requirements"
"$PYTHON" -m pip install --require-hashes -r constraints/vm-python.lock

if systemctl list-unit-files hanstock.service >/dev/null 2>&1; then
    echo "[update] syncing dashboard systemd unit"
    sudo install -m 0644 \
        "$ROOT_DIR/scripts/vm/hanstock.service" \
        /etc/systemd/system/hanstock.service
    sudo systemctl daemon-reload
fi

echo "[update] restarting dashboard"
"$ROOT_DIR/scripts/vm/server.sh" restart
"$ROOT_DIR/scripts/vm/server.sh" status

if systemctl list-unit-files hanstock-autonomy.service >/dev/null 2>&1; then
    echo "[update] restarting autonomy service"
    sudo systemctl restart hanstock-autonomy.service
    systemctl status hanstock-autonomy.service --no-pager
else
    echo "[update] autonomy service is not installed (safe default)"
fi

echo "[update] syncing condition monitor service"
sudo install -m 0644 \
    "$ROOT_DIR/scripts/vm/hanstock-condition-monitor.service" \
    /etc/systemd/system/hanstock-condition-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable hanstock-condition-monitor.service
sudo systemctl restart hanstock-condition-monitor.service
systemctl status hanstock-condition-monitor.service --no-pager

echo "[update] done"
