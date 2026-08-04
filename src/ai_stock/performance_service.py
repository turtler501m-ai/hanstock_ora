# -*- coding: utf-8 -*-
"""AI stock performance verification.

Performance rows are evaluation data only. They can recommend review, but never
auto-disable or auto-promote a strategy without a separate approval path.
"""
from __future__ import annotations

from src.db import ai_watchlist_repository as watchlist_repository
from src.db import ai_snapshot_repository as snapshot_repository

from typing import Any

MIN_SAMPLE = 5
EVALUATION_DAYS = 20


def run_update(market: str) -> dict[str, Any]:
    """Update candidate performance for each market.

    Uses provider daily close series when available. The candidate initial price
    remains the base price, and 1/5/20 day returns plus MFE/MAE are stored.
    """
    from src.ai_stock.market_data import get_provider
    from src.ai_stock.markets import markets_for_query


    provider = get_provider()
    updated = 0
    skipped = 0
    for m in markets_for_query(market):
        benchmarks = provider.index_series(m) or {}
        benchmark_series = next(iter(benchmarks.values()), []) if benchmarks else []
        benchmark_return = _horizon_return(benchmark_series, EVALUATION_DAYS)
        for w in watchlist_repository.list_watchlist(market=m):
            base = _num(w.get("initial_price"))
            symbol = w.get("symbol")
            if base <= 0 or not symbol:
                skipped += 1
                continue
            series = provider.daily_series(m, symbol) or []
            closes = [_num(v) for v in series if _num(v) > 0]
            if not closes:
                quote = provider.quote(m, symbol)
                if quote and quote.get("price") is not None:
                    closes = [_num(quote["price"])]
            if not closes:
                skipped += 1
                continue
            metrics = _metrics(base, closes, benchmark_return=benchmark_return)
            snapshot_repository.save_performance(w["candidate_id"], {
                "market": m,
                "base_price": base,
                "base_date": w.get("created_at"),
                "price_1d": metrics["price_1d"],
                "return_1d": metrics["return_1d"],
                "price_5d": metrics["price_5d"],
                "return_5d": metrics["return_5d"],
                "price_20d": metrics["price_20d"],
                "return_20d": metrics["return_20d"],
                "mfe": metrics["mfe"],
                "mae": metrics["mae"],
                "benchmark_return": metrics["benchmark_return"],
                "rule_only_result": metrics["rule_only_result"],
                "actually_entered": 0,
                "evaluation_complete": 1 if len(closes) >= EVALUATION_DAYS else 0,
            })
            updated += 1
    return {"market": market, "updated": updated, "skipped": skipped}


def summarize(rows: list[dict[str, Any]], market: str) -> dict[str, Any]:
    complete = [r for r in rows if r.get("evaluation_complete")]
    if len(complete) < MIN_SAMPLE:
        return {
            "market": market,
            "status": "insufficient_sample",
            "complete_sample": len(complete),
            "min_sample": MIN_SAMPLE,
        }
    ret1 = _avg([r.get("return_1d") for r in complete])
    ret5 = _avg([r.get("return_5d") for r in complete])
    ret20 = _avg([r.get("return_20d") for r in complete])
    bench = _avg([r.get("benchmark_return") for r in complete])
    hit = [r for r in complete if (r.get("return_5d") or 0) > 0]
    excess = None
    if ret20 is not None and bench is not None:
        excess = round(ret20 - bench, 3)
    recommendation = None
    if excess is not None and excess < 0:
        recommendation = "review_required"
    return {
        "market": market,
        "status": "ok",
        "complete_sample": len(complete),
        "avg_return_1d": ret1,
        "avg_return_5d": ret5,
        "avg_return_20d": ret20,
        "benchmark_return": bench,
        "excess_vs_benchmark": excess,
        "hit_rate": round(len(hit) / len(complete), 3) if complete else None,
        "mfe_avg": _avg([r.get("mfe") for r in complete]),
        "mae_avg": _avg([r.get("mae") for r in complete]),
        "recommendation": recommendation,
    }


def _metrics(base: float, closes: list[float], *, benchmark_return: float | None) -> dict[str, Any]:
    upto_20 = closes[:EVALUATION_DAYS] if len(closes) >= EVALUATION_DAYS else closes
    return {
        "price_1d": _price_at(closes, 1),
        "return_1d": _return_from_base(base, _price_at(closes, 1)),
        "price_5d": _price_at(closes, 5),
        "return_5d": _return_from_base(base, _price_at(closes, 5)),
        "price_20d": _price_at(closes, EVALUATION_DAYS),
        "return_20d": _return_from_base(base, _price_at(closes, EVALUATION_DAYS)),
        "mfe": _return_from_base(base, max(upto_20) if upto_20 else None),
        "mae": _return_from_base(base, min(upto_20) if upto_20 else None),
        "benchmark_return": benchmark_return,
        "rule_only_result": {
            "horizon_days": min(len(closes), EVALUATION_DAYS),
            "complete": len(closes) >= EVALUATION_DAYS,
        },
    }


def _price_at(closes: list[float], days: int) -> float | None:
    if not closes:
        return None
    idx = min(max(days - 1, 0), len(closes) - 1)
    return closes[idx]


def _return_from_base(base: float, price: float | None) -> float | None:
    if base <= 0 or price is None:
        return None
    return round((float(price) / base - 1.0) * 100.0, 3)


def _horizon_return(series: list[float], days: int) -> float | None:
    vals = [_num(v) for v in series if _num(v) > 0]
    if len(vals) < days or vals[0] <= 0:
        return None
    return round((vals[days - 1] / vals[0] - 1.0) * 100.0, 3)


def _avg(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
