"""DB 기반 전략 스케쥴 디스패처.

VM에서 단일 cron(예: */5 9-15 * * 1-5)이 이 모듈을 주기적으로 호출하면,
strategy_schedules 테이블에서 enabled 스케쥴을 읽어 실행 윈도우/주기 조건을
만족하는 전략만 run_scheduled_cycle로 돌린다. 전략별 cron을 따로 두지 않고
대시보드에서 등록/제어한 스케쥴 하나로 관리하기 위한 진입점이다.
"""
from __future__ import annotations

import sys
import time
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
from src.strategy_ids import (
    AI_STOCK_SCHEDULE_ID,
    INDEPENDENT_STOCK_SCHEDULE_IDS,
    ISOLATED_STOCK_STRATEGY_IDS,
    resolve_ai_schedule_strategy_ids,
)
from src.utils.logger import logger

_ISOLATED_STRATEGY_IDS = ISOLATED_STOCK_STRATEGY_IDS
_TRADER_SCHEDULE_STRATEGY_IDS = set()
_MAIN_SCHEDULE_IDS = frozenset({AI_STOCK_SCHEDULE_ID, *INDEPENDENT_STOCK_SCHEDULE_IDS})
_TRADER_DISPATCH_PRIORITY = {
    "ai_stock_default_v1": 10,
    "plunge_bounce_strategy": 20,
    "volatility_adaptive_momentum_strategy": 30,
    "heikin_ashi_scalping_strategy": 40,
}
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
    schedules = [
        schedule
        for schedule in list_strategy_schedules(enabled_only=True)
        if str(schedule.get("strategy_id") or "") in _MAIN_SCHEDULE_IDS
        or str(schedule.get("strategy_id") or "") == NARRATIVE_MOMENTUM_STRATEGY_ID
    ]
    if not schedules:
        logger.info("[dispatch] no enabled strategy schedules")
        return ran

    due_schedules = [sched for sched in schedules if is_schedule_due(sched)]
    explicit_ids = {
        str(sched.get("strategy_id") or "")
        for sched in due_schedules
        if str(sched.get("strategy_id") or "") != AI_STOCK_SCHEDULE_ID
    }
    expanded_schedules = []
    for sched in due_schedules:
        strategy_id = str(sched.get("strategy_id") or "")
        if strategy_id != AI_STOCK_SCHEDULE_ID:
            expanded_schedules.append(sched)
            continue
        resolved_ids = resolve_ai_schedule_strategy_ids([strategy_id])
        for resolved_id in resolved_ids:
            if resolved_id in explicit_ids:
                continue
            expanded_schedules.append({
                **sched,
                "strategy_id": resolved_id,
                "_schedule_strategy_id": strategy_id,
            })
    due_schedules = expanded_schedules
    due_schedules.sort(
        key=lambda sched: _TRADER_DISPATCH_PRIORITY.get(str(sched.get("strategy_id") or ""), 100)
    )
    for sched in due_schedules:
        strategy_id = sched.get("strategy_id")
        schedule_strategy_id = (
            sched.get("_schedule_strategy_id") or strategy_id
        )
        mode = str(sched.get("mode") or "execute")
        auto_approve = bool(sched.get("auto_approve"))
        started_at = datetime.now(KST)
        started_monotonic = time.monotonic()
        try:
            logger.info(
                f"[dispatch] running {strategy_id} (mode={mode}, auto_approve={auto_approve})"
            )
            if strategy_id == NARRATIVE_MOMENTUM_STRATEGY_ID:
                result = run_narrative_momentum_cycle(
                    save_candidates=(mode != "analysis_only"),
                    auto_collect=True,
                )
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
                market_blocks = []
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
                    blocked = (m_result.get("automation") or {}).get("blocked") or []
                    market_blocks.extend(f"{m}:{reason}" for reason in blocked)
                if market_errors:
                    result["errors"] = market_errors
                    result["status"] = "failed"
                    result["ok"] = False
                elif market_blocks:
                    result["blocked"] = market_blocks
                    result["status"] = "blocked"
                    result["ok"] = True
                else:
                    result["status"] = "completed"
                    result["ok"] = True
            else:
                result = run_scheduled_cycle(
                    mode,
                    auto_approve=auto_approve,
                    force_strategy_id=strategy_id,
                    allowed_categories=_allowed_categories_for_strategy(strategy_id),
                    persist_result=False,
                )
            completed_at = datetime.now(KST)
            duration_seconds = round(time.monotonic() - started_monotonic, 3)
            if not isinstance(result, dict):
                result = {"result": result}
            result.update({
                "scheduler_started_at": started_at.isoformat(),
                "scheduler_completed_at": completed_at.isoformat(),
                "duration_seconds": duration_seconds,
                "schedule_strategy_id": schedule_strategy_id,
            })
            if isinstance(result, dict) and (
                result.get("status") == "failed" or result.get("ok") is False
            ):
                raise RuntimeError(
                    f"scheduler result reported failure: {result.get('errors') or result}"
                )
            save_scheduler_result(mode, completed_at.isoformat(), result)
            mark_strategy_schedule_run(schedule_strategy_id)
            ran.append(strategy_id)
            if isinstance(result, dict) and result.get("status") == "blocked":
                logger.warning(
                    f"[dispatch] blocked {strategy_id} duration_seconds={duration_seconds}: "
                    f"{result.get('blocked')}"
                )
            else:
                logger.info(
                    f"[dispatch] done {strategy_id} duration_seconds={duration_seconds}"
                )
        except Exception as exc:  # noqa: BLE001
            completed_at = datetime.now(KST)
            duration_seconds = round(time.monotonic() - started_monotonic, 3)
            failure_result = {
                "strategy_id": strategy_id,
                "schedule_strategy_id": schedule_strategy_id,
                "status": "failed",
                "ok": False,
                "errors": [str(exc)],
                "scheduler_started_at": started_at.isoformat(),
                "scheduler_completed_at": completed_at.isoformat(),
                "duration_seconds": duration_seconds,
            }
            try:
                save_scheduler_result(mode, completed_at.isoformat(), failure_result)
            except Exception as save_exc:  # noqa: BLE001
                logger.error(
                    f"[dispatch] failed to save error result for {strategy_id}: {save_exc}"
                )
            failures.append(f"{strategy_id}: {exc}")
            logger.error(
                f"[dispatch] {strategy_id} failed duration_seconds={duration_seconds}: {exc}"
            )
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
