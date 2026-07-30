# -*- coding: utf-8 -*-
"""종목 유니버스 정책 (§4.6): ETF 포함 · 소형주 제외.

기준 수치는 정책(§6.6)/환경설정에서 변경 가능(§1.4). 제외 사유를 기록한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.ai_stock.constants import INSTRUMENT_ETF, MARKET_KR
from src.ai_stock.markets import require_storable_market

# 시장별 기본 하한 (계획서 §4.6 제안값; 정책에서 override).
DEFAULT_LIMITS = {
    "KR": {"min_market_cap": 3.0e11, "min_avg_trading_value": 5.0e9, "min_price": 5000.0},
    "US": {"min_market_cap": 2.0e9, "min_avg_trading_value": 2.0e7, "min_price": 5.0},
}


@dataclass
class UniversePolicy:
    market: str
    min_market_cap: float | None = None
    min_avg_trading_value: float | None = None
    min_price: float | None = None
    include_etf: bool = True
    exclude_small_cap: bool = True
    excluded_types: tuple[str, ...] = ()

    @classmethod
    def from_policy(cls, market: str, policy: dict[str, Any] | None) -> "UniversePolicy":
        market = require_storable_market(market)
        d = DEFAULT_LIMITS.get(market, DEFAULT_LIMITS["KR"])
        policy = policy or {}

        def pick(key, default):
            val = policy.get(key)
            return val if val is not None else default

        excluded = policy.get("excluded_types")
        if isinstance(excluded, str):
            excluded = tuple(x.strip() for x in excluded.split(",") if x.strip())
        return cls(
            market=market,
            min_market_cap=pick("min_market_cap", d["min_market_cap"]),
            min_avg_trading_value=pick("min_avg_trading_value", d["min_avg_trading_value"]),
            min_price=pick("min_price", d["min_price"]),
            include_etf=bool(pick("include_etf", True)),
            exclude_small_cap=bool(pick("exclude_small_cap", True)),
            excluded_types=tuple(excluded or ()),
        )


def _excluded_reason(item: dict[str, Any], p: UniversePolicy) -> str | None:
    itype = str(item.get("instrument_type") or "stock")
    if itype in p.excluded_types:
        return f"excluded_type:{itype}"
    if itype == INSTRUMENT_ETF and not p.include_etf:
        return "etf_disabled"
    if not p.exclude_small_cap:
        return None
    # 값이 있는 항목만 하한 검사(없으면 통과 — 데이터 부족은 후보 단계에서 처리).
    mc = item.get("market_cap")
    if mc is not None and p.min_market_cap and float(mc) < p.min_market_cap:
        return "small_cap:market_cap"
    av = item.get("avg_trading_value")
    if av is not None and p.min_avg_trading_value and float(av) < p.min_avg_trading_value:
        return "low_liquidity:trading_value"
    pr = item.get("price")
    if pr is not None and p.min_price and float(pr) < p.min_price:
        return "low_price"
    return None


def build(market: str, items: list[dict[str, Any]], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """유니버스 필터 적용 → 통과/제외 분리 (§4.6)."""
    p = UniversePolicy.from_policy(market, policy)
    passed, excluded = [], []
    for item in items or []:
        reason = _excluded_reason(item, p)
        if reason:
            excluded.append({"symbol": item.get("symbol"), "reason": reason})
        else:
            passed.append(item)
    return {
        "market": p.market,
        "policy": {
            "min_market_cap": p.min_market_cap,
            "min_avg_trading_value": p.min_avg_trading_value,
            "min_price": p.min_price,
            "include_etf": p.include_etf,
            "exclude_small_cap": p.exclude_small_cap,
        },
        "passed": passed,
        "excluded": excluded,
        "passed_count": len(passed),
        "excluded_count": len(excluded),
    }


def _load_policy(market: str) -> dict[str, Any] | None:
    try:
        from src.db import ai_stock_repository as repo

        pol = repo.get_policy("ai_stock_default_v1", market)
        return pol
    except Exception:
        return None


def describe(market: str) -> dict[str, Any]:
    """현재 유니버스 통과/제외 (API용, §7.3)."""
    market = require_storable_market(market)
    from src.ai_stock.market_data import get_provider

    items = get_provider().universe_items(market)
    if market == MARKET_KR:
        from src.db.repository import load_watchlist_data
        from src.strategy.watchlist_policy import filter_registered_items

        items = filter_registered_items(items, load_watchlist_data().get("symbols", []))
    return build(market, items, _load_policy(market))
