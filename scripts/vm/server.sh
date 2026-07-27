#!/bin/bash
ACTION="${1:-restart}"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
RELOAD="${RELOAD:-false}"
LINES="${LINES:-80}"
UVICORN_LOG_LEVEL="${UVICORN_LOG_LEVEL:-error}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR" || exit 1
RUNTIME_DIR="$ROOT_DIR/.runtime"
PID_FILE="$RUNTIME_DIR/dashboard-server.pid"
STDOUT_LOG="$RUNTIME_DIR/dashboard-server.log"
STDERR_LOG="$RUNTIME_DIR/dashboard-server.err.log"

mkdir -p "$RUNTIME_DIR"

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

    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            resolved=$(command -v "$candidate")
            echo "$resolved"
            return 0
        fi
    done

    echo "[server] python executable not found" >&2
    return 1
}

get_listening_pids() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -ti ":$PORT" 2>/dev/null || true
    elif command -v ss >/dev/null 2>&1; then
        ss -tlnp 2>/dev/null | grep -E "(:$PORT)" | grep -oP 'pid=\K[0-9]+' || true
    else
        netstat -tlnp 2>/dev/null | grep -E "[:\s]$PORT\s" | grep -oP '\d+/' | cut -d'/' -f1 || true
    fi
}

get_dashboard_pids() {
    pgrep -f "uvicorn.*src.dashboard" 2>/dev/null || true
}

get_pid_file_pids() {
    if [ -f "$PID_FILE" ]; then
        cat "$PID_FILE" 2>/dev/null || true
    fi
}

get_server_pids() {
    echo "$(get_pid_file_pids) $(get_listening_pids) $(get_dashboard_pids)" | tr ' ' '\n' | grep -E "^[0-9]+$" | sort -nu || true
}

stop_server() {
    pids=$(get_server_pids)

    if [ -z "$pids" ]; then
        echo "[server] no dashboard server found on port $PORT"
        [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
        return
    fi

    for pid in $pids; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null && echo "[server] stopped PID $pid" || echo "[server] stop skipped PID $pid"
        fi
    done

    [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
}

start_server() {
    listening_pids=$(get_listening_pids)

    if [ -n "$listening_pids" ]; then
        echo "[server] already listening on port $PORT (PID $listening_pids)"
        return
    fi

    python=$(find_python)
    if [ $? -ne 0 ]; then
        exit 1
    fi

    echo "[server] starting server on http://$HOST:$PORT"

    args=(-m uvicorn src.dashboard:app --host "$HOST" --port "$PORT" --log-level "$UVICORN_LOG_LEVEL")
    if [ "$RELOAD" = "true" ]; then
        args+=(--reload)
    fi

    nohup "$python" "${args[@]}" \
        > "$STDOUT_LOG" 2> "$STDERR_LOG" &

    new_pid=$!
    echo "$new_pid" > "$PID_FILE"

    echo "[server] started PID $new_pid -- http://$HOST:$PORT"
    echo "[server] stdout: $STDOUT_LOG"
    echo "[server] stderr: $STDERR_LOG"

    sleep 2
    listening_pids=$(get_listening_pids)
    if [ -n "$listening_pids" ]; then
        echo "[server] listening PID: $listening_pids"
    else
        echo "[server] not listening yet; check logs with: ./scripts/vm/server.sh logs"
    fi
}

show_status() {
    listening_pids=$(get_listening_pids)
    server_pids=$(get_server_pids)

    if [ -n "$listening_pids" ]; then
        echo "[server] running: http://$HOST:$PORT"
        echo "[server] listening PID: $listening_pids"
    else
        echo "[server] stopped on port $PORT"
    fi

    if [ -n "$server_pids" ]; then
        echo "[server] related PID: $server_pids"
    fi
}

show_logs() {
    echo "[server] stderr tail: $STDERR_LOG"
    [ -f "$STDERR_LOG" ] && tail -n "$LINES" "$STDERR_LOG"

    echo "[server] stdout tail: $STDOUT_LOG"
    [ -f "$STDOUT_LOG" ] && tail -n "$LINES" "$STDOUT_LOG"
}

watch_logs() {
    [ -f "$STDERR_LOG" ] && tail -n "$LINES" -f "$STDERR_LOG" &
    [ -f "$STDOUT_LOG" ] && tail -n "$LINES" -f "$STDOUT_LOG" &
    wait
}

use_systemd() {
    if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files hanstock.service >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

if use_systemd; then
    case "$ACTION" in
        start)    sudo systemctl start hanstock ;;
        stop)     sudo systemctl stop hanstock ;;
        restart)  sudo systemctl restart hanstock ;;
        status)   systemctl status hanstock ;;
        logs)     journalctl -u hanstock -n "$LINES" --no-pager ;;
        tail)     journalctl -u hanstock -f ;;
        *)        echo "Usage: $0 {start|stop|restart|status|logs|tail}"; exit 1 ;;
    esac
    exit 0
fi

case "$ACTION" in
    start)    start_server ;;
    stop)     stop_server ;;
    restart)  stop_server; sleep 1; start_server ;;
    status)   show_status ;;
    logs)     show_logs ;;
    tail)     watch_logs ;;
    *)        echo "Usage: $0 {start|stop|restart|status|logs|tail}"; exit 1 ;;
esac
