#!/bin/bash
# DB 기반 전략 스케쥴 디스패처 실행기.
# strategy_schedules 테이블의 enabled 스케쥴 중 실행 윈도우/주기 조건을 만족하는
# 전략을 run_scheduled_cycle로 돌린다. VM cron이 주기적으로 이 스크립트를 호출한다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

RUNTIME_DIR="$ROOT_DIR/.runtime"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

find_python() {
    if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return 0; fi
    if [ -x "$ROOT_DIR/.venv/bin/python" ]; then echo "$ROOT_DIR/.venv/bin/python"; return 0; fi
    if [ -x "$ROOT_DIR/venv/bin/python" ]; then echo "$ROOT_DIR/venv/bin/python"; return 0; fi
    command -v python3 || command -v python
}

PYTHON_BIN="$(find_python)"
LOG_FILE="$LOG_DIR/strategy-dispatch.log"
LOCK_FILE="$RUNTIME_DIR/strategy-dispatch.lock"

acquire_lock() {
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK_FILE"
        if ! flock -n 9; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] strategy_dispatch already running; skipped"
            exit 0
        fi
        return 0
    fi
    if ! mkdir "$LOCK_FILE.dir" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] strategy_dispatch already running; skipped"
        exit 0
    fi
    trap 'rmdir "$LOCK_FILE.dir" 2>/dev/null || true' EXIT
}

{
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] strategy_dispatch start"
    acquire_lock
    set +e
    "$PYTHON_BIN" -m src.strategy_scheduler
    status=$?
    set -e
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] strategy_dispatch done status=$status"
    exit "$status"
} >> "$LOG_FILE" 2>&1
