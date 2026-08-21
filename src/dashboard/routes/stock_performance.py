"""Performance HTTP handlers extracted from the legacy stock route module."""

import functools
import inspect

from fastapi import APIRouter
from src.dashboard.routes import stock as _stock
from src.dashboard.routes import stock_order as _order

def _refresh_legacy_dependencies() -> None:
    protected = {"router", "_refresh_legacy_dependencies", "_CompatRouter", "_stock", "_order"}
    globals().update({
        name: value for name, value in vars(_order).items()
        if name not in protected and not name.startswith("__")
    })
    globals().update({
        name: value for name, value in vars(_stock).items()
        if name not in protected and not name.startswith("__")
    })


class _CompatRouter(APIRouter):
    def api_route(self, path: str, **kwargs):
        register = super().api_route(path, **kwargs)
        def decorator(endpoint):
            if inspect.iscoroutinefunction(endpoint):
                @functools.wraps(endpoint)
                async def dispatch(*args, **inner_kwargs):
                    _refresh_legacy_dependencies()
                    return await endpoint(*args, **inner_kwargs)
            else:
                @functools.wraps(endpoint)
                def dispatch(*args, **inner_kwargs):
                    _refresh_legacy_dependencies()
                    return endpoint(*args, **inner_kwargs)
            register(dispatch)
            return endpoint
        return decorator


_refresh_legacy_dependencies()
router = _CompatRouter(tags=["stock", "stock-performance"])

@router.get("/api/performance/periodic")
def get_periodic_performance(response: Response, strategy_id: str | None = None):
    _refresh_legacy_dependencies()
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    try:
        trades = _load_merged_trades()
        if strategy_id:
            trades = [trade for trade in trades if str(trade.get("strategy_id") or "") == strategy_id]
        result = _build_periodic_performance(trades)
        # Historical holding changes come from stored daily prices. The
        # current session is more accurately available from the live broker
        # balance, so expose it in the cumulative-performance table/chart.
        if not strategy_id:
            try:
                parsed = _parse_balance(_get_balance_data(_get_api()))
                current_change = parsed.get("holding_daily_change_pct")
                if current_change is not None:
                    today = trader.datetime.now(trader.KST).strftime("%Y-%m-%d")
                    month = today[:7]
                    holdings = parsed.get("holdings") or []
                    for row in result.get("daily", []):
                        if row.get("period") == today:
                            row["holding_change_pct"] = current_change
                            row["holding_change_symbol_count"] = len(holdings)
                            row["holding_change_missing_count"] = 0
                    for row in result.get("monthly", []):
                        if row.get("period") == month:
                            row["holding_change_pct"] = current_change
                            row["holding_change_symbol_count"] = len(holdings)
                            row["holding_change_missing_count"] = 0
            except Exception:
                pass
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/performance/forward")
def get_forward_performance(response: Response, strategy_id: str | None = None):
    _refresh_legacy_dependencies()
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    try:
        trades = _load_merged_trades()
        strategies = _build_forward_strategy_performance(
            trades, strategy_id=strategy_id
        )
        account = _build_forward_account_performance(trades) if not strategy_id else None
        for row in strategies:
            row.pop("daily_nav", None)
        if account:
            account.pop("daily_nav", None)
        return {
            "schema_version": 2,
            "strategies": strategies,
            "account": account,
            "method": "cash_flow_matched_forward_ledger",
            "methodology": {
                "capital_basis": "synthetic_buy_shortfall",
                "return_method": "unitized_daily_nav",
                "benchmark_price": "previous_finalized_session_close",
                "costs": "excluded",
            },
            "manual_review_only": True,
        }
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/performance/forward/{strategy_id}/nav")
def get_forward_performance_nav(response: Response, strategy_id: str):
    _refresh_legacy_dependencies()
    from src.db.performance_repository import list_daily_nav
    response.headers["Cache-Control"] = "no-store"
    scope_type = "account" if strategy_id == "__account__" else "strategy"
    return {"strategy_id": strategy_id, "daily_nav": list_daily_nav(strategy_id, scope_type=scope_type)}


@router.patch("/api/performance/forward/{strategy_id}/review")
def update_forward_performance_review(
    strategy_id: str,
    payload: StrategyPerformanceReviewPayload,
):
    _refresh_legacy_dependencies()
    from src.db.performance_repository import save_strategy_performance_review
    from src.db.strategy_repository import load_ai_strategies
    from src.strategy_ids import AI_REBALANCE_STRATEGY_ID


    try:
        known_ids = {
            str(item.get("id")) for item in load_ai_strategies() if item.get("id")
        }
        known_ids.add(AI_REBALANCE_STRATEGY_ID)
        if strategy_id not in known_ids and strategy_id != "unattributed":
            raise ValueError(f"strategy not found: {strategy_id}")
        review = save_strategy_performance_review(
            strategy_id, payload.decision, payload.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "review": review, "trading_state_changed": False}


@router.get("/api/performance/account-cashflows")
def get_performance_account_cashflows(response: Response):
    _refresh_legacy_dependencies()
    from src.db.performance_repository import list_account_cashflows
    response.headers["Cache-Control"] = "no-store"
    return {"cashflows": list_account_cashflows(), "manual_confirmation_required": True}


@router.post("/api/performance/account-cashflows")
def save_performance_account_cashflow(payload: AccountCashflowPayload):
    _refresh_legacy_dependencies()
    from src.db.performance_repository import record_account_cashflow
    try:
        row = record_account_cashflow(
            external_ref=payload.external_ref,
            occurred_at=payload.occurred_at,
            amount=payload.amount,
            kind=payload.kind,
            confirmed=payload.confirmed,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "cashflow": row, "performance_recalculated": False}




@router.get("/api/performance")
def get_performance(response: Response, strategy_id: str | None = None):
    _refresh_legacy_dependencies()
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    try:
        trades = _account_trades(_load_merged_trades())
        if strategy_id:
            trades = [trade for trade in trades if str(trade.get("strategy_id") or "") == strategy_id]
        record_started_at = min((str(t.get("ts") or "") for t in trades if t.get("ts")), default="")

        total_trades = len(trades)
        success_count = sum(1 for t in trades if t.get("ok", False))
        success_rate = (success_count / total_trades * 100) if total_trades > 0 else 0

        holdings = {}
        realized_pnl = 0
        names = {}


        for t in trades:
            if not t.get("ok", False): continue
            sym = t["symbol"]
            qty = t["qty"]
            price = t["price"]

            # Skip invalid qty or price <= 0 trades to avoid avg_cost and realized_pnl distortion
            if qty <= 0 or price <= 0:
                continue

            names[sym] = t.get("name", sym)

            if sym not in holdings:
                holdings[sym] = {"qty": 0, "cost": 0.0}

            if t["action"] == "buy":
                total_qty = holdings[sym]["qty"] + qty
                total_cost = (holdings[sym]["qty"] * holdings[sym]["cost"]) + (qty * price)
                holdings[sym]["qty"] = total_qty
                holdings[sym]["cost"] = total_cost / total_qty if total_qty > 0 else 0
            elif t["action"] == "sell":
                sell_qty = min(qty, holdings[sym]["qty"])
                profit = (price - holdings[sym]["cost"]) * sell_qty
                realized_pnl += profit
                holdings[sym]["qty"] -= sell_qty
                if holdings[sym]["qty"] <= 0:
                    holdings[sym]["qty"] = 0
                    holdings[sym]["cost"] = 0

        # Explicitly calculate realized_pnl by summing daily periodic performance values to match daily performance view exactly
        try:
            periodic_perf = _build_periodic_performance(trades)
            realized_pnl = sum(day["realized_pnl"] for day in periodic_perf.get("daily", []))
        except Exception:
            pass

        # Fetch current prices to calculate evaluation PnL
        current_holdings = {}
        total_broker_pnl = 0
        holding_daily_change_pct = None
        try:
            api = _get_api()
            balance_data = _get_balance_data(api)
            parsed_balance = _parse_balance(balance_data)
            current_holdings = {h['symbol']: h for h in parsed_balance['holdings']}
            total_broker_pnl = parsed_balance.get("pnl", 0)
            holding_daily_change_pct = parsed_balance.get("holding_daily_change_pct")
        except Exception:
            pass

        # 사용자 요청: 불일치가 발생하면 증권사 잔고 정보에 맞춰 보정한다.
        # 자동매매 기록으로 추적한 보유량보다 증권사 실제 잔고를 우선한다.
        eval_details = []
        total_eval_pnl = total_broker_pnl

        if trader.config.dry_run:
            total_eval_pnl = 0
            for sym, data in holdings.items():
                if data["qty"] > 0:
                    current_price = data["cost"]
                    if sym in current_holdings:
                        current_price = current_holdings[sym]["price"]

                    else:
                        try:
                            q = api.get_quote(sym)
                            current_price = q["current"]
                        except Exception:
                            pass

                    eval_pnl = (current_price - data["cost"]) * data["qty"]
                    return_rate = ((current_price / data["cost"]) - 1) * 100 if data["cost"] > 0 else 0
                    total_eval_pnl += eval_pnl

                    eval_details.append({
                        "symbol": sym,
                        "name": names.get(sym, sym),
                        "qty": data["qty"],
                        "avg_cost": data["cost"],
                        "current_price": current_price,
                        "eval_pnl": int(eval_pnl),
                        "return_rate": round(return_rate, 2),
                        "daily_change_pct": current_holdings.get(sym, {}).get("daily_change_pct"),
                        "broker_qty": current_holdings.get(sym, {}).get("qty", 0),
                        "broker_pnl": int(current_holdings.get(sym, {}).get("pnl", 0)),
                        "diff_reason": "DRY_RUN"
                    })
        else:
            for sym, ch in current_holdings.items():
                raw_stock = ch.get("_raw", {})
                avg_cost = float(raw_stock.get("pchs_avg_pric", 0)) if raw_stock.get("pchs_avg_pric") else 0

                if avg_cost == 0 and ch["qty"] > 0:
                    avg_cost = ch["price"] - (ch["pnl"] / ch["qty"])

                recorded_qty = holdings.get(sym, {}).get("qty", 0)
                diff_reason = ""
                if recorded_qty == 0:
                    diff_reason = "수동매수/기록누락 보정 완료"
                elif recorded_qty != ch["qty"]:
                    diff_reason = f"수량 불일치 {recorded_qty}주->{ch['qty']}주 보정 완료"

                return_rate = ((ch["price"] / avg_cost) - 1) * 100 if avg_cost > 0 else 0.0
                eval_details.append({
                    "symbol": sym,
                    "name": ch["name"],
                    "qty": ch["qty"],
                    "avg_cost": avg_cost,
                    "current_price": ch["price"],
                    "eval_pnl": int(ch["pnl"]),
                    "return_rate": round(return_rate, 2),
                    "daily_change_pct": ch.get("daily_change_pct"),
                    "broker_qty": ch["qty"],
                    "broker_pnl": int(ch["pnl"]),
                    "diff_reason": diff_reason
                })

        untracked_details = []  # 호환성을 위해 유지하며 상세 내용은 eval_details에 수집한다.

        return {
            "total_trades": total_trades,
            "success_rate": round(success_rate, 2),
            "realized_pnl": int(realized_pnl),
            "total_eval_pnl": int(total_eval_pnl),
            "total_broker_pnl": int(total_broker_pnl),
            "holding_daily_change_pct": holding_daily_change_pct,

            "eval_details": eval_details,
            "untracked_details": untracked_details,
            "record_started_at": record_started_at,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/decisions/history")
def get_decision_history(limit: int = 50):
    _refresh_legacy_dependencies()
    try:
        with trader.connect_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM decision_logs ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            logs = [dict(row) for row in rows]
            for log in logs:
                if isinstance(log.get("indicators"), str):
                    try:
                        log["indicators"] = json.loads(log["indicators"])
                    except (TypeError, ValueError):
                        pass
            return {"decisions": logs}
    except Exception:
        return {"decisions": []}
