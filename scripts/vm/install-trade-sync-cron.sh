#!/bin/bash
set -euo pipefail

TIME_SPEC="${1:-10 16 * * 1-5}"
CRON_TZ_VALUE="${HANSTOCK_CRON_TZ:-Asia/Seoul}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_NAME="$(basename "$ROOT_DIR")"
MARKER="${REPO_NAME//_/-}-trade-sync"
JOB="$TIME_SPEC cd $ROOT_DIR && $ROOT_DIR/scripts/vm/trade-sync.sh"

existing="$(mktemp)"
crontab -l 2>/dev/null | awk -v marker="$MARKER" '
    $0 == "# " marker " begin" { skip = 1; next }
    $0 == "# " marker " end" { skip = 0; next }
    skip != 1 { print }
' > "$existing" || true
{
    cat "$existing"
    echo "# $MARKER begin"
    echo "CRON_TZ=$CRON_TZ_VALUE"
    echo "$JOB"
    echo "# $MARKER end"
} | crontab -
rm -f "$existing"

echo "[cron] installed: CRON_TZ=$CRON_TZ_VALUE $JOB"
