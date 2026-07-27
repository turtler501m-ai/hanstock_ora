#!/bin/bash
# DB 기반 전략 스케쥴 디스패처 cron 등록기.
# 전략별 cron(install-plunge-bounce-cron.sh 등)을 대체한다. 이 디스패처 하나가
# strategy_schedules 테이블을 읽어 각 전략의 실행 윈도우/주기를 판단하므로,
# cron은 충분히 자주 호출만 하면 된다.
# 기본값은 정각(:00)을 피한 오프셋(:02,:07,...:57)이다. daily-auto cron이 매시
# 정각(0 9-15)에 돌므로, 겹쳐서 KIS API가 경합/중복 주문되지 않게 분리한다.
set -euo pipefail

TIME_SPEC="${1:-2-57/5 9-15 * * 1-5}"
CRON_TZ_VALUE="${HANSTOCK_CRON_TZ:-Asia/Seoul}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
JOB="$TIME_SPEC cd $ROOT_DIR && $ROOT_DIR/scripts/vm/strategy-dispatch.sh"

existing="$(mktemp)"
crontab -l 2>/dev/null | awk '
    /# hanstock-strategy-dispatch begin/ { skip = 1; next }
    /# hanstock-strategy-dispatch end/ { skip = 0; next }
    skip != 1 { print }
' > "$existing" || true
{
    cat "$existing"
    echo "# hanstock-strategy-dispatch begin"
    echo "CRON_TZ=$CRON_TZ_VALUE"
    echo "$JOB"
    echo "# hanstock-strategy-dispatch end"
} | crontab -
rm -f "$existing"

echo "[cron] installed: CRON_TZ=$CRON_TZ_VALUE $JOB"
echo "[cron] current matching entries:"
crontab -l | awk '
    /# hanstock-strategy-dispatch begin/ { show = 1 }
    show == 1 { print }
    /# hanstock-strategy-dispatch end/ { show = 0 }
'
