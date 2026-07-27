"""DB 기반 전략 스케쥴 디스패처.

VM에서 단일 cron(예: */5 9-15 * * 1-5)이 이 모듈을 주기적으로 호출하면,
strategy_schedules 테이블에서 enabled 스케쥴을 읽어 실행 윈도우/주기 조건을
만족하는 전략만 run_scheduled_cycle로 돌린다. 전략별 cron을 따로 두지 않고
대시보드에서 등록/제어한 스케쥴 하나로 관리하기 위한 진입점이다.
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime

# Add project root to sys.path to allow running as a script directly
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db.repository import (
    is_schedule_due,
    list_strategy_schedules,
    mark_strategy_schedule_run,
    save_scheduler_result,
)
from src.scheduler import run_scheduled_cycle
from src.db.scheduler_repository import KST
from src.strategy.narrative_momentum import STRATEGY_ID as NARRATIVE_MOMENTUM_STRATEGY_ID
from src.strategy.narrative_momentum_runner import run_narrative_momentum_cycle
from src.utils.logger import logger


_ISOLATED_STRATEGY_IDS = {"plunge_bounce_strategy", "heikin_ashi_scalping_strategy"}
_TRADER_SCHEDULE_STRATEGY_IDS = {"issue_sector_rotation_strategy"}
_last_dispatch_failures: list[str] = []


def _is_registered_ai_strategy(strategy_id: str | None) -> bool:
    if not strategy_id:
        return False
    try:
        from src.db.repository import load_ai_strategies

        return any(
            str(item.get("id")) == str(strategy_id)
            for item in load_ai_strategies()
        )
    except Exception:
        return False


def _allowed_categories_for_strategy(strategy_id: str | None) -> set[str]:
    if strategy_id in _ISOLATED_STRATEGY_IDS:
        return {"candidate"}
    return {"position", "candidate", "ai_rebalance"}


def dispatch_due_schedules() -> list[str]:
    global _last_dispatch_failures
    _last_dispatch_failures = []
    ran: list[str] = []
    failures: list[str] = []
    schedules = list_strategy_schedules(enabled_only=True)
    if not schedules:
        logger.info("[dispatch] no enabled strategy schedules")
        return ran

    for sched in schedules:
        strategy_id = sched.get("strategy_id")
        if not is_schedule_due(sched):
            continue
        mode = str(sched.get("mode") or "execute")
        auto_approve = bool(sched.get("auto_approve"))
        try:
            logger.info(
                f"[dispatch] running {strategy_id} (mode={mode}, auto_approve={auto_approve})"
            )
            if strategy_id == NARRATIVE_MOMENTUM_STRATEGY_ID:
                result = run_narrative_momentum_cycle(
                    save_candidates=(mode != "analysis_only"),
                    auto_collect=True,
                )
                save_scheduler_result(mode, datetime.now(KST).isoformat(), result)
            elif (
                strategy_id not in _TRADER_SCHEDULE_STRATEGY_IDS
                and strategy_id not in _ISOLATED_STRATEGY_IDS
                and _is_registered_ai_strategy(strategy_id)
            ):
                # AI스톡: 주문 경로(run_scheduled_cycle)를 타지 않고 자동화 엔진을 호출한다(§5.12.2).
                from src.ai_stock.automation_service import run_strategy as _ai_run
                from src.ai_stock.realtime_service import run_realtime_cycle
                from src.ai_stock.markets import normalize_market, STORABLE_MARKETS

                raw_market = normalize_market(sched.get("market") or "KR", default="KR")
                # market=ALL은 KR로 좁히지 않고 두 시장(KR/US) 모두 순회한다.
                markets = STORABLE_MARKETS if raw_market == "ALL" else (raw_market,)
                result: dict = {"strategy_id": strategy_id, "market": raw_market, "by_market": {}}
                market_errors = []
                for m in markets:
                    # 한 시장의 실패가 다른 시장 실행/이력 저장/스케줄 갱신을 막지 않도록 시장별로 격리한다.
                    try:
                        m_result = _ai_run(market=m, strategy_id=strategy_id, run_type="scheduled")
                    except Exception as m_exc:
                        logger.error(f"[dispatch] {strategy_id} run_strategy failed for {m}: {m_exc}")
                        market_errors.append(f"{m}:{m_exc}")
                        result["by_market"][m] = {"error": str(m_exc)}
                        continue
                    # 2차 실시간 사이클(후보 풀 대상)도 같은 디스패치에서 best-effort 실행
                    try:
                        m_result["realtime"] = run_realtime_cycle(m, strategy_id=strategy_id)
                    except Exception as rt_exc:
                        logger.warning(f"[dispatch] {strategy_id} realtime cycle failed for {m}: {rt_exc}")
                    result["by_market"][m] = m_result
                if market_errors:
                    result["errors"] = market_errors
                    result["status"] = "failed"
                    result["ok"] = False
                save_scheduler_result(mode, datetime.now(KST).isoformat(), result)
            else:
                result = run_scheduled_cycle(
                    mode,
                    auto_approve=auto_approve,
                    force_strategy_id=strategy_id,
                    allowed_categories=_allowed_categories_for_strategy(strategy_id),
                )
            if isinstance(result, dict) and (
                result.get("status") == "failed" or result.get("ok") is False
            ):
                raise RuntimeError(
                    f"scheduler result reported failure: {result.get('errors') or result}"
                )
            mark_strategy_schedule_run(strategy_id)
            ran.append(strategy_id)
            logger.info(f"[dispatch] done {strategy_id}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{strategy_id}: {exc}")
            logger.error(f"[dispatch] {strategy_id} failed: {exc}")
    _last_dispatch_failures = failures
    return ran


def main() -> int:
    ran = dispatch_due_schedules()
    print(f"[dispatch] ran: {ran}")
    if _last_dispatch_failures:
        print(f"[dispatch] failures: {_last_dispatch_failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
