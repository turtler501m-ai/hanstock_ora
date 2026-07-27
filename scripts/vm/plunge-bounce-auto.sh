#!/bin/bash
set -euo pipefail

FORCE_RUN=0
if [ $# -gt 0 ] && { [ "$1" = "--force" ] || [ "$1" = "force" ]; }; then
    FORCE_RUN=1
    export HANSTOCK_SCHEDULE_FORCE=1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

RUNTIME_DIR="$ROOT_DIR/.runtime"
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
LOG_FILE="$LOG_DIR/plunge-bounce-auto.log"
LOCK_FILE="$RUNTIME_DIR/plunge-bounce-auto.lock"

should_run_now() {
    if [ "${HANSTOCK_SCHEDULE_FORCE:-0}" = "1" ] || [ "$FORCE_RUN" = "1" ]; then
        return 0
    fi

    local dow hhmm
    dow="$(TZ=Asia/Seoul date '+%u')"
    hhmm="$(TZ=Asia/Seoul date '+%H%M')"
    # 09:00 ~ 15:30 (KST) during weekdays
    [ "$dow" -ge 1 ] && [ "$dow" -le 5 ] && [ "$hhmm" -ge 900 ] && [ "$hhmm" -le 1530 ]
}

acquire_lock() {
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK_FILE"
        if ! flock -n 9; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] plunge_bounce_auto already running; skipped"
            exit 0
        fi
        return 0
    fi

    if ! mkdir "$LOCK_FILE.dir" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] plunge_bounce_auto already running; skipped"
        exit 0
    fi
    trap 'rmdir "$LOCK_FILE.dir" 2>/dev/null || true' EXIT
}

{
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] plunge_bounce_auto start"
    acquire_lock
    if ! should_run_now; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] outside Korea market schedule; skipped"
        exit 0
    fi
    set +e
    "$PYTHON_BIN" -m src.scheduler --mode execute --auto-approve --force-strategy-id plunge_bounce_strategy
    status=$?
    set -e
    if [ "$status" -ne 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] plunge_bounce_auto failed status=$status"
        exit "$status"
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] plunge_bounce_auto done"
} >> "$LOG_FILE" 2>&1
