# -*- coding: utf-8 -*-
"""AI stock portfolio summary.

KRW and USD are kept separated unless a caller explicitly adds FX conversion.
The summary includes broker/account state plus AI watchlist target hints.
"""
from __future__ import annotations

from src.db import ai_watchlist_repository as watchlist_repository

import os
from typing import Any

from src.ai_stock.freshness import now
from src.ai_stock.markets import currency_of, markets_for_query



def summary(market: str, display_currency: str = "LOCAL") -> dict[str, Any]:
    by_market = []
    for m in markets_for_query(market):
        account = _account_snapshot(m)
        watch = watchlist_repository.list_watchlist(market=m)
        candidates = [w for w in watch if w.get("status") in ("watching", "confirmed", "execution_planned")]
        holdings = account.get("holdings") or []
        total_eval = _num(account.get("total_eval"))
        by_market.append({
            "market": m,
            "currency": currency_of(m),
            "account_source": account.get("source"),
            "account_available": bool(account.get("available")),
            "data_as_of": account.get("data_as_of"),
            "cash": account.get("cash"),
            "stock_eval": account.get("stock_eval"),
            "total_eval": account.get("total_eval"),
            "pnl": account.get("pnl"),
            "holdings": holdings,
            "holding_count": len(holdings),
            "concentration": _concentration(holdings, total_eval),
            "watch_count": len(watch),
            "active_candidates": len(candidates),
            "suggested_weights": _suggest_weights(candidates),
            "error": account.get("error"),
            "note": "원통화 기준. KRW/USD 직접 합산은 하지 않음.",
        })
    return {
        "display_currency": display_currency,
        "by_market": by_market,
        "warning": "KRW/USD는 환율 명시 없이 직접 합산하지 않음.",
    }


def _suggest_weights(candidates: list[dict[str, Any]]) -> dict[str, float]:
    if not candidates:
        return {}
    scores = {c["symbol"]: max(0.0, float(c.get("current_score") or 0)) for c in candidates}
    total = sum(scores.values())
    if total <= 0:
        return {}
    return {sym: round(v / total, 4) for sym, v in scores.items()}


def _account_snapshot(market: str) -> dict[str, Any]:
    try:
        from src.online_access import is_online_access_blocked

        if is_online_access_blocked() or os.environ.get("HANSTOCK_TESTING") == "1":
            return {
                "available": False,
                "source": "online_blocked",
                "data_as_of": now().isoformat(),
                "cash": None,
                "stock_eval": None,
                "total_eval": None,
                "pnl": None,
                "holdings": [],
                "error": "online access blocked",
            }
        if market == "US":
            from src.mistock import trader as mistock_trader

            data = mistock_trader.get_balance()
            return _normalize_account(data, source=data.get("balance_source") or "mistock")
        if market == "KR":
            from src.api.kis_api import KIStockAPI
            from src.dashboard.services.balance_service import parse_balance

            raw = KIStockAPI(notify_errors=False).get_balance()
            data = parse_balance(raw)
            return _normalize_account(data, source="kis")
    except Exception as exc:
        return {
            "available": False,
            "source": "unavailable",
            "data_as_of": now().isoformat(),
            "cash": None,
            "stock_eval": None,
            "total_eval": None,
            "pnl": None,
            "holdings": [],
            "error": str(exc),
        }
    return {
        "available": False,
        "source": "unsupported",
        "data_as_of": now().isoformat(),
        "holdings": [],
    }


def _normalize_account(data: dict[str, Any], *, source: str) -> dict[str, Any]:
    holdings = [_normalize_holding(h) for h in (data.get("holdings") or []) if isinstance(h, dict)]
    return {
        "available": not bool(data.get("_error")),
        "source": source,
        "data_as_of": now().isoformat(),
        "cash": data.get("cash"),
        "stock_eval": data.get("stock_eval"),
        "total_eval": data.get("total_eval"),
        "pnl": data.get("pnl"),
        "holdings": holdings,
        "error": data.get("_error"),
    }


def _normalize_holding(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(item.get("symbol") or "").strip(),
        "name": item.get("name") or item.get("symbol"),
        "qty": item.get("qty"),
        "sellable_qty": item.get("sellable_qty"),
        "price": item.get("price"),
        "avg_price": item.get("avg_price"),
        "value": item.get("value"),
        "pnl": item.get("pnl"),
        "rt": item.get("rt"),
        "source": item.get("source"),
    }


def _concentration(holdings: list[dict[str, Any]], total_eval: float) -> dict[str, Any]:
    if total_eval <= 0:
        return {"max_symbol": None, "max_weight": None, "weights": {}}
    weights = {
        str(h.get("symbol")): round(_num(h.get("value")) / total_eval, 4)
        for h in holdings
        if h.get("symbol")
    }
    max_symbol = max(weights, key=weights.get) if weights else None
    return {
        "max_symbol": max_symbol,
        "max_weight": weights.get(max_symbol) if max_symbol else None,
        "weights": weights,
    }


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
