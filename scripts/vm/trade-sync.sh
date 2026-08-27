#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
LOG_DIR="$ROOT_DIR/logs"
PORT="${PORT:-8000}"
DAYS="${HANSTOCK_TRADE_SYNC_DAYS:-14}"
URL="${HANSTOCK_DASHBOARD_URL:-http://127.0.0.1:$PORT}"
LOCK_FILE="$RUNTIME_DIR/trade-sync-trigger.lock"
LOG_FILE="$LOG_DIR/trade-sync-cron.log"

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

{
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] trade sync trigger start days=$DAYS"
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK_FILE"
        if ! flock -n 9; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] trade sync trigger already running; skipped"
            exit 0
        fi
    fi
    response="$(curl --fail --silent --show-error --max-time 30 \
        -X POST "$URL/api/trades/sync?days=$DAYS")"
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] trade sync accepted response=$response"
} >> "$LOG_FILE" 2>&1
