from __future__ import annotations


def to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def summary_item(summary):
    if isinstance(summary, list):
        return summary[0] if summary else {}
    if isinstance(summary, dict):
        return summary
    return {}


def clamp_ratio(value: float) -> float:
    return max(0.0, min(1.0, value))


def holding_value(stock: dict, qty: int, price: int) -> int:
    broker_value = to_int(stock.get("evlu_amt"))
    if broker_value > 0:
        return broker_value
    return qty * price


def portfolio_totals(cash: int, summary_total: int, holdings: list[dict]) -> dict:
    stock_eval = sum(to_int(holding.get("value")) for holding in holdings)
    broker_total = max(0, summary_total)
    calculated_total = cash + stock_eval
    effective_total = broker_total if broker_total > 0 else calculated_total
    if effective_total <= 0:
        effective_total = calculated_total
    return {
        "stock_eval": stock_eval,
        "broker_total_eval": broker_total,
        "calculated_total_eval": calculated_total,
        "total_eval": effective_total,
        "cash_ratio": clamp_ratio(cash / effective_total) if effective_total > 0 else 0.0,
        "stock_ratio": clamp_ratio(stock_eval / effective_total) if effective_total > 0 else 0.0,
    }


def parse_balance(balance_data: dict) -> dict:
    if balance_data.get("_error"):
        raise RuntimeError(balance_data["_error"])

    stocks = balance_data.get("output1", [])
    first_summary = summary_item(balance_data.get("output2", [{}]))

    holdings = []
    for stock in stocks:
        qty = to_int(stock.get("hldg_qty"))
        sellable_source = stock.get("hldg_qty")
        for key in (
            "ord_psbl_qty",
            "ord_psbl_qty1",
            "sell_psbl_qty",
            "sll_psbl_qty",
            "trad_psbl_qty",
            "able_qty",
        ):
            if key in stock and stock.get(key) not in (None, ""):
                sellable_source = stock.get(key)
                break
        sellable_qty = max(0, to_int(sellable_source))
        sellable_qty = min(qty, sellable_qty) if qty > 0 else 0
        price = to_int(stock.get("prpr"))
        value = holding_value(stock, qty, price)
        if price <= 0 and qty > 0:
            price = round(value / qty)
        holdings.append({
            "symbol": stock.get("pdno", ""),
            "name": stock.get("prdt_name", stock.get("pdno", "")),
            "qty": qty,
            "sellable_qty": sellable_qty,
            "price": price,
            "rt": to_float(stock.get("evlu_pfls_rt")),
            "daily_change_pct": to_float(stock.get("fltt_rt")),
            "pnl": to_int(stock.get("evlu_pfls_amt")),
            "value": value,
            "_raw": stock,
        })

    summary_total = to_int(first_summary.get("tot_evlu_amt"))
    summary_stock_eval = to_int(first_summary.get("scts_evlu_amt"))
    cash = to_int(first_summary.get("prvs_rcdl_excc_amt"))
    if cash == 0:
        cash = to_int(first_summary.get("dnca_tot_amt"))
    if cash == 0 and summary_total > 0:
        cash = summary_total - summary_stock_eval
    totals = portfolio_totals(cash, summary_total, holdings)
    previous_stock_eval = 0.0
    daily_change_amount = 0.0
    for holding in holdings:
        rate = float(holding.get("daily_change_pct") or 0.0) / 100.0
        value = float(holding.get("value") or 0.0)
        previous_value = value / (1.0 + rate) if rate > -1.0 else 0.0
        previous_stock_eval += previous_value
        daily_change_amount += value - previous_value
    return {
        "cash": cash,
        "total_eval": totals["total_eval"],
        "broker_total_eval": totals["broker_total_eval"],
        "calculated_total_eval": totals["calculated_total_eval"],
        "stock_eval": totals["stock_eval"],
        "cash_ratio": totals["cash_ratio"],
        "stock_ratio": totals["stock_ratio"],
        "pnl": to_int(first_summary.get("evlu_pfls_smtl_amt")),
        "holding_daily_change_pct": round(
            daily_change_amount / previous_stock_eval * 100, 2
        ) if previous_stock_eval > 0 else None,
        "holdings": holdings,
    }
