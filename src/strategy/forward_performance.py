"""Forward performance reconstruction for paper-account strategy review.

The calculation intentionally uses recorded fills and as-of market closes.  It
does not predict returns or decide whether a strategy is valid.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any


CALC_VERSION = "daily-nav-v1"
BLOCKING_ISSUES = {
    "missing_market_close",
    "strategy_ownership_mismatch",
    "shared_symbol_attribution",
    "unprocessed_trade_after_last_session",
    "nav_unavailable",
}


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _close_on_or_before(rows: list[dict], session_date: str) -> float | None:
    eligible = [
        (str(row.get("date") or "")[:10], _as_float(row.get("close")))
        for row in rows
        if str(row.get("date") or "")[:10] <= session_date and _as_float(row.get("close")) > 0
    ]
    return max(eligible, key=lambda item: item[0])[1] if eligible else None


def _close_before(rows: list[dict], session_date: str) -> float | None:
    eligible = [
        (str(row.get("date") or "")[:10], _as_float(row.get("close")))
        for row in rows
        if str(row.get("date") or "")[:10] < session_date and _as_float(row.get("close")) > 0
    ]
    return max(eligible, key=lambda item: item[0])[1] if eligible else None


def _return_pct(current: float, base: float) -> float | None:
    if base <= 0:
        return None
    return round((current / base - 1) * 100, 2)


def _exact_close(rows: list[dict], session_date: str) -> float | None:
    values = [
        _as_float(row.get("close"))
        for row in rows
        if str(row.get("date") or "")[:10] == session_date
        and _as_float(row.get("close")) > 0
    ]
    return values[-1] if values else None


def _daily_nav_series(
    rows: list[dict],
    price_rows: dict[str, list[dict]],
    benchmark_rows: dict[str, list[dict]],
    cutoff: str,
) -> list[dict]:
    """Build flow-neutral daily NAV observations on finalized KOSPI sessions."""
    if not rows:
        return []
    start = min(str(row.get("ts") or "")[:10] for row in rows)
    sessions = sorted({
        str(row.get("date") or "")[:10]
        for row in benchmark_rows.get("KOSPI", [])
        if start <= str(row.get("date") or "")[:10] <= cutoff
    })
    if not sessions:
        return []
    ordered = sorted(rows, key=lambda row: str(row.get("ts") or ""))
    trade_index = 0
    cash = 0.0
    holdings: dict[str, dict[str, float]] = {}
    previous_equity = 0.0
    twr_index = 100.0
    running_peak = 100.0
    mdd_pct = 0.0
    chain_available = True
    result: list[dict] = []

    for session_date in sessions:
        external_flow = 0.0
        buy_amount = 0.0
        sell_amount = 0.0
        ownership_mismatch = False
        while trade_index < len(ordered):
            trade = ordered[trade_index]
            trade_date = str(trade.get("ts") or "")[:10]
            if trade_date > session_date:
                break
            trade_index += 1
            action = str(trade.get("action") or "").lower()
            symbol = str(trade.get("symbol") or "").strip()
            qty = int(_as_float(trade.get("qty")))
            price = _as_float(trade.get("price"))
            if action not in {"buy", "sell"} or not symbol or qty <= 0 or price <= 0:
                continue
            position = holdings.setdefault(symbol, {"qty": 0.0, "avg_cost": 0.0})
            if action == "buy":
                amount = qty * price
                added = max(0.0, amount - cash)
                cash += added - amount
                external_flow += added
                buy_amount += amount
                total_qty = position["qty"] + qty
                position["avg_cost"] = (
                    position["qty"] * position["avg_cost"] + amount
                ) / total_qty
                position["qty"] = total_qty
            else:
                sold_qty = min(float(qty), position["qty"])
                if sold_qty < qty:
                    ownership_mismatch = True
                cash += sold_qty * price
                sell_amount += sold_qty * price
                position["qty"] -= sold_qty

        market_value = 0.0
        missing_symbols = []
        for symbol, position in holdings.items():
            if position["qty"] <= 0:
                continue
            close = _exact_close(price_rows.get(symbol, []), session_date)
            if close is None:
                missing_symbols.append(symbol)
            else:
                market_value += position["qty"] * close
        issues = []
        if missing_symbols:
            issues.append("missing_market_close")
        if ownership_mismatch:
            issues.append("strategy_ownership_mismatch")
        equity = None if issues else cash + market_value
        denominator = previous_equity + external_flow
        daily_return = None
        if chain_available and equity is not None and denominator > 0:
            daily_return = equity / denominator - 1
            twr_index *= 1 + daily_return
            running_peak = max(running_peak, twr_index)
            drawdown_pct = (twr_index / running_peak - 1) * 100
            mdd_pct = min(mdd_pct, drawdown_pct)
        else:
            drawdown_pct = None
            chain_available = False
        if equity is not None:
            previous_equity = equity
        result.append({
            "session_date": session_date,
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "nav": round(equity, 2) if equity is not None else None,
            "external_flow": round(external_flow, 2),
            "buy_amount": round(buy_amount, 2),
            "sell_amount": round(sell_amount, 2),
            "daily_return_pct": round(daily_return * 100, 6) if daily_return is not None else None,
            "twr_index": round(twr_index, 6) if chain_available else None,
            "drawdown_pct": round(drawdown_pct, 6) if drawdown_pct is not None else None,
            "mdd_pct": round(mdd_pct, 6) if chain_available else None,
            "quality_issues": issues,
            "calc_version": CALC_VERSION,
        })
    if trade_index < len(ordered) and result:
        result[-1]["quality_issues"].append("unprocessed_trade_after_last_session")
        result[-1]["twr_index"] = None
        result[-1]["drawdown_pct"] = None
        result[-1]["mdd_pct"] = None
    return result


def _input_hash(
    rows: list[dict], prices: dict[str, list[dict]], benchmarks: dict[str, list[dict]], cutoff: str
) -> str:
    payload = {
        "cutoff": cutoff, "trades": rows, "prices": prices,
        "benchmarks": benchmarks, "version": CALC_VERSION,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _benchmark_twr(
    rows: list[dict], sessions: list[str], daily_nav: list[dict], field_prefix: str
) -> tuple[float | None, float | None]:
    index_value = 100.0
    peak = 100.0
    mdd = 0.0
    available = True
    for index, session_date in enumerate(sessions):
        previous = _close_before(rows, session_date)
        current = _exact_close(rows, session_date)
        if not available or previous is None or current is None:
            available = False
            daily_nav[index][f"{field_prefix}_twr_index"] = None
            daily_nav[index][f"{field_prefix}_drawdown_pct"] = None
            continue
        index_value *= current / previous
        peak = max(peak, index_value)
        drawdown = (index_value / peak - 1) * 100
        mdd = min(mdd, drawdown)
        daily_nav[index][f"{field_prefix}_twr_index"] = round(index_value, 6)
        daily_nav[index][f"{field_prefix}_drawdown_pct"] = round(drawdown, 6)
    return (
        (round(index_value - 100, 2), round(mdd, 2))
        if available else (None, None)
    )


def build_strategy_forward_performance(
    trades: list[dict],
    price_rows: dict[str, list[dict]],
    benchmark_rows: dict[str, list[dict]],
    *,
    as_of: str | None = None,
    strategy_names: dict[str, str] | None = None,
    reviews: dict[str, dict] | None = None,
) -> list[dict]:
    """Reconstruct strategy ledgers and cash-flow-matched benchmarks.

    A capital contribution is recorded only when a buy cannot be funded by the
    strategy's virtual cash.  The same contribution buys each benchmark at the
    close available on that session, avoiding an arbitrary initial-capital
    assumption.
    """

    kst = timezone(timedelta(hours=9))
    cutoff = str(as_of or datetime.now(kst).date().isoformat())[:10]
    names = strategy_names or {}
    review_map = reviews or {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    symbol_strategies: dict[str, set[str]] = defaultdict(set)
    for trade in trades:
        strategy_id = str(trade.get("strategy_id") or "unattributed").strip() or "unattributed"
        trade_date = str(trade.get("ts") or "")[:10]
        if trade_date and trade_date <= cutoff:
            grouped[strategy_id].append(trade)
            if str(trade.get("action") or "").lower() == "buy" and trade.get("symbol"):
                symbol_strategies[str(trade.get("symbol"))].add(strategy_id)

    results = []
    for strategy_id, rows in grouped.items():
        rows.sort(key=lambda row: str(row.get("ts") or ""))
        cash = 0.0
        contribution = 0.0
        realized_pnl = 0.0
        holdings: dict[str, dict[str, float]] = {}
        benchmark_units = {"KOSPI": 0.0, "KOSDAQ": 0.0}
        benchmark_contribution = {"KOSPI": 0.0, "KOSDAQ": 0.0}
        benchmark_missing_contribution = {"KOSPI": False, "KOSDAQ": False}
        missing_prices: set[str] = set()
        ownership_mismatch = False
        contribution_dates: list[str] = []
        processed_fill_count = 0

        for trade in rows:
            action = str(trade.get("action") or "").lower()
            symbol = str(trade.get("symbol") or "").strip()
            qty = int(_as_float(trade.get("qty")))
            price = _as_float(trade.get("price"))
            trade_date = str(trade.get("ts") or "")[:10]
            if action not in {"buy", "sell"} or not symbol or qty <= 0 or price <= 0:
                continue
            processed_fill_count += 1
            position = holdings.setdefault(symbol, {"qty": 0.0, "avg_cost": 0.0})
            if action == "buy":
                amount = qty * price
                added = max(0.0, amount - cash)
                if added > 0:
                    cash += added
                    contribution += added
                    contribution_dates.append(trade_date)
                    for code in benchmark_units:
                        # Intraday index prices are not stored.  Use only the
                        # previous finalized session close so a morning fill
                        # never consumes that day's future closing value.
                        close = _close_before(benchmark_rows.get(code, []), trade_date)
                        if close:
                            benchmark_units[code] += added / close
                            benchmark_contribution[code] += added
                        else:
                            benchmark_missing_contribution[code] = True
                total_qty = position["qty"] + qty
                position["avg_cost"] = (
                    position["qty"] * position["avg_cost"] + amount
                ) / total_qty
                position["qty"] = total_qty
                cash -= amount
            else:
                sold_qty = min(float(qty), position["qty"])
                if sold_qty < float(qty):
                    ownership_mismatch = True
                if sold_qty <= 0:
                    continue
                cash += sold_qty * price
                realized_pnl += sold_qty * (price - position["avg_cost"])
                position["qty"] -= sold_qty
                if position["qty"] <= 0:
                    position["qty"] = 0.0
                    position["avg_cost"] = 0.0

        market_value = 0.0
        open_positions = 0
        for symbol, position in holdings.items():
            if position["qty"] <= 0:
                continue
            close = _close_on_or_before(price_rows.get(symbol, []), cutoff)
            if close is None:
                missing_prices.add(symbol)
                continue
            market_value += position["qty"] * close
            open_positions += 1

        equity = cash + market_value if not missing_prices else None
        strategy_return = _return_pct(equity, contribution) if equity is not None else None
        benchmark_values: dict[str, float | None] = {}
        benchmark_returns: dict[str, float | None] = {}
        for code, units in benchmark_units.items():
            close = _close_on_or_before(benchmark_rows.get(code, []), cutoff)
            value = (
                units * close
                if close is not None and units > 0 and not benchmark_missing_contribution[code]
                else None
            )
            benchmark_values[code] = round(value, 2) if value is not None else None
            benchmark_returns[code] = (
                _return_pct(value, benchmark_contribution[code]) if value is not None else None
            )

        review = review_map.get(strategy_id) or {}
        quality_issues = []
        if strategy_id == "unattributed":
            quality_issues.append("strategy_unattributed")
        if missing_prices:
            quality_issues.append("missing_market_close")
        if ownership_mismatch:
            quality_issues.append("strategy_ownership_mismatch")
        shared_symbols = sorted(
            symbol for symbol in holdings
            if len(symbol_strategies.get(symbol, set())) > 1
        )
        if shared_symbols:
            quality_issues.append("shared_symbol_attribution")
        if not contribution:
            quality_issues.append("no_invested_capital")
        for code in benchmark_units:
            if benchmark_missing_contribution[code]:
                quality_issues.append(f"incomplete_{code.lower()}_contributions")
            elif benchmark_returns[code] is None:
                quality_issues.append(f"missing_{code.lower()}_benchmark")
        quality_issues.append("costs_not_included")
        quality_issues.append("benchmark_uses_previous_close")
        if "strategy_ownership_mismatch" in quality_issues or "shared_symbol_attribution" in quality_issues:
            strategy_return = None
            equity = None

        daily_nav = _daily_nav_series(rows, price_rows, benchmark_rows, cutoff)
        historical_nav_issues = {
            issue for item in daily_nav for issue in item.get("quality_issues", [])
        }
        for issue in sorted(historical_nav_issues):
            if issue not in quality_issues:
                quality_issues.append(issue)
        nav_last = daily_nav[-1] if daily_nav else None
        nav_blocked = bool(
            not nav_last
            or nav_last.get("twr_index") is None
            or any(issue in BLOCKING_ISSUES for issue in quality_issues)
        )
        if nav_blocked and not any(issue in BLOCKING_ISSUES for issue in quality_issues):
            quality_issues.append("nav_unavailable")
        blocking_issues = sorted({
            issue for issue in quality_issues if issue in BLOCKING_ISSUES
        })
        warnings = sorted({
            issue for issue in quality_issues if issue not in BLOCKING_ISSUES
        } | {"synthetic_cashflow"})
        twr_pct = None if nav_blocked else round(float(nav_last["twr_index"]) - 100, 2)
        max_drawdown_pct = None if nav_blocked else round(float(nav_last["mdd_pct"]), 2)
        nav_sessions = [item["session_date"] for item in daily_nav]
        kospi_twr_pct, kospi_mdd_pct = _benchmark_twr(
            benchmark_rows.get("KOSPI", []), nav_sessions, daily_nav, "kospi"
        )
        kosdaq_twr_pct, kosdaq_mdd_pct = _benchmark_twr(
            benchmark_rows.get("KOSDAQ", []), nav_sessions, daily_nav, "kosdaq"
        )
        for code, value in (("kospi", kospi_twr_pct), ("kosdaq", kosdaq_twr_pct)):
            if value is None:
                issue = f"missing_{code}_nav_sessions"
                if issue not in quality_issues:
                    quality_issues.append(issue)
                if issue not in warnings:
                    warnings.append(issue)

        results.append({
            "strategy_id": strategy_id,
            "strategy_name": names.get(strategy_id, strategy_id),
            "started_at": min(contribution_dates) if contribution_dates else str(rows[0].get("ts") or "")[:10],
            "as_of": cutoff,
            "net_contribution": round(contribution, 2),
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "current_equity": round(equity, 2) if equity is not None else None,
            "return_pct": strategy_return,
            "realized_pnl": round(realized_pnl, 2),
            "kospi_return_pct": benchmark_returns["KOSPI"],
            "kosdaq_return_pct": benchmark_returns["KOSDAQ"],
            "excess_vs_kospi_pct": round(strategy_return - benchmark_returns["KOSPI"], 2)
            if strategy_return is not None and benchmark_returns["KOSPI"] is not None else None,
            "excess_vs_kosdaq_pct": round(strategy_return - benchmark_returns["KOSDAQ"], 2)
            if strategy_return is not None and benchmark_returns["KOSDAQ"] is not None else None,
            "order_count": processed_fill_count,
            "open_position_count": open_positions,
            "review_decision": review.get("decision") or "monitor",
            "review_note": review.get("note") or "",
            "reviewed_at": review.get("reviewed_at"),
            "data_quality": "complete" if not quality_issues else "estimated",
            "reliable": not quality_issues,
            "quality_issues": quality_issues,
            "missing_price_symbols": sorted(missing_prices),
            "shared_symbol_symbols": shared_symbols,
            "attribution_method": "recorded_trade_strategy_id",
            "input_hash": _input_hash(rows, price_rows, benchmark_rows, cutoff),
            "calc_version": CALC_VERSION,
            "capital": {
                "basis": "synthetic_buy_shortfall",
                "synthetic_contribution": round(contribution, 2),
                "actual_deposit_known": False,
                "withdrawal": None,
            },
            "costs": {"status": "excluded", "total": None, "gross_or_net": "gross"},
            "returns": {
                "gross_capital_return_pct": strategy_return,
                "net_return_pct": None,
                "twr_pct": twr_pct,
                "kospi_twr_pct": kospi_twr_pct,
                "kosdaq_twr_pct": kosdaq_twr_pct,
                "excess_twr_vs_kospi_pct": round(twr_pct - kospi_twr_pct, 2)
                if twr_pct is not None and kospi_twr_pct is not None else None,
                "benchmark_kospi_pct": benchmark_returns["KOSPI"],
                "benchmark_kosdaq_pct": benchmark_returns["KOSDAQ"],
            },
            "nav": {
                "available": not nav_blocked,
                "current_index": nav_last.get("twr_index") if not nav_blocked else None,
                "peak_index": max(
                    (float(item["twr_index"]) for item in daily_nav if item.get("twr_index") is not None),
                    default=None,
                ),
                "max_drawdown_pct": max_drawdown_pct,
                "kospi_max_drawdown_pct": kospi_mdd_pct,
                "kosdaq_max_drawdown_pct": kosdaq_mdd_pct,
                "observations": len(daily_nav),
                "started_at": daily_nav[0]["session_date"] if daily_nav else None,
                "ended_at": daily_nav[-1]["session_date"] if daily_nav else None,
            },
            "daily_nav": daily_nav,
            "quality": {
                "status": "blocked" if blocking_issues else "usable_with_limits",
                "blocking_issues": blocking_issues,
                "warnings": warnings,
            },
        })

    return sorted(results, key=lambda row: (row["strategy_id"] == "unattributed", row["strategy_name"]))


__all__ = ["CALC_VERSION", "build_strategy_forward_performance"]
