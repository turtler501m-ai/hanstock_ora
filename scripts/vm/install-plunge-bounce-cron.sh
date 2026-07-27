#!/bin/bash
set -euo pipefail

TIME_SPEC="${1:-*/15 9-15 * * 1-5}"
CRON_TZ_VALUE="${HANSTOCK_CRON_TZ:-Asia/Seoul}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
JOB="$TIME_SPEC cd $ROOT_DIR && $ROOT_DIR/scripts/vm/plunge-bounce-auto.sh"

existing="$(mktemp)"
crontab -l 2>/dev/null | awk '
    /# hanstock-plunge-bounce begin/ { skip = 1; next }
    /# hanstock-plunge-bounce end/ { skip = 0; next }
    skip != 1 { print }
' > "$existing" || true
{
    cat "$existing"
    echo "# hanstock-plunge-bounce begin"
    echo "CRON_TZ=$CRON_TZ_VALUE"
    echo "$JOB"
    echo "# hanstock-plunge-bounce end"
} | crontab -
rm -f "$existing"

echo "[cron] installed: CRON_TZ=$CRON_TZ_VALUE $JOB"
echo "[cron] current matching entries:"
crontab -l | awk '
    /# hanstock-plunge-bounce begin/ { show = 1 }
    show == 1 { print }
    /# hanstock-plunge-bounce end/ { show = 0 }
'
