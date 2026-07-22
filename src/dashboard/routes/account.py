# -*- coding: utf-8 -*-
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse
import src.dashboard.core as _core
from src.dashboard.core import *
globals().update({k: v for k, v in _core.__dict__.items() if not k.startswith('__')})

router = APIRouter(tags=["account"])


def _attach_holding_strategies(parsed: dict) -> dict:
    """Attach best-effort strategy ownership reconstructed from successful trades."""
    from src.db.repository import load_ai_strategies

    names = {
        str(item.get("id")): str(item.get("name") or item.get("id"))
        for item in load_ai_strategies()
        if item.get("id")
    }
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
            (str(trader.TRADING_ENV or ""), str(trader.TRADING_ENV or "")),
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
    return parsed


def _active_sell_approval_symbols() -> set[str]:
    _init_approval_db()
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT symbol
            FROM approvals
            WHERE action = 'sell'
              AND status IN ('pending', 'executing', 'executed')
              AND source IN ('dashboard_holding_sell', 'dashboard_sell_all')
              AND COALESCE(symbol, '') <> ''
            """
        ).fetchall()
    return {str(row["symbol"]) for row in rows}


def _hide_active_sell_approval_holdings(parsed: dict) -> dict:
    active_symbols = _active_sell_approval_symbols()
    if not active_symbols:
        return parsed
    holdings = parsed.get("holdings") or []
    parsed["holdings"] = [
        holding for holding in holdings
        if str(holding.get("symbol") or "") not in active_symbols
    ]
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
        "trading_env": trader.TRADING_ENV,
        "dry_run": trader.DRY_RUN,
        "enable_live_trading": trader.ENABLE_LIVE_TRADING,
        "require_approval": trader.REQUIRE_APPROVAL,
        "order_submission_enabled": trader.ORDER_SUBMISSION_ENABLED,
        "real_orders_enabled": trader.REAL_ORDERS_ENABLED,
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
