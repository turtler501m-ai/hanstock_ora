#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-status}"
UNIT="hanstock-autonomy.service"

if ! command -v systemctl >/dev/null 2>&1 ||
   ! systemctl list-unit-files "$UNIT" >/dev/null 2>&1; then
    echo "[autonomy] $UNIT is not installed; run install-autonomy-service.sh"
    exit 2
fi

case "$ACTION" in
    start|stop|restart) sudo systemctl "$ACTION" "$UNIT" ;;
    status) systemctl status "$UNIT" --no-pager ;;
    logs) journalctl -u "$UNIT" -n "${LINES:-80}" --no-pager ;;
    tail) journalctl -u "$UNIT" -f ;;
    *) echo "Usage: $0 {start|stop|restart|status|logs|tail}"; exit 1 ;;
esac
