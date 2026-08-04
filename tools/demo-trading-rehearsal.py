from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import dashboard, trader  # noqa: E402
from src.strategy.seven_split import adjust_tick_size  # noqa: E402


def _check(key: str, ok: bool, message: str, *, critical: bool = True) -> dict[str, Any]:
    return {
        "key": key,
        "ok": bool(ok),
        "critical": bool(critical),
        "message": message,
    }


def _db_schema_check() -> dict[str, Any]:
    trader.init_db()
    required_columns = {
        "broker_order_id",
        "order_status",
        "filled_qty",
        "filled_price",
        "pre_order_qty",
        "response_msg",
        "broker_result",
    }
    with trader.connect_db() as conn:
        rows = conn.execute("PRAGMA table_info(trades)").fetchall()
    columns = {row[1] for row in rows}
    missing = sorted(required_columns - columns)
    return _check(
        "trade_tracking_schema",
        not missing,
        "trade tracking columns are present" if not missing else f"missing columns: {', '.join(missing)}",
    )


def _pending_order_tracking_check() -> dict[str, Any]:
    try:
        tracked = dashboard._load_trackable_order_trades()
    except Exception as exc:
        return _check("pending_order_tracking", False, f"failed to inspect pending order tracking: {exc}")
    return _check(
        "pending_order_tracking",
        True,
        f"trackable submitted/open orders: {len(tracked)}",
        critical=False,
    )


def _kis_read_only_checks(days: int) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        api = dashboard._get_api()
        balance = dashboard._get_balance_data(api, allow_cache=False)
        checks.append(
            _check(
                "kis_demo_balance",
                True,
                f"balance response holdings={len(balance.get('output1', []))} summary={len(balance.get('output2', []))}",
            )
        )
    except Exception as exc:
        checks.append(_check("kis_demo_balance", False, f"balance check failed: {exc}"))
        return checks

    try:
        start_date, end_date = dashboard._order_history_window(days)
        history = api.get_trade_history(start_date, end_date)
        checks.append(
            _check(
                "kis_demo_order_history",
                True,
                f"order history rows={len(history)} window={start_date}-{end_date}",
                critical=False,
            )
        )
    except Exception as exc:
        checks.append(_check("kis_demo_order_history", False, f"order history check failed: {exc}", critical=False))
    return checks


def _safe_limit_price(api, symbol: str, side: str, explicit_price: int) -> int:
    if explicit_price > 0:
        return adjust_tick_size(explicit_price)
    quote = api.get_quote(symbol)
    current = int(float(quote.get("current") or quote.get("ask1") or quote.get("bid1") or 0))
    if current <= 0:
        raise RuntimeError(f"Could not resolve quote for {symbol}: {quote}")
    if side == "buy":
        return adjust_tick_size(max(1, int(current * 0.97)))
    return adjust_tick_size(int(current * 1.03))


def _demo_order_rehearsal(
    *,
    symbol: str,
    side: str,
    qty: int,
    price: int,
    confirm: bool,
    sync_days: int,
    quote_lookup: bool = False,
) -> dict[str, Any]:
    readiness = dashboard.get_demo_trading_readiness()
    if not readiness["ready"]:
        return {
            "submitted": False,
            "ok": False,
            "reason": "demo trading readiness is not satisfied",
            "readiness": readiness,
        }
    if not symbol:
        raise ValueError("symbol is required for --demo-order")
    if side not in {"buy", "sell"}:
        raise ValueError("--side must be buy or sell")
    if qty <= 0:
        raise ValueError("--qty must be greater than 0")

    api = None
    if confirm or quote_lookup or price > 0:
        api = dashboard._get_api() if (confirm or quote_lookup) else None
    if price > 0:
        limit_price = adjust_tick_size(price)
    elif quote_lookup or confirm:
        api = api or dashboard._get_api()
        limit_price = _safe_limit_price(api, symbol, side, price)
    else:
        limit_price = 0
    plan = {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "limit_price": limit_price,
        "estimated_amount": limit_price * qty,
        "submitted": False,
        "requires_flag": "--confirm-demo-order",
    }
    if not confirm:
        return {
            "ok": True,
            "submitted": False,
            "reason": "dry rehearsal only; pass --confirm-demo-order to submit to KIS demo",
            "plan": plan,
        }

    api = api or dashboard._get_api()
    pre_order_qty = dashboard._current_holding_qty_from_balance(api, symbol)
    result = api.place_order(symbol, side, limit_price, qty)
    ok = result.get("rt_cd") == "0"
    response_msg = dashboard._approval_response_msg(result, ok=ok)
    trader.save_trade(
        symbol,
        symbol,
        side,
        qty,
        limit_price,
        "demo order rehearsal",
        ok,
        trader.runtime_flags().order_submission_enabled,
        broker_result=result,
        order_status="submitted" if ok else "failed",
        response_msg=response_msg,
        filled_qty=0,
        filled_price=0,
        pre_order_qty=pre_order_qty,
    )
    sync_result = None
    if ok:
        sync_result = dashboard._sync_order_status_from_history(api, days=sync_days)
    return {
        "ok": ok,
        "submitted": True,
        "plan": plan,
        "broker_result": result,
        "response_msg": response_msg,
        "order_status_sync": sync_result,
    }


def build_report(
    *,
    with_kis: bool = False,
    days: int = 7,
    check_db: bool = True,
    demo_order: dict[str, Any] | None = None,
) -> dict[str, Any]:
    readiness = dashboard.get_demo_trading_readiness()
    checks = list(readiness["checks"])
    if check_db:
        checks.append(_db_schema_check())
        checks.append(_pending_order_tracking_check())
    if with_kis:
        checks.extend(_kis_read_only_checks(days))
    order_rehearsal = None
    if demo_order:
        order_rehearsal = _demo_order_rehearsal(sync_days=days, quote_lookup=with_kis, **demo_order)

    critical_ready = all(item["ok"] for item in checks if item.get("critical"))
    return {
        "ok": critical_ready and (order_rehearsal is None or bool(order_rehearsal.get("ok"))),
        "recorded_at": datetime.now(trader.KST).isoformat(),
        "mode": "kis_demo_auto_rehearsal",
        "with_kis": bool(with_kis),
        "readiness": readiness,
        "checks": checks,
        "demo_order": order_rehearsal,
    }


def write_report(report: dict[str, Any], path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return output_path


def _print_text(report: dict[str, Any]) -> None:
    print(f"mode={report['mode']} ok={str(report['ok']).lower()} with_kis={str(report['with_kis']).lower()}")
    for check in report["checks"]:
        marker = "OK" if check["ok"] else "FAIL"
        critical = "critical" if check.get("critical") else "optional"
        print(f"[{marker}] {check['key']} ({critical}) - {check['message']}")
    if report.get("demo_order"):
        order = report["demo_order"]
        status = "submitted" if order.get("submitted") else "planned"
        print(f"demo_order={status} ok={str(order.get('ok')).lower()} reason={order.get('reason', order.get('response_msg', ''))}")
        if order.get("plan"):
            print(f"demo_order_plan={json.dumps(order['plan'], ensure_ascii=False)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS demo auto-trading rehearsal checks")
    parser.add_argument("--with-kis", action="store_true", help="run read-only KIS balance/order-history checks")
    parser.add_argument("--days", type=int, default=30, help="order-history lookback window for --with-kis")
    parser.add_argument("--no-db", action="store_true", help="skip local trade DB schema checks")
    parser.add_argument("--demo-order", action="store_true", help="prepare a tiny KIS demo order rehearsal")
    parser.add_argument("--confirm-demo-order", action="store_true", help="actually submit --demo-order to KIS demo")
    parser.add_argument("--symbol", default="005930", help="symbol for --demo-order")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy", help="side for --demo-order")
    parser.add_argument("--qty", type=int, default=1, help="quantity for --demo-order")
    parser.add_argument("--price", type=int, default=0, help="limit price for --demo-order; default derives a conservative limit")
    parser.add_argument(
        "--record",
        nargs="?",
        const=".runtime/demo-trading-rehearsal-last.json",
        help="write the full rehearsal report to .runtime or a custom JSON path",
    )
    parser.add_argument("--json", action="store_true", help="print JSON report")
    parser.add_argument("--allow-not-ready", action="store_true", help="always exit 0 after printing the report")
    args = parser.parse_args()

    demo_order = None
    if args.demo_order:
        demo_order = {
            "symbol": args.symbol.strip(),
            "side": args.side,
            "qty": args.qty,
            "price": args.price,
            "confirm": args.confirm_demo_order,
        }
    report = build_report(with_kis=args.with_kis, days=args.days, check_db=not args.no_db, demo_order=demo_order)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_text(report)
    if args.record:
        path = write_report(report, args.record)
        if not args.json:
            print(f"recorded={path}")
    if args.allow_not_ready:
        return 0
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
