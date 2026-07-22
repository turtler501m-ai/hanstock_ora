#!/bin/bash
# [유튜브 24편 구현] 미국주식 자동매매 크론 스케줄 등록기
set -euo pipefail

# 미국장은 KST 기준 자정을 넘긴다. 단일 라인(0 21-23,0-5 * * 1-5)으로 두면
# cron 요일 필드(1-5)가 KST 캘린더 날짜 기준이라 금요일 미국장 마감 구간
# (KST 토요일 00:00~06:00)이 통째로 누락된다. 따라서 저녁/새벽 세션을 분리해
# 새벽 세션은 화~토(2-6)로 발화시켜 금요일 마감까지 커버한다. 최종 시간 판정은
# mistock-auto.sh의 should_run_now()가 다시 거른다.
EVENING_SPEC="${HANSTOCK_MISTOCK_EVENING_SPEC:-*/5 21-23 * * 1-5}"  # 전략별 주기 판정을 위한 5분 tick
MORNING_SPEC="${HANSTOCK_MISTOCK_MORNING_SPEC:-*/5 0-5 * * 2-6}"     # 전략별 주기 판정을 위한 5분 tick
EVENING_MONITOR_SPEC="${HANSTOCK_MISTOCK_EVENING_MONITOR_SPEC:-30 22-23 * * 1-5}"  # 모니터 저녁 세션 (월~금)
MORNING_MONITOR_SPEC="${HANSTOCK_MISTOCK_MORNING_MONITOR_SPEC:-30 0-5 * * 2-6}"     # 모니터 새벽 세션 (화~토)
CRON_TZ_VALUE="${HANSTOCK_CRON_TZ:-Asia/Seoul}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN="cd $ROOT_DIR && $ROOT_DIR/scripts/vm/mistock-auto.sh"
MONITOR_RUN="cd $ROOT_DIR && PYTHONPATH=. .venv/bin/python3 src/mistock/monitor.py >> logs/mistock_monitor.log 2>&1"

existing="$(mktemp)"
crontab -l 2>/dev/null | awk '
    /# hanstock-mistock-auto begin/ { skip = 1; next }
    /# hanstock-mistock-auto end/ { skip = 0; next }
    skip != 1 { print }
' > "$existing" || true
{
    cat "$existing"
    echo "# hanstock-mistock-auto begin"
    echo "CRON_TZ=$CRON_TZ_VALUE"
    echo "$EVENING_SPEC $RUN"
    echo "$MORNING_SPEC $RUN"
    echo "$EVENING_MONITOR_SPEC $MONITOR_RUN"
    echo "$MORNING_MONITOR_SPEC $MONITOR_RUN"
    echo "# hanstock-mistock-auto end"
} | crontab -
rm -f "$existing"

echo "[cron] Mistock US stock schedule installed: CRON_TZ=$CRON_TZ_VALUE"
echo "[cron]   evening: $EVENING_SPEC $RUN"
echo "[cron]   morning: $MORNING_SPEC $RUN"
echo "[cron]   evening monitor: $EVENING_MONITOR_SPEC $MONITOR_RUN"
echo "[cron]   morning monitor: $MORNING_MONITOR_SPEC $MONITOR_RUN"
echo "[cron] current matching entries:"
crontab -l | awk '
    /# hanstock-mistock-auto begin/ { show = 1 }
    show == 1 { print }
    /# hanstock-mistock-auto end/ { show = 0 }
'
