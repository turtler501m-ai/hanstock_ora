from __future__ import annotations

from collections.abc import Callable
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from src.db.analysis_repository import (
    create_analysis_cycle,
    get_analysis_cycle,
    get_analysis_cycle_stage,
    get_latest_analysis_cycle,
    record_analysis_cycle_stage,
)
from src.strategy_ids import ISOLATED_STOCK_STRATEGY_IDS

ISOLATED_STRATEGY_IDS = ISOLATED_STOCK_STRATEGY_IDS
_capture_locks_guard = threading.Lock()
_capture_locks: dict[tuple[str, str], threading.Lock] = {}
KST = timezone(timedelta(hours=9))


class AnalysisCycleError(ValueError):
    pass


def is_common_dashboard_strategy(strategy_id: str | None) -> bool:
    return bool(strategy_id) and strategy_id not in ISOLATED_STRATEGY_IDS


def start_common_analysis_cycle(
    strategy_id: str,
    trading_env: str,
    *,
    mode: str = "analysis",
    account_captured_at: str | None = None,
) -> dict:
    if not is_common_dashboard_strategy(strategy_id):
        raise AnalysisCycleError(
            f"{strategy_id or 'missing strategy'} uses an isolated dashboard flow"
        )
    return create_analysis_cycle(
        strategy_id,
        trading_env,
        mode=mode,
        account_captured_at=account_captured_at,
    )


def resolve_common_analysis_cycle(
    strategy_id: str,
    trading_env: str,
    cycle_id: str | None = None,
) -> dict:
    if not is_common_dashboard_strategy(strategy_id):
        raise AnalysisCycleError(
            f"{strategy_id or 'missing strategy'} uses an isolated dashboard flow"
        )
    cycle = get_analysis_cycle(cycle_id) if cycle_id else None
    if cycle_id and cycle is None:
        raise AnalysisCycleError(f"analysis cycle not found: {cycle_id}")
    if cycle is not None and (
        cycle.get("strategy_id") != strategy_id
        or cycle.get("trading_env") != trading_env
    ):
        raise AnalysisCycleError("analysis cycle does not match strategy or trading environment")
    return cycle or get_latest_analysis_cycle(strategy_id, trading_env) or start_common_analysis_cycle(
        strategy_id,
        trading_env,
    )


def mark_common_analysis_stage(
    cycle_id: str,
    stage: str,
    *,
    status: str = "completed",
    details: dict | None = None,
    payload: dict | None = None,
) -> dict | None:
    return record_analysis_cycle_stage(
        cycle_id,
        stage,
        status=status,
        details=details,
        payload=payload,
    )


def get_common_analysis_stage(cycle_id: str, stage: str) -> dict | None:
    return get_analysis_cycle_stage(cycle_id, stage)


def get_latest_usable_analysis_cycle(
    strategy_id: str,
    trading_env: str,
    *,
    max_age_seconds: int = 900,
) -> dict | None:
    cycle = get_latest_analysis_cycle(strategy_id, trading_env)
    if cycle is None or cycle.get("status") == "failed":
        return None
    try:
        updated_at = datetime.fromisoformat(str(cycle.get("updated_at") or cycle.get("created_at")))
    except (TypeError, ValueError):
        return None
    if (datetime.now(KST) - updated_at).total_seconds() > max_age_seconds:
        return None
    return cycle


def load_or_capture_common_stage(
    cycle_id: str,
    stage: str,
    builder: Callable[[], Any],
    *,
    details: dict | None = None,
) -> Any:
    key = (cycle_id, stage)
    with _capture_locks_guard:
        lock = _capture_locks.setdefault(key, threading.Lock())
    with lock:
        stored = get_analysis_cycle_stage(cycle_id, stage)
        if stored is not None and stored.get("status") == "completed":
            return stored.get("payload")
        payload = builder()
        record_analysis_cycle_stage(
            cycle_id,
            stage,
            status="completed",
            details=details,
            payload=payload,
        )
        return payload
