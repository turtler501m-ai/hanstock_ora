# -*- coding: utf-8 -*-
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse
import src.dashboard.core as _core
from src.dashboard.core import *
globals().update({k: v for k, v in _core.__dict__.items() if not k.startswith('__')})

router = APIRouter(tags=["account"])


def _allocated_integer_amounts(total: int, weights: list[float]) -> list[int]:
    if not weights:
        return []
    allocated = []
    remaining = int(total)
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            amount = remaining
        else:
            amount = round(int(total) * max(0.0, float(weight)))
            remaining -= amount
        allocated.append(amount)
    return allocated


def _allocated_integer_quantities(total: int, requested: list[float]) -> list[int]:
    """Scale strategy ownership to whole domestic shares using largest remainders."""
    total = max(0, int(total))
    positive = [max(0.0, float(value)) for value in requested]
    requested_total = sum(positive)
    if total <= 0 or requested_total <= 0:
        return [0 for _ in positive]
    if requested_total <= total:
        return [int(value) for value in positive]

    exact = [total * value / requested_total for value in positive]
    allocated = [int(value) for value in exact]
    remaining = total - sum(allocated)
    remainder_order = sorted(
        range(len(exact)),
        key=lambda index: (-(exact[index] - allocated[index]), index),
    )
    for index in remainder_order[:remaining]:
        allocated[index] += 1
    return allocated


def _summarize_holding_strategies(parsed: dict) -> dict:
    """Allocate broker holdings to recorded strategies without exceeding broker quantity."""
    strategy_totals: dict[str, dict] = {}
    total_value = 0
    total_pnl = 0
    attributed_value = 0

    for holding in parsed.get("holdings", []):
        broker_qty = max(0.0, float(holding.get("qty") or 0))
        holding_value = int(holding.get("value") or 0)
        holding_pnl = int(holding.get("pnl") or 0)
        total_value += holding_value
        total_pnl += holding_pnl

        recorded = [
            {
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or item.get("id") or "").strip(),
                "qty": max(0.0, float(item.get("qty") or 0)),
            }
            for item in holding.get("strategies", [])
            if str(item.get("id") or "").strip() and float(item.get("qty") or 0) > 0
        ]
        allocated_strategy_qty = _allocated_integer_quantities(
            int(broker_qty),
            [item["qty"] for item in recorded],
        )
        allocations = [
            {
                "strategy_id": item["id"],
                "strategy_name": item["name"],
                "allocated_qty": float(qty),
            }
            for item, qty in zip(recorded, allocated_strategy_qty)
            if qty > 0
        ]
        allocated_qty = sum(item["allocated_qty"] for item in allocations)
        unattributed_qty = max(0.0, broker_qty - allocated_qty)
        # Proportional scaling can leave a tiny positive IEEE-754 remainder
        # (for example, 29 - 28.999999999999996).  It is not a real share and
        # must not create a visible "귀속 미확인 0주" allocation.
        if unattributed_qty > 1e-6:
            allocations.append({
                "strategy_id": "unattributed",
                "strategy_name": "귀속 미확인",
                "allocated_qty": unattributed_qty,
            })

        if broker_qty > 0:
            weights = [item["allocated_qty"] / broker_qty for item in allocations]
        else:
            weights = [0.0 for _ in allocations]
        allocated_values = _allocated_integer_amounts(holding_value, weights)
        allocated_pnls = _allocated_integer_amounts(holding_pnl, weights)

        for item, value, pnl in zip(allocations, allocated_values, allocated_pnls):
            item["allocated_qty"] = round(item["allocated_qty"], 4)
            item["evaluation_amount"] = value
            item["pnl"] = pnl
            item["is_loss"] = pnl < 0
            item["return_rate"] = round(
                pnl / (value - pnl) * 100,
                2,
            ) if value - pnl > 0 else 0.0

            strategy_id = item["strategy_id"]
            summary = strategy_totals.setdefault(strategy_id, {
                "strategy_id": strategy_id,
                "strategy_name": item["strategy_name"],
                "evaluation_amount": 0,
                "pnl": 0,
                "holding_count": 0,
                "loss_holding_count": 0,
                "profit_holding_count": 0,
                "_symbols": set(),
            })
            summary["evaluation_amount"] += value
            summary["pnl"] += pnl
            symbol = str(holding.get("symbol") or "")
            if symbol not in summary["_symbols"]:
                summary["_symbols"].add(symbol)
                summary["holding_count"] += 1
                if pnl < 0:
                    summary["loss_holding_count"] += 1
                elif pnl > 0:
                    summary["profit_holding_count"] += 1
            if strategy_id != "unattributed":
                attributed_value += value

        holding["strategy_allocations"] = allocations
        holding["pnl_status"] = (
            "loss" if holding_pnl < 0 else ("profit" if holding_pnl > 0 else "flat")
        )

    strategy_summary = []
    for summary in strategy_totals.values():
        summary.pop("_symbols", None)
        cost = summary["evaluation_amount"] - summary["pnl"]
        summary["return_rate"] = round(
            summary["pnl"] / cost * 100,
            2,
        ) if cost > 0 else 0.0
        summary["allocation_ratio"] = round(
            summary["evaluation_amount"] / total_value * 100,
            2,
        ) if total_value > 0 else 0.0
        summary["is_loss"] = summary["pnl"] < 0
        strategy_summary.append(summary)

    holdings = parsed.get("holdings", [])
    parsed["strategy_summary"] = sorted(
        strategy_summary,
        key=lambda item: (-item["evaluation_amount"], item["strategy_name"]),
    )
    parsed["holding_summary"] = {
        "total_count": len(holdings),
        "profit_count": sum(1 for item in holdings if item.get("pnl_status") == "profit"),
        "loss_count": sum(1 for item in holdings if item.get("pnl_status") == "loss"),
        "flat_count": sum(1 for item in holdings if item.get("pnl_status") == "flat"),
        "evaluation_amount": total_value,
        "pnl": total_pnl,
        "attribution_coverage": round(
            attributed_value / total_value * 100,
            2,
        ) if total_value > 0 else 0.0,
    }
    return parsed


def _attach_holding_strategies(parsed: dict) -> dict:
    """Attach best-effort strategy ownership reconstructed from successful trades."""
    from src.db.repository import load_ai_strategies

    names = {
        str(item.get("id")): str(item.get("name") or item.get("id"))
        for item in load_ai_strategies()
        if item.get("id")
    }
    names.setdefault("ai_rebalance", "AI 리밸런싱")
    ownership: dict[str, list[dict]] = {}
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT symbol, strategy_id,
                   SUM(CASE WHEN action = 'buy' THEN qty WHEN action = 'sell' THEN -qty ELSE 0 END) AS net_qty
            FROM trades
            WHERE ok = 1
              AND COALESCE(strategy_id, '') <> ''
              AND (? = '' OR env = ?)
            GROUP BY symbol, strategy_id
            HAVING net_qty > 0
            ORDER BY net_qty DESC, strategy_id
            """,
            (str(trader.runtime_flags().trading_env or ""), str(trader.runtime_flags().trading_env or "")),
        ).fetchall()
    for row in rows:
        sid = str(row["strategy_id"])
        ownership.setdefault(str(row["symbol"]), []).append({
            "id": sid,
            "name": names.get(sid, sid),
            "qty": int(row["net_qty"] or 0),
        })
    for holding in parsed.get("holdings", []):
        strategies = ownership.get(str(holding.get("symbol") or ""), [])
        holding["strategies"] = strategies
        holding["strategy_ids"] = [item["id"] for item in strategies]
        holding["strategy_names"] = [item["name"] for item in strategies]
    return _summarize_holding_strategies(parsed)


def _active_sell_approval_symbols() -> set[str]:
    trader.init_db()
    _init_approval_db()
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT a.symbol
            FROM approvals a
            WHERE a.action = 'sell'
              AND a.source IN (
                  'dashboard_holding_sell', 'dashboard_sell_all',
                  'dashboard_strategy_holding_sell', 'dashboard_strategy_sell_all'
              )
              AND COALESCE(a.symbol, '') <> ''
              AND (
                    a.status IN ('pending', 'executing')
                    OR (
                        a.status = 'executed'
                        AND EXISTS (
                            SELECT 1
                            FROM trades t
                            WHERE t.source_approval_id = a.id
                              AND t.id = (
                                  SELECT MAX(t2.id)
                                  FROM trades t2
                                  WHERE t2.source_approval_id = a.id
                              )
                              AND t.action = 'sell'
                              AND t.order_status IN ('submitted', 'open', 'partial')
                              AND t.qty > COALESCE(t.filled_qty, 0)
                        )
                    )
              )
            """
        ).fetchall()
    return {str(row["symbol"]) for row in rows}


def _hide_active_sell_approval_holdings(parsed: dict) -> dict:
    active_symbols = _active_sell_approval_symbols()
    holdings = parsed.get("holdings") or []
    for holding in holdings:
        holding["sell_pending"] = str(holding.get("symbol") or "") in active_symbols
    parsed["pending_sell_symbols"] = sorted(active_symbols)
    return parsed

@router.get("/api/health")
def health():
    missing = _required_env_missing()
    account_warning = _account_format_warning(trader.config.kistock_account)
    demo_readiness = _demo_trading_readiness()
    from src.db.repository import _load_token_usage
    return {
        "ok": not missing and not account_warning,
        "missing": missing,
        "account_warning": account_warning,
        "trading_env": trader.runtime_flags().trading_env,
        "dry_run": trader.runtime_flags().dry_run,
        "enable_live_trading": trader.runtime_flags().enable_live_trading,
        "require_approval": trader.runtime_flags().require_approval,
        "order_submission_enabled": trader.runtime_flags().order_submission_enabled,
        "real_orders_enabled": trader.runtime_flags().real_orders_enabled,
        "online_access_blocked": bool(getattr(trader.config, "online_access_blocked", False)),
        "circuit_breaker": KIStockAPI.circuit_status(),
        "active_model_version": getattr(trader.config, "active_model_version", "v1"),
        "ai_analysis": _ai_analysis_config(),
        "auto_approval_enabled": _auto_approval_enabled(),
        "demo_trading_ready": demo_readiness["ready"],
        "demo_trading_readiness": demo_readiness,
        "kill_switch_active": Path(".runtime/kill_switch.json").exists(),
        "dashboard_runtime": _runtime_dashboard_info(),
        "token_usage": _load_token_usage(),
    }




@router.get("/api/demo-trading/readiness")
def get_demo_trading_readiness():
    return _demo_trading_readiness()




@router.get("/api/mock-trading/summary")
def get_mock_trading_summary():
    """모의거래 성과 요약"""
    import os
    json_path = Path(".runtime/mock_trades.json")
    if not json_path.exists():
        return {
            "open_positions": 0,
            "closed_trades": 0,
            "total_pnl": 0,
            "win_rate": 0,
            "wins": 0,
            "losses": 0,
            "positions": [],
            "recent_trades": []
        }
    
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    positions = data.get("positions", [])
    history = data.get("history", [])
    
    total_pnl = sum(h.get("pnl", 0) for h in history)
    wins = sum(1 for h in history if h.get("pnl", 0) > 0)
    losses = sum(1 for h in history if h.get("pnl", 0) <= 0)
    total_trades = len(history)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    
    return {
        "open_positions": len(positions),
        "closed_trades": total_trades,
        "total_pnl": round(total_pnl, 4),
        "win_rate": round(win_rate, 1),
        "wins": wins,
        "losses": losses,
        "positions": positions,
        "recent_trades": history[-10:] if history else []
    }




@router.get("/api/mock-trading/positions")
def get_mock_trading_positions():
    """모의거래 현재 포지션"""
    import os
    from datetime import datetime
    import requests
    
    json_path = Path(".runtime/mock_trades.json")
    if not json_path.exists():
        return {"positions": []}
    
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    positions = data.get("positions", [])
    
    from src.online_access import is_online_access_blocked

    # 실시간 시세 조회
    try:
        if is_online_access_blocked():
            raise RuntimeError("online access blocked")
        resp = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
        current_price = float(resp.json()["price"])
    except:
        current_price = 0
    
    # PnL 계산
    for pos in positions:
        if pos.get("symbol") == "BTC" and current_price:
            entry = pos.get("entry_price", 0)
            qty = pos.get("qty", 0)
            if pos.get("side") == "LONG":
                pnl = (current_price - entry) * qty
            else:
                pnl = (entry - current_price) * qty
            pos["current_price"] = current_price
            pos["current_pnl"] = round(pnl, 4)
    
    return {"positions": positions, "current_price": current_price}




@router.get("/api/mock-trading/trades")
def get_mock_trading_trades(limit: int = 200):
    """모의거래 체결 내역"""
    json_path = Path(".runtime/mock_trades.json")
    if not json_path.exists():
        return {"trades": []}
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc), "trades": []}
    history = data.get("history", [])
    if not isinstance(history, list):
        history = []
    safe_limit = max(1, min(int(limit or 200), 1000))
    return {"trades": history[-safe_limit:][::-1]}




@router.get("/api/balance")
def get_balance():
    from src.online_access import is_online_access_blocked

    if is_online_access_blocked():
        balance_data = _load_balance_cache()
        if balance_data is None:
            raise HTTPException(status_code=503, detail="Online access is blocked and no balance snapshot is available")
        parsed = _parse_balance(balance_data)
        _hide_active_sell_approval_holdings(parsed)
        _attach_holding_strategies(parsed)
        for holding in parsed["holdings"]:
            holding.pop("_raw", None)
        parsed["_offline"] = True
        return parsed

    missing = _required_env_missing()
    if missing:
        if "KISTOCK_ACCOUNT_FORMAT" in missing:
            raise HTTPException(
                status_code=503,
                detail=(
                    "KISTOCK_ACCOUNT must be 8 digits, or 10 digits including "
                    "2-digit product code"
                ),
            )
        raise HTTPException(status_code=503, detail=f"Missing environment variables: {', '.join(missing)}")

    try:
        api = _get_api()
        balance_data = _get_balance_data(api)
        parsed = _parse_balance(balance_data)
        _hide_active_sell_approval_holdings(parsed)
        _attach_holding_strategies(parsed)
        for holding in parsed["holdings"]:
            holding.pop("_raw", None)
        if balance_data.get("_cache"):
            parsed["_cache"] = balance_data["_cache"]
        return parsed
    except SystemExit as e:
        raise HTTPException(status_code=502, detail=f"KIS API initialization failed: {e}") from e
    except KISAccountError as e:
        raise HTTPException(status_code=503, detail=f"KIS account setting is invalid. Check KISTOCK_ACCOUNT: {e}") from e
    except KISRateLimitError as e:
        raise HTTPException(status_code=429, detail=f"KIS API rate limit exceeded. Retry shortly: {e}") from e
    except RuntimeError as e:
        if "timed out" in str(e):
            raise HTTPException(status_code=504, detail=f"KIS balance API timed out after {BALANCE_FETCH_TIMEOUT_SECONDS:g}s") from e
        raise HTTPException(status_code=502, detail=f"KIS API request failed: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"KIS API request failed: {e}") from e




@router.get("/api/portfolio-optimizer")
def get_portfolio_optimizer():
    missing = _required_env_missing()
    if missing:
        raise HTTPException(status_code=503, detail=f"Missing environment variables: {', '.join(missing)}")

    def _build():
        api = _get_api()
        parsed = _parse_balance(_get_balance_data(api))
        holdings = _holding_history(api, parsed, n=120)
        capital = trader.operating_capital(parsed["total_eval"])
        return trader.generate_portfolio_optimizer_plan(holdings, capital)

    try:
        return snapshot_read_through("portfolio_optimizer", _build)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Portfolio optimizer failed: {e}") from e
