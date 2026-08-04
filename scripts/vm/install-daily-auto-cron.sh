#!/bin/bash
set -euo pipefail

TIME_SPEC="${1:-0 9,15 * * 1-5}"
CRON_TZ_VALUE="${HANSTOCK_CRON_TZ:-Asia/Seoul}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
JOB="$TIME_SPEC cd $ROOT_DIR && $ROOT_DIR/scripts/vm/daily-auto.sh"

existing="$(mktemp)"
crontab -l 2>/dev/null | awk '
    /# hanstock-daily-auto begin/ { skip = 1; next }
    /# hanstock-daily-auto end/ { skip = 0; next }
    skip != 1 { print }
' > "$existing" || true
{
    cat "$existing"
    echo "# hanstock-daily-auto begin"
    echo "CRON_TZ=$CRON_TZ_VALUE"
    echo "$JOB"
    echo "# hanstock-daily-auto end"
} | crontab -
rm -f "$existing"

echo "[cron] installed: CRON_TZ=$CRON_TZ_VALUE $JOB"
echo "[cron] current matching entries:"
crontab -l | awk '
    /# hanstock-daily-auto begin/ { show = 1 }
    show == 1 { print }
    /# hanstock-daily-auto end/ { show = 0 }
'
