#!/bin/bash
# [유튜브 24편 구현] 미국주식 자동매매 스케줄러 VM 실행 스크립트
set -euo pipefail

FORCE_RUN=0
if [ $# -gt 0 ] && { [ "$1" = "--force" ] || [ "$1" = "force" ]; }; then
    FORCE_RUN=1
    export MISTOCK_SCHEDULE_FORCE=1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

RUNTIME_DIR="$ROOT_DIR/.runtime/mistock"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

find_python() {
    if [ -n "${PYTHON:-}" ]; then
        echo "$PYTHON"
        return 0
    fi
    if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
        echo "$ROOT_DIR/.venv/bin/python"
        return 0
    fi
    if [ -x "$ROOT_DIR/venv/bin/python" ]; then
        echo "$ROOT_DIR/venv/bin/python"
        return 0
    fi
    command -v python3 || command -v python
}

PYTHON_BIN="$(find_python)"
LOG_FILE="$LOG_DIR/mistock-auto.log"
LOCK_FILE="$RUNTIME_DIR/mistock-auto.lock"

should_run_now() {
    if [ "${MISTOCK_SCHEDULE_FORCE:-0}" = "1" ] || [ "$FORCE_RUN" = "1" ]; then
        return 0
    fi

    local dow hhmm
    dow="$(TZ=Asia/Seoul date '+%u')"    # 1 (Mon) - 7 (Sun)
    hhmm="$(TZ=Asia/Seoul date '+%H%M')"  # 0000 - 2359

    # 1. Weekday evening session
    if [ "$dow" -ge 1 ] && [ "$dow" -le 5 ] && [ "$hhmm" -ge 2100 ] && [ "$hhmm" -le 2359 ]; then
        return 0
    fi

    # 2. Weekday early morning session
    if [ "$dow" -ge 2 ] && [ "$dow" -le 6 ] && [ "$hhmm" -ge 0000 ] && [ "$hhmm" -le 0600 ]; then
        return 0
    fi

    return 1
}

acquire_lock() {
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK_FILE"
        if ! flock -n 9; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] mistock_auto already running; skipped"
            exit 0
        fi
        return 0
    fi

    if ! mkdir "$LOCK_FILE.dir" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] mistock_auto already running; skipped"
        exit 0
    fi
    trap 'rmdir "$LOCK_FILE.dir" 2>/dev/null || true' EXIT
}

{
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] mistock_auto start"
    acquire_lock
    if ! should_run_now; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] outside US stock market schedule (KST 21:00 - 06:00); skipped"
        exit 0
    fi
    set +e
    "$PYTHON_BIN" -m src.mistock.scheduler --mode execute
    status=$?
    set -e
    if [ "$status" -ne 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] mistock_auto failed status=$status"
        exit "$status"
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] mistock_auto done"
} >> "$LOG_FILE" 2>&1
