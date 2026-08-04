# -*- coding: utf-8 -*-
"""AI stock execution plan generation.

Creating a plan never submits an order. It validates the candidate, calculates
position size, records safety checks, and leaves approval/execution to the
automation service or dashboard route.
"""
from __future__ import annotations

from src.db import ai_scan_repository as scan_repository
from src.db import ai_watchlist_repository as watchlist_repository
from src.db import ai_execution_repository as execution_repository

import math
import os
from datetime import datetime, time
from typing import Any

from src.ai_stock.constants import WATCH_CONFIRMED, WATCH_EXECUTION_PLANNED
from src.ai_stock.freshness import is_stale, now as _now


ACTIVE_PLAN_STATUSES = {"planned", "approval_queued", "approved", "submitted"}


def create_plan(candidate_id: int, *, options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    watch = watchlist_repository.get_watch(candidate_id)
    candidate = scan_repository.get_candidate(candidate_id)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return ok

    if watch is None or candidate is None:
        raise ValueError("watch/candidate not found")
    market = watch["market"]

    check("watch_confirmed", watch["status"] == WATCH_CONFIRMED, watch["status"])
    entry_price = float(options.get("entry_price") or candidate.get("current_price") or 0)
    stop_price = float(options.get("stop_price") or 0)
    take_profit = options.get("take_profit")

    pol = watchlist_repository.get_policy(candidate.get("strategy_id") or "ai_stock_default_v1", market) or {}
    stale = is_stale(candidate.get("data_as_of"), "ai_eval")
    check("fresh_data", (not stale) or _truthy(pol.get("allow_stale_data_trade")), "stale_allowed" if stale else "")
    check("entry_price", entry_price > 0, str(entry_price))
    check("stop_price", stop_price > 0, str(stop_price))
    rps = entry_price - stop_price
    check("risk_per_share_positive", rps > 0, str(rps))
    check("kill_switch_off", not _kill_switch_active())
    check("no_active_duplicate_plan", not _has_active_duplicate_plan(candidate_id), str(candidate_id))

    _check_liquidity(check, candidate, pol)
    if options.get("enforce_market_open") or _truthy(pol.get("auto_execute")):
        check("market_open", _market_open(market), market)

    blocking = [c["check"] for c in checks if not c["ok"]]
    if blocking:
        raise ValueError("plan blocked: " + ", ".join(blocking))

    risk_pct = float(pol.get("max_risk_per_trade_pct") or 1.0) / 100.0
    risk_budget = _capital() * risk_pct
    qty_by_risk = math.floor(risk_budget / rps) if rps > 0 else 0
    qty_by_cash = math.floor(_capital() / entry_price) if entry_price > 0 else 0
    account = _account_snapshot(market)
    qty_by_portfolio = _portfolio_quantity_cap(
        account,
        symbol=candidate.get("symbol"),
        entry_price=entry_price,
        policy=pol,
    )
    quantity = max(0, min(qty_by_risk, qty_by_cash, qty_by_portfolio))
    estimated_cost = round(entry_price * quantity, 2)

    _check_order_amount(check, market, estimated_cost)
    _check_portfolio_limits(check, account, candidate.get("symbol"), estimated_cost, pol, entry_price=entry_price)
    check("quantity_positive", quantity > 0, str(quantity))

    blocking = [c["check"] for c in checks if not c["ok"]]
    if blocking:
        raise ValueError("plan blocked: " + ", ".join(blocking))

    plan = {
        "candidate_id": candidate_id,
        "market": market,
        "symbol": candidate.get("symbol"),
        "strategy_id": candidate.get("strategy_id"),
        "strategy_version": candidate.get("strategy_version"),
        "action": "buy",
        "entry_price": entry_price,
        "stop_price": stop_price,
        "take_profit": float(take_profit) if take_profit else None,
        "risk_budget": round(risk_budget, 2),
        "quantity": quantity,
        "estimated_cost": estimated_cost,
        "safety_checks": checks,
        "status": "planned",
        "created_at": _now().isoformat(),
    }
    plan_id = execution_repository.save_execution_plan(plan)
    plan["id"] = plan_id
    watchlist_repository.update_watch_status(candidate_id, WATCH_EXECUTION_PLANNED, reason=f"plan #{plan_id}")
    return plan


def _capital() -> float:
    try:
        from src.config import config

        return float(getattr(config, "total_capital", 10_000_000) or 0)
    except Exception:
        return 0.0


def _kill_switch_active() -> bool:
    try:
        from src.strategy.risk import RiskEngine

        return RiskEngine().check_kill_switch()
    except Exception:
        return False


def _has_active_duplicate_plan(candidate_id: int) -> bool:
    for plan in execution_repository.list_execution_plans(limit=500):
        if int(plan.get("candidate_id") or 0) == int(candidate_id) and plan.get("status") in ACTIVE_PLAN_STATUSES:
            return True
    return False


def _check_liquidity(check, candidate: dict[str, Any], policy: dict[str, Any]) -> None:
    min_price = _num(policy.get("min_price"))
    if min_price > 0:
        check("min_price", _num(candidate.get("current_price")) >= min_price, str(min_price))
    min_market_cap = _num(policy.get("min_market_cap"))
    if min_market_cap > 0 and candidate.get("market_cap") is not None:
        check("min_market_cap", _num(candidate.get("market_cap")) >= min_market_cap, str(min_market_cap))
    min_value = _num(policy.get("min_avg_trading_value"))
    if min_value > 0 and candidate.get("avg_trading_value") is not None:
        check("min_avg_trading_value", _num(candidate.get("avg_trading_value")) >= min_value, str(min_value))


def _check_order_amount(check, market: str, estimated_cost: float) -> None:
    env_key = "AI_STOCK_MIN_ORDER_USD" if market == "US" else "AI_STOCK_MIN_ORDER_KRW"
    default = 1.0 if market == "US" else 5000.0
    min_order = _num(os.environ.get(env_key), default)
    check("min_order_amount", estimated_cost >= min_order, f"{estimated_cost}/{min_order}")


def _check_portfolio_limits(
    check,
    account: dict[str, Any],
    symbol: str | None,
    estimated_cost: float,
    policy: dict[str, Any],
    *,
    entry_price: float = 0.0,
) -> None:
    if not account.get("available"):
        check("account_snapshot", True, "unavailable_nonblocking")
        return
    cash = _num(account.get("cash"))
    total = _num(account.get("total_eval"))
    stock_eval = _num(account.get("stock_eval"))
    holdings = account.get("holdings") or []
    required_cash = estimated_cost if estimated_cost > 0 else entry_price
    check("cash_available", cash >= required_cash, f"{cash}/{required_cash}")
    if total <= 0:
        check("portfolio_total_positive", False, str(total))
        return
    max_position_pct = _num(policy.get("max_position_pct"), 10.0) / 100.0
    max_market_pct = _num(policy.get("max_market_exposure_pct"), 50.0) / 100.0
    current_symbol_value = sum(_num(h.get("value")) for h in holdings if str(h.get("symbol")) == str(symbol))
    check(
        "max_position_pct",
        (current_symbol_value + estimated_cost) / total <= max_position_pct,
        f"{round((current_symbol_value + estimated_cost) / total, 4)}/{max_position_pct}",
    )
    check(
        "max_market_exposure_pct",
        (stock_eval + estimated_cost) / total <= max_market_pct,
        f"{round((stock_eval + estimated_cost) / total, 4)}/{max_market_pct}",
    )


def _portfolio_quantity_cap(
    account: dict[str, Any],
    *,
    symbol: str | None,
    entry_price: float,
    policy: dict[str, Any],
) -> int:
    if entry_price <= 0 or not account.get("available"):
        return 10**12
    cash = _num(account.get("cash"))
    total = _num(account.get("total_eval"))
    stock_eval = _num(account.get("stock_eval"))
    holdings = account.get("holdings") or []
    if total <= 0:
        return 0

    max_position_pct = _num(policy.get("max_position_pct"), 10.0) / 100.0
    max_market_pct = _num(policy.get("max_market_exposure_pct"), 50.0) / 100.0
    current_symbol_value = sum(_num(h.get("value")) for h in holdings if str(h.get("symbol")) == str(symbol))

    position_room = max(0.0, (total * max_position_pct) - current_symbol_value)
    market_room = max(0.0, (total * max_market_pct) - stock_eval)
    available_room = max(0.0, min(cash, position_room, market_room))
    return math.floor(available_room / entry_price)


def _account_snapshot(market: str) -> dict[str, Any]:
    try:
        from src.ai_stock import portfolio_service

        return portfolio_service._account_snapshot(market)  # central broker parser, best-effort
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _market_open(market: str) -> bool:
    if market == "US":
        try:
            from src.mistock.scheduler import is_us_market_open

            return bool(is_us_market_open())
        except Exception:
            return False
    current = _now()
    if current.weekday() >= 5:
        return False
    t = current.time()
    return time(9, 0) <= t <= time(15, 30)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
