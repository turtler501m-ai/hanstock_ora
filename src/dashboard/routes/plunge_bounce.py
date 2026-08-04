# -*- coding: utf-8 -*-
import json
import sqlite3
import threading
from datetime import datetime
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import RedirectResponse

from src.dashboard.core import (
    WEB_DIR,
    _scheduler_running_lock,
    _scheduler_run_state,
    _bg_run_scheduled_cycle,
)
from src import trader
from src.utils.logger import logger

router = APIRouter(tags=["strategies"])

STRATEGY_META = {
    "plunge_bounce": {
        "id": "plunge_bounce_strategy",
        "label": "급락반등",
        "last_result_path": ".runtime/plunge_bounce_last_result.json",
    },
    "heikin_ashi_scalping": {
        "id": "heikin_ashi_scalping_strategy",
        "label": "알파 하이킨아시",
        "last_result_path": ".runtime/heikin_ashi_scalping_last_result.json",
    },
}
EMPTY_UNIVERSE_MESSAGE = "Register strategy-specific watchlist symbols first."


def _strategy_meta(key: str) -> dict:
    meta = STRATEGY_META.get(key)
    if not meta:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return meta


def _strategy_performance(strategy_id: str) -> dict:
    trades = []
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades WHERE strategy_id = ? AND ok = 1 ORDER BY ts ASC",
            (strategy_id,),
        ).fetchall()
        trades = [dict(row) for row in rows]

    holdings = {}
    realized_pnl = 0.0
    winning_trades = 0
    losing_trades = 0
    pnl_by_date = {}
    pnl_by_month = {}
    total_sell_trades = 0

    for t in trades:
        symbol = t["symbol"]
        qty = t["qty"]
        price = t["price"]
        action = t["action"]
        date_str = t["ts"][:10]
        month_str = t["ts"][:7]

        if symbol not in holdings:
            holdings[symbol] = {"qty": 0, "cost": 0.0}

        if action == "buy":
            current_qty = holdings[symbol]["qty"]
            new_qty = current_qty + qty
            if new_qty > 0:
                holdings[symbol]["cost"] = (
                    (current_qty * holdings[symbol]["cost"]) + (qty * price)
                ) / new_qty
            holdings[symbol]["qty"] = new_qty
        elif action == "sell":
            current_qty = holdings[symbol]["qty"]
            sell_qty = min(qty, current_qty)
            if sell_qty > 0:
                profit = (price - holdings[symbol]["cost"]) * sell_qty
                realized_pnl += profit
                pnl_by_month[month_str] = pnl_by_month.get(month_str, 0.0) + profit
                total_sell_trades += 1
                if profit > 0:
                    winning_trades += 1
                elif profit < 0:
                    losing_trades += 1

                holdings[symbol]["qty"] -= sell_qty
                if holdings[symbol]["qty"] <= 0:
                    holdings[symbol]["qty"] = 0
                    holdings[symbol]["cost"] = 0.0

        pnl_by_date[date_str] = realized_pnl

    total_trades = len(trades)
    win_rate = (winning_trades / total_sell_trades * 100) if total_sell_trades > 0 else 0
    avg_profit = (realized_pnl / total_sell_trades) if total_sell_trades > 0 else 0

    return {
        "ok": True,
        "metrics": {
            "total_trades": total_trades,
            "sell_trades": total_sell_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": round(win_rate, 2),
            "realized_pnl": round(realized_pnl, 2),
            "avg_profit_per_trade": round(avg_profit, 2),
        },
        "daily_pnl": [
            {"date": d, "pnl": round(pnl, 2)}
            for d, pnl in sorted(pnl_by_date.items())
        ],
        "monthly_pnl": [
            {"month": m, "pnl": round(pnl, 2)}
            for m, pnl in sorted(pnl_by_month.items())
        ],
        "trades": trades[::-1],
    }


def _strategy_scan(strategy_id: str, *, min_score: float = 1.0) -> dict:
    from src.trader import KIStockAPI
    from src.strategy.seven_split import find_candidates
    from src.db.repository import save_scanned_candidate

    api = KIStockAPI()
    balance = api.get_balance()
    stocks = balance.get("output1", []) or []
    held_symbols = {s.get("pdno", "") for s in stocks}
    from src.db.repository import load_strategy_universe_symbols
    dedicated = load_strategy_universe_symbols(strategy_id)
    if not dedicated:
        return {
            "ok": True,
            "candidates": [],
            "scan_summary": [],
            "scanned_count": 0,
            "candidates_count": 0,
            "scan_error": f"{strategy_id} has no dedicated universe. {EMPTY_UNIVERSE_MESSAGE}",
        }
    scan_list = [code for code in dedicated if code not in held_symbols]

    logger.info(f"[StrategyRoute] {strategy_id} scan initiated for {len(scan_list)} symbols")
    scan_result = find_candidates(
        held_symbols,
        universe=scan_list,
        min_score=min_score,
        ranker="rule_only",
        api=api,
        strategy_model=strategy_id,
    )

    candidates = scan_result.get("candidates", [])
    scan_summary = scan_result.get("scan_summary", [])

    for s in scan_summary:
        save_scanned_candidate(
            symbol=s.get("ticker", ""),
            name=s.get("name", ""),
            score=s.get("score", 0.0),
            reasons=s.get("reasons", []),
            price=s.get("current_price", 0.0),
            env=trader.runtime_flags().trading_env,
            indicators={
                "rsi": s.get("rsi"),
                "rsi2": s.get("rsi2"),
                "sma20": s.get("sma20"),
                "sma60": s.get("sma60"),
                "macd_hist": s.get("macd_hist"),
            },
            strategy={"id": strategy_id},
            scoring={"final_score": s.get("score")},
        )

    summary_clean = []
    for s in scan_summary:
        summary_clean.append({
            "symbol": s.get("ticker"),
            "name": s.get("name"),
            "price": s.get("current_price"),
            "score": s.get("score"),
            "reasons": s.get("reasons"),
            "rsi": s.get("rsi"),
            "rsi2": s.get("rsi2"),
            "sma20": s.get("sma20"),
            "sma60": s.get("sma60"),
            "bb_lo": s.get("bb_lo"),
        })

    return {
        "ok": True,
        "candidates": candidates,
        "scan_summary": summary_clean,
        "scanned_count": len(scan_summary),
        "candidates_count": len(candidates),
    }


def _strategy_scans_history(strategy_id: str, limit: int = 100) -> dict:
    from src.db.repository import connect_db
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT scanned_at, symbol, name, score, reasons, price, env, rsi, rsi2, sma20
            FROM scanned_candidates
            WHERE strategy_id = ?
            ORDER BY scanned_at DESC
            LIMIT ?
            """,
            (strategy_id, limit),
        ).fetchall()
        return {"ok": True, "history": [dict(row) for row in rows]}


def _strategy_schedule_history(strategy_id: str, limit: int = 50) -> dict:
    from src.db.repository import connect_db
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT recorded_at, mode, result
            FROM scheduler_results
            WHERE strategy_id = ?
            ORDER BY recorded_at DESC
            LIMIT ?
            """,
            (strategy_id, limit),
        ).fetchall()

        parsed_history = []
        for row in rows:
            row_dict = dict(row)
            if row_dict.get("result"):
                try:
                    row_dict["result"] = json.loads(row_dict["result"])
                except Exception:
                    pass
            parsed_history.append(row_dict)
        return {"ok": True, "history": parsed_history}

@router.get("/plunge-bounce")
def read_plunge_bounce_dashboard():
    """Redirects to the main dashboard with plunge-bounce tab active."""
    return RedirectResponse(url="/?tab=plunge-bounce")


@router.get("/heikin-ashi-scalping")
def read_heikin_ashi_dashboard():
    """Redirects to the main dashboard with alpha Heikin Ashi tab active."""
    return RedirectResponse(url="/?tab=heikin-ashi")


@router.get("/api/strategy/plunge_bounce/performance")
def get_strategy_performance():
    """Calculates daily, monthly, and overall performance for plunge_bounce_strategy."""
    try:
        return _strategy_performance("plunge_bounce_strategy")
    except Exception as e:
        logger.error(f"[PlungeBounceRoute] Performance calculation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/strategy/plunge_bounce/scan")
def run_plunge_bounce_scan():
    """Triggers real-time scanning of the dedicated PlungeBounceStrategy universe."""
    try:
        from src.trader import KIStockAPI
        api = KIStockAPI()

        from src.strategy.seven_split import find_candidates

        # 1. Fetch currently held symbols to exclude them from the scan
        balance = api.get_balance()
        stocks = balance.get("output1", []) or []
        held_symbols = {s.get("pdno", "") for s in stocks}

        # 2. Use only the dedicated strategy universe.
        from src.db.repository import load_strategy_universe_symbols
        dedicated = load_strategy_universe_symbols("plunge_bounce_strategy")
        if not dedicated:
            return {
                "ok": True,
                "candidates": [],
                "scan_summary": [],
                "scanned_count": 0,
                "candidates_count": 0,
                "scan_error": (
                    "plunge_bounce_strategy has no dedicated universe. "
                    f"{EMPTY_UNIVERSE_MESSAGE}"
                ),
            }
        scan_list = [code for code in dedicated if code not in held_symbols]

        # 3. Perform scan forcing the plunge_bounce_strategy custom rule
        logger.info(f"[PlungeBounceRoute] Real-time scan initiated for {len(scan_list)} symbols")
        scan_result = find_candidates(
            held_symbols,
            universe=scan_list,
            min_score=1.0,  # score 5.0 is passed, 0.0 is skipped
            ranker="rule_only",
            api=api,
            strategy_model="plunge_bounce_strategy",
        )

        candidates = scan_result.get("candidates", [])
        scan_summary = scan_result.get("scan_summary", [])

        from src.db.repository import save_scanned_candidate
        # Persist scan details to DB for history tracking
        for s in scan_summary:
            save_scanned_candidate(
                symbol=s.get("ticker", ""),
                name=s.get("name", ""),
                score=s.get("score", 0.0),
                reasons=s.get("reasons", []),
                price=s.get("current_price", 0.0),
                env=trader.runtime_flags().trading_env,
                indicators={
                    "rsi": s.get("rsi"),
                    "rsi2": s.get("rsi2"),
                    "sma20": s.get("sma20"),
                    "sma60": s.get("sma60"),
                },
                strategy={"id": "plunge_bounce_strategy"},
                scoring={"final_score": s.get("score")}
            )

        # Sort summary to show highest-scoring or lowest-disparity first
        summary_clean = []
        for s in scan_summary:
            summary_clean.append({
                "symbol": s.get("ticker"),
                "name": s.get("name"),
                "price": s.get("current_price"),
                "score": s.get("score"),
                "reasons": s.get("reasons"),
                "rsi": s.get("rsi"),
                "rsi2": s.get("rsi2"),
                "sma20": s.get("sma20"),
                "bb_lo": s.get("bb_lo"),
            })

        return {
            "ok": True,
            "candidates": candidates,
            "scan_summary": summary_clean,
            "scanned_count": len(scan_summary),
            "candidates_count": len(candidates),
        }
    except Exception as e:
        logger.error(f"[PlungeBounceRoute] Scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/strategy/plunge_bounce/run-trader")
def run_plunge_bounce_trader(payload: dict = Body(...)):
    """Triggers an independent execution run cycle using plunge_bounce_strategy in the background."""
    global _scheduler_run_state
    mode = str(payload.get("mode", "execute")).lower()

    include_ai_rebalance = False
    auto_approve = bool(payload.get("auto_approve", True))

    # 2. Trigger scheduler in background thread forcing plunge_bounce_strategy
    with _scheduler_running_lock:
        if _scheduler_run_state["is_running"]:
            raise HTTPException(status_code=409, detail="스케줄러가 이미 실행 중입니다.")

        _scheduler_run_state["is_running"] = True
        _scheduler_run_state["mode"] = mode
        _scheduler_run_state["started_at"] = trader.datetime.now(trader.KST).isoformat()
        _scheduler_run_state["completed_at"] = None
        _scheduler_run_state["result"] = None
        _scheduler_run_state["error"] = None

    t = threading.Thread(
        target=_bg_run_scheduled_cycle,
        args=(mode, include_ai_rebalance, auto_approve, "plunge_bounce_strategy"),
        daemon=True,
    )
    t.start()
    return {"status": "started", "mode": mode}


@router.get("/api/strategy/plunge_bounce/scans-history")
def get_plunge_bounce_scans_history(limit: int = 100):
    """Retrieves the history of scanned candidates for plunge_bounce_strategy."""
    try:
        from src.db.repository import connect_db
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT scanned_at, symbol, name, score, reasons, price, env, rsi, rsi2, sma20
                FROM scanned_candidates
                WHERE strategy_id = 'plunge_bounce_strategy'
                ORDER BY scanned_at DESC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
            return {"ok": True, "history": [dict(row) for row in rows]}
    except Exception as e:
        logger.error(f"[PlungeBounceRoute] Failed to fetch scan history: {e}")
        return {"ok": False, "error": str(e)}


def _trim_scheduler_items(items, limit: int = 50):
    if not isinstance(items, list):
        return []
    return items[:limit]


def _summarize_candidate_scan(candidate_scan):
    if not isinstance(candidate_scan, dict):
        return {}
    candidates = candidate_scan.get("candidates")
    scan_summary = candidate_scan.get("scan_summary")
    scanned = candidate_scan.get("scanned", candidate_scan.get("scanned_count"))
    candidates_count = candidate_scan.get("candidates_count")
    if candidates_count is None and isinstance(candidates, list):
        candidates_count = len(candidates)
    return {
        "scanned": scanned,
        "scanned_count": scanned,
        "candidates_count": candidates_count,
        "candidates": _trim_scheduler_items(candidates, 20),
        "scan_error": candidate_scan.get("scan_error"),
        "summary_count": len(scan_summary) if isinstance(scan_summary, list) else candidate_scan.get("summary_count"),
    }


def _summarize_scheduler_result(result):
    if not isinstance(result, dict):
        return result
    return {
        "plan": _trim_scheduler_items(result.get("plan"), 50),
        "results": _trim_scheduler_items(result.get("results"), 50),
        "auto_approved": _trim_scheduler_items(result.get("auto_approved"), 50),
        "errors": _trim_scheduler_items(result.get("errors"), 10),
        "auto_approval_errors": _trim_scheduler_items(result.get("auto_approval_errors"), 10),
        "candidate_scan": _summarize_candidate_scan(result.get("candidate_scan")),
        "remaining_cash": result.get("remaining_cash"),
        "daily_loss_halt": result.get("daily_loss_halt"),
        "cash": result.get("cash"),
        "held_symbols": _trim_scheduler_items(result.get("held_symbols"), 50),
        "strategy_id": result.get("strategy_id"),
        "order_status_sync": result.get("order_status_sync"),
    }


@router.get("/api/strategy/plunge_bounce/schedule-history")
def get_plunge_bounce_schedule_history(limit: int = 10):
    """Retrieves the execution history of scheduler runs for plunge_bounce_strategy."""
    try:
        limit = max(1, min(int(limit), 50))
        from src.db.repository import connect_db
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT recorded_at, mode, result
                FROM scheduler_results
                WHERE strategy_id = 'plunge_bounce_strategy'
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
            
            # parse the JSON result string for convenience in the frontend
            parsed_history = []
            for row in rows:
                row_dict = dict(row)
                if row_dict.get("result"):
                    try:
                        row_dict["result"] = _summarize_scheduler_result(json.loads(row_dict["result"]))
                    except Exception:
                        pass
                parsed_history.append(row_dict)
            return {"ok": True, "history": parsed_history}
    except Exception as e:
        logger.error(f"[PlungeBounceRoute] Failed to fetch schedule history: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/strategy/plunge_bounce/settings")
def get_plunge_bounce_settings():
    """Retrieves the custom rule filters for the Plunge Bounce strategy from DB."""
    try:
        from src.db.repository import get_watchlist_setting
        return {
            "ok": True,
            "settings": {
                "PLUNGE_DEVIATION_THRESHOLD": float(get_watchlist_setting("PLUNGE_DEVIATION_THRESHOLD", "-15.0")),
                "PLUNGE_RSI_THRESHOLD": float(get_watchlist_setting("PLUNGE_RSI_THRESHOLD", "30.0")),
                "PLUNGE_VOL_RATIO_THRESHOLD": float(get_watchlist_setting("PLUNGE_VOL_RATIO_THRESHOLD", "1.4")),
                "PLUNGE_MIN_VAL_KRW": float(get_watchlist_setting("PLUNGE_MIN_VAL_KRW", "1000000.0")),
                "PLUNGE_MAX_VAL_KRW": float(get_watchlist_setting("PLUNGE_MAX_VAL_KRW", "500000000.0")),
                "PLUNGE_INDEX_FILTER_ENABLED": get_watchlist_setting("PLUNGE_INDEX_FILTER_ENABLED", "1") == "1",
            }
        }
    except Exception as e:
        logger.error(f"[PlungeBounceRoute] Failed to get settings: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/strategy/plunge_bounce/settings")
def save_plunge_bounce_settings(payload: dict = Body(...)):
    """Saves the custom rule filters for the Plunge Bounce strategy to DB."""
    try:
        from src.db.repository import save_watchlist_setting
        
        # Validation and parsing
        deviation = payload.get("PLUNGE_DEVIATION_THRESHOLD")
        rsi = payload.get("PLUNGE_RSI_THRESHOLD")
        vol = payload.get("PLUNGE_VOL_RATIO_THRESHOLD")
        min_val = payload.get("PLUNGE_MIN_VAL_KRW")
        max_val = payload.get("PLUNGE_MAX_VAL_KRW")
        idx_enabled = payload.get("PLUNGE_INDEX_FILTER_ENABLED")

        if deviation is not None:
            save_watchlist_setting("PLUNGE_DEVIATION_THRESHOLD", str(float(deviation)))
        if rsi is not None:
            save_watchlist_setting("PLUNGE_RSI_THRESHOLD", str(float(rsi)))
        if vol is not None:
            save_watchlist_setting("PLUNGE_VOL_RATIO_THRESHOLD", str(float(vol)))
        if min_val is not None:
            save_watchlist_setting("PLUNGE_MIN_VAL_KRW", str(float(min_val)))
        if max_val is not None:
            save_watchlist_setting("PLUNGE_MAX_VAL_KRW", str(float(max_val)))
        if idx_enabled is not None:
            save_watchlist_setting("PLUNGE_INDEX_FILTER_ENABLED", "1" if bool(idx_enabled) else "0")

        return {"ok": True, "message": "성공적으로 설정이 저장되었습니다."}
    except Exception as e:
        logger.error(f"[PlungeBounceRoute] Failed to save settings: {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 전략 일반화: 스케쥴 등록/제어, 전용 유니버스, 전용 포지션
#   (plunge_bounce_strategy / heikin_ashi_scalping_strategy 등 strategy_id 기준)
# ---------------------------------------------------------------------------
_KNOWN_STRATEGY_IDS = {
    "ai_stock_default_v1",
    "plunge_bounce_strategy",
    "heikin_ashi_scalping_strategy",
}


def _validate_strategy_id(strategy_id: str) -> str:
    sid = (strategy_id or "").strip()
    if sid not in _KNOWN_STRATEGY_IDS:
        raise HTTPException(status_code=404, detail=f"Unknown strategy_id: {strategy_id}")
    return sid


@router.get("/api/strategy/{strategy_id}/schedule")
def get_strategy_schedule(strategy_id: str):
    sid = _validate_strategy_id(strategy_id)
    from src.db.repository import load_strategy_schedule
    return {"ok": True, "schedule": load_strategy_schedule(sid)}


@router.post("/api/strategy/{strategy_id}/schedule")
def save_strategy_schedule_route(strategy_id: str, payload: dict = Body(...)):
    sid = _validate_strategy_id(strategy_id)
    from src.db.repository import save_strategy_schedule

    allowed = {"enabled", "interval_minutes", "start_hm", "end_hm", "weekdays", "mode", "auto_approve"}
    fields = {k: v for k, v in payload.items() if k in allowed}
    if "mode" in fields and str(fields["mode"]).lower() not in {"execute", "analysis_only"}:
        raise HTTPException(status_code=400, detail="mode must be 'execute' or 'analysis_only'")
    if "interval_minutes" in fields:
        try:
            fields["interval_minutes"] = max(1, int(fields["interval_minutes"]))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="interval_minutes must be an integer")
    schedule = save_strategy_schedule(sid, **fields)
    return {"ok": True, "schedule": schedule}


@router.get("/api/strategy/{strategy_id}/universe")
def get_strategy_universe(strategy_id: str):
    sid = _validate_strategy_id(strategy_id)
    from src.db.repository import load_strategy_universe
    from src.strategy.seven_split import STOCK_NAMES
    from src.market_metadata import resolve_stock_name

    universe = load_strategy_universe(sid)
    for item in universe:
        if not item.get("name"):
            item["name"] = resolve_stock_name(item["symbol"], STOCK_NAMES.get(item["symbol"], item["symbol"]))
    return {"ok": True, "universe": universe, "count": len(universe)}


@router.post("/api/strategy/{strategy_id}/universe")
def add_strategy_universe(strategy_id: str, payload: dict = Body(...)):
    sid = _validate_strategy_id(strategy_id)
    from src.db.repository import add_strategy_universe_symbol
    from src.strategy.seven_split import STOCK_NAMES
    from src.market_metadata import resolve_stock_name

    symbol = str(payload.get("symbol", "")).strip()
    if not symbol.isdigit() or len(symbol) != 6:
        raise HTTPException(status_code=400, detail="유효하지 않은 종목코드입니다. (6자리 숫자)")
    name = resolve_stock_name(symbol, str(payload.get("name") or STOCK_NAMES.get(symbol, symbol)))
    add_strategy_universe_symbol(sid, symbol, name)
    return {"ok": True, "symbol": symbol, "name": name}


@router.delete("/api/strategy/{strategy_id}/universe/{symbol}")
def delete_strategy_universe(strategy_id: str, symbol: str):
    sid = _validate_strategy_id(strategy_id)
    from src.db.repository import remove_strategy_universe_symbol

    deleted = remove_strategy_universe_symbol(sid, symbol.strip())
    if deleted <= 0:
        raise HTTPException(status_code=404, detail="전용 유니버스에 없는 종목입니다.")
    return {"ok": True}


@router.get("/api/strategy/{strategy_id}/positions")
def get_strategy_positions(strategy_id: str):
    sid = _validate_strategy_id(strategy_id)
    from src.db.repository import reconstruct_strategy_positions

    positions = reconstruct_strategy_positions(sid, env=trader.runtime_flags().trading_env)
    # 현재가를 붙여 평가손익 계산(실패해도 보유 정보는 반환)
    try:
        from src.trader import KIStockAPI
        api = KIStockAPI(notify_errors=False)
        for p in positions:
            try:
                q = api.get_quote(p["symbol"])
                cur = int(q.get("current") or 0)
                p["current_price"] = cur
                p["eval_pnl"] = int((cur - p["avg_cost"]) * p["qty"]) if p["qty"] else 0
                p["return_rate"] = round(((cur / p["avg_cost"]) - 1) * 100, 2) if p["avg_cost"] else 0.0
            except Exception:
                p.setdefault("current_price", 0)
    except Exception:
        pass
    return {"ok": True, "positions": positions, "count": len(positions)}


@router.get("/api/strategy/heikin_ashi_scalping/performance")
def get_heikin_ashi_performance():
    try:
        return _strategy_performance("heikin_ashi_scalping_strategy")
    except Exception as e:
        logger.error(f"[HeikinAshiRoute] Performance calculation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/strategy/heikin_ashi_scalping/scan")
def run_heikin_ashi_scan():
    try:
        from src.db.repository import get_watchlist_setting

        min_score = float(get_watchlist_setting("HEIKIN_MIN_SCORE", "3.5"))
        return _strategy_scan("heikin_ashi_scalping_strategy", min_score=min_score)
    except Exception as e:
        logger.error(f"[HeikinAshiRoute] Scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/strategy/heikin_ashi_scalping/run-trader")
def run_heikin_ashi_trader(payload: dict = Body(...)):
    global _scheduler_run_state
    mode = str(payload.get("mode", "execute")).lower()
    include_ai_rebalance = False
    auto_approve = bool(payload.get("auto_approve", True))

    with _scheduler_running_lock:
        if _scheduler_run_state["is_running"]:
            raise HTTPException(status_code=409, detail="스케줄러가 이미 실행 중입니다.")

        _scheduler_run_state["is_running"] = True
        _scheduler_run_state["mode"] = mode
        _scheduler_run_state["started_at"] = trader.datetime.now(trader.KST).isoformat()
        _scheduler_run_state["completed_at"] = None
        _scheduler_run_state["result"] = None
        _scheduler_run_state["error"] = None

    t = threading.Thread(
        target=_bg_run_scheduled_cycle,
        args=(mode, include_ai_rebalance, auto_approve, "heikin_ashi_scalping_strategy"),
        daemon=True,
    )
    t.start()
    return {"status": "started", "mode": mode}


@router.get("/api/strategy/heikin_ashi_scalping/scans-history")
def get_heikin_ashi_scans_history(limit: int = 100):
    try:
        return _strategy_scans_history("heikin_ashi_scalping_strategy", limit=limit)
    except Exception as e:
        logger.error(f"[HeikinAshiRoute] Failed to fetch scan history: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/strategy/heikin_ashi_scalping/schedule-history")
def get_heikin_ashi_schedule_history(limit: int = 50):
    try:
        return _strategy_schedule_history("heikin_ashi_scalping_strategy", limit=limit)
    except Exception as e:
        logger.error(f"[HeikinAshiRoute] Failed to fetch schedule history: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/strategy/heikin_ashi_scalping/settings")
def get_heikin_ashi_settings():
    try:
        from src.db.repository import get_watchlist_setting
        return {
            "ok": True,
            "settings": {
                "HEIKIN_FAST_EMA": int(float(get_watchlist_setting("HEIKIN_FAST_EMA", "10"))),
                "HEIKIN_SLOW_EMA": int(float(get_watchlist_setting("HEIKIN_SLOW_EMA", "20"))),
                "HEIKIN_RSI_PERIOD": int(float(get_watchlist_setting("HEIKIN_RSI_PERIOD", "14"))),
                "HEIKIN_MIN_SCORE": float(get_watchlist_setting("HEIKIN_MIN_SCORE", "3.5")),
                "HEIKIN_VOLUME_RATIO": float(get_watchlist_setting("HEIKIN_VOLUME_RATIO", "1.2")),
            },
        }
    except Exception as e:
        logger.error(f"[HeikinAshiRoute] Failed to get settings: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/strategy/heikin_ashi_scalping/settings")
def save_heikin_ashi_settings(payload: dict = Body(...)):
    try:
        from src.db.repository import save_watchlist_setting

        numeric_keys = {
            "HEIKIN_FAST_EMA": int,
            "HEIKIN_SLOW_EMA": int,
            "HEIKIN_RSI_PERIOD": int,
            "HEIKIN_MIN_SCORE": float,
            "HEIKIN_VOLUME_RATIO": float,
        }
        for key, caster in numeric_keys.items():
            if key in payload and payload[key] is not None:
                value = caster(float(payload[key])) if caster is int else caster(payload[key])
                if value <= 0:
                    raise ValueError(f"{key} must be positive")
                save_watchlist_setting(key, str(value))

        return {"ok": True, "message": "알파 하이킨아시 설정이 저장되었습니다."}
    except Exception as e:
        logger.error(f"[HeikinAshiRoute] Failed to save settings: {e}")
        return {"ok": False, "error": str(e)}
