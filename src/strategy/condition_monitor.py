"""Intraday condition/leader monitor feeding fresh scan universes."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from src.runtime_state import runtime_state_store
from src.utils.logger import logger


STATE_KEY = "technical_condition_monitor_v1"


def save_condition_symbols(market: str, symbols: list[str], *, source: str) -> dict:
    normalized = list(dict.fromkeys(str(symbol or "").upper().strip() for symbol in symbols if symbol))
    if not normalized:
        return {
            "market": str(market).upper(),
            "symbols": [],
            "source": source,
            "updated_at_epoch": None,
            "saved": False,
        }
    state = runtime_state_store.get(STATE_KEY, {"markets": {}})
    row = {
        "market": str(market).upper(),
        "symbols": normalized,
        "source": source,
        "updated_at_epoch": time.time(),
        "saved": True,
    }
    state.setdefault("markets", {})[row["market"]] = row
    runtime_state_store.set(STATE_KEY, state)
    return row


def get_fresh_condition_symbols(
    market: str,
    *,
    max_age_seconds: float = 180,
    now: float | None = None,
) -> list[str]:
    state = runtime_state_store.get(STATE_KEY, {"markets": {}})
    row = (state.get("markets") or {}).get(str(market).upper()) or {}
    updated = float(row.get("updated_at_epoch") or 0)
    current = time.time() if now is None else float(now)
    if updated <= 0 or current - updated > max(1.0, float(max_age_seconds)):
        return []
    return list(row.get("symbols") or [])


def condition_monitor_status(*, max_age_seconds: float = 180, now: float | None = None) -> dict:
    state = runtime_state_store.get(STATE_KEY, {"markets": {}})
    current = time.time() if now is None else float(now)
    markets = {}
    for market in ("KR", "US"):
        row = (state.get("markets") or {}).get(market) or {}
        updated = float(row.get("updated_at_epoch") or 0)
        age = round(max(0.0, current - updated), 1) if updated > 0 else None
        markets[market] = {
            "source": row.get("source") or "",
            "symbol_count": len(row.get("symbols") or []),
            "updated_at_epoch": updated or None,
            "age_seconds": age,
            "fresh": bool(
                row.get("symbols")
                and updated > 0
                and age is not None
                and age <= max(1.0, float(max_age_seconds))
            ),
        }
    heartbeat_epoch = float(state.get("heartbeat_epoch") or 0)
    heartbeat_age = (
        round(max(0.0, current - heartbeat_epoch), 1)
        if heartbeat_epoch > 0
        else None
    )
    return {
        "running_data_available": any(row["symbol_count"] > 0 for row in markets.values()),
        "fresh": any(row["fresh"] for row in markets.values()),
        "max_age_seconds": max_age_seconds,
        "market_open": state.get("market_open") or {"KR": False, "US": False},
        "heartbeat_epoch": heartbeat_epoch or None,
        "heartbeat_age_seconds": heartbeat_age,
        "markets": markets,
    }


def run_condition_monitor_cycle(markets: set[str] | None = None) -> dict:
    active_markets = {"KR", "US"} if markets is None else {
        str(market).upper() for market in markets
    }
    result = {"KR": {"symbols": [], "source": ""}, "US": {"symbols": [], "source": ""}, "errors": []}
    if "KR" in active_markets:
        try:
            from src.trader import KIStockAPI
            from src.strategy.seven_split import _condition_search_universe

            api = KIStockAPI()
            symbols = _condition_search_universe(api)
            source = "kis_condition"
            if not symbols:
                symbols = api.get_volume_rank(top_n=50)
                source = "kis_volume_rank"
            result["KR"] = save_condition_symbols("KR", symbols, source=source)
        except Exception as exc:
            result["errors"].append(f"KR:{type(exc).__name__}:{exc}")
            logger.info(f"[CONDITION_MONITOR] KR unavailable: {exc}")

    if "US" in active_markets:
        try:
            from src.mistock.trader import _get_kis_client

            api = _get_kis_client()
            symbols = list(dict.fromkeys(
                api.get_overseas_volume_rank(excd="NAS", cnt=50)
                + api.get_overseas_volume_rank(excd="NYS", cnt=50)
            ))
            result["US"] = save_condition_symbols("US", symbols, source="kis_overseas_volume_rank")
        except Exception as exc:
            result["errors"].append(f"US:{type(exc).__name__}:{exc}")
            logger.info(f"[CONDITION_MONITOR] US unavailable: {exc}")
    result["ok"] = bool(result["KR"].get("symbols") or result["US"].get("symbols"))
    return result


def market_open_status() -> dict[str, bool]:
    kr_open = False
    us_open = False
    try:
        from src.utils.market_calendar import is_market_session

        now_kr = datetime.now(ZoneInfo("Asia/Seoul"))
        kr_open = bool(
            is_market_session("KR", now_kr)
            and now_kr.replace(hour=9, minute=0, second=0, microsecond=0)
            <= now_kr
            <= now_kr.replace(hour=15, minute=30, second=0, microsecond=0)
        )
    except Exception:
        pass
    try:
        from src.mistock.scheduler import is_us_market_open

        us_open = bool(is_us_market_open())
    except Exception:
        pass
    return {"KR": kr_open, "US": us_open}


def is_any_market_open() -> bool:
    return any(market_open_status().values())


def save_monitor_heartbeat(market_open: dict[str, bool]) -> None:
    state = runtime_state_store.get(STATE_KEY, {"markets": {}})
    state["market_open"] = {
        "KR": bool(market_open.get("KR")),
        "US": bool(market_open.get("US")),
    }
    state["heartbeat_epoch"] = time.time()
    runtime_state_store.set(STATE_KEY, state)


def run_forever(
    *,
    interval_seconds: float = 60,
    stop_requested: Callable[[], bool] | None = None,
) -> None:
    should_stop = stop_requested or (lambda: False)
    interval = max(10.0, float(interval_seconds or 60))
    while not should_stop():
        open_markets = market_open_status()
        save_monitor_heartbeat(open_markets)
        if any(open_markets.values()):
            run_condition_monitor_cycle({
                market for market, is_open in open_markets.items() if is_open
            })
        deadline = time.monotonic() + interval
        while not should_stop() and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
