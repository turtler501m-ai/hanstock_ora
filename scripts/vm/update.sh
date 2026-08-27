#!/usr/bin/env bash
set -euo pipefail

BRANCH="${1:-main}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_NAME="$(basename "$ROOT_DIR")"
INSTANCE_SUFFIX="${REPO_NAME#hanstock_}"
if [ "$INSTANCE_SUFFIX" != "$REPO_NAME" ]; then
    DASHBOARD_UNIT="hanstock-$INSTANCE_SUFFIX.service"
    CONDITION_MONITOR_UNIT="hanstock-$INSTANCE_SUFFIX-condition-monitor.service"
else
    DASHBOARD_UNIT="hanstock.service"
    CONDITION_MONITOR_UNIT="hanstock-condition-monitor.service"
fi
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

install_systemd_unit() {
    local source_file="$1"
    local target_file="$2"
    local rendered_file

    rendered_file="$(mktemp)"
    sed -E \
        -e "s#/home/ubuntu/hanstock[^ /]*#$ROOT_DIR#g" \
        -e "s#hanstock\.service#$DASHBOARD_UNIT#g" \
        "$source_file" > "$rendered_file"
    sudo install -m 0644 "$rendered_file" "$target_file"
    rm -f "$rendered_file"
}

"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Hanstock VM requires Python 3.10+"'

echo "[update] installing requirements"
"$PYTHON" -m pip install --require-hashes -r constraints/vm-python.lock

if systemctl list-unit-files "$DASHBOARD_UNIT" >/dev/null 2>&1; then
    echo "[update] syncing dashboard systemd unit"
    install_systemd_unit \
        "$ROOT_DIR/scripts/vm/hanstock.service" \
        "/etc/systemd/system/$DASHBOARD_UNIT"
    sudo systemctl daemon-reload
fi

echo "[update] restarting dashboard"
"$ROOT_DIR/scripts/vm/server.sh" restart
"$ROOT_DIR/scripts/vm/server.sh" status

if systemctl list-unit-files hanstock-autonomy.service >/dev/null 2>&1; then
    echo "[update] syncing autonomy systemd unit"
    install_systemd_unit \
        "$ROOT_DIR/scripts/vm/hanstock-autonomy.service" \
        /etc/systemd/system/hanstock-autonomy.service
    if [ -f /etc/systemd/system/hanstock-autonomy.service.d/override.conf ]; then
        echo "[update] syncing autonomy systemd override paths"
        install_systemd_unit \
            /etc/systemd/system/hanstock-autonomy.service.d/override.conf \
            /etc/systemd/system/hanstock-autonomy.service.d/override.conf
    fi
    sudo systemctl daemon-reload
    echo "[update] restarting autonomy service"
    sudo systemctl restart hanstock-autonomy.service
    systemctl status hanstock-autonomy.service --no-pager
else
    echo "[update] autonomy service is not installed (safe default)"
fi

echo "[update] syncing condition monitor service"
install_systemd_unit \
    "$ROOT_DIR/scripts/vm/hanstock-condition-monitor.service" \
    "/etc/systemd/system/$CONDITION_MONITOR_UNIT"
sudo systemctl daemon-reload
sudo systemctl enable "$CONDITION_MONITOR_UNIT"
sudo systemctl restart "$CONDITION_MONITOR_UNIT"
systemctl status "$CONDITION_MONITOR_UNIT" --no-pager

echo "[update] done"
