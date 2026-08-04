# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from src import trader
from src.dashboard.core import WEB_DIR
from src.db.repository import init_db, save_scanned_candidate
from src.db.scheduler_repository import save_scheduler_result
from src.strategy.narrative_collector import collect_narrative_history
from src.strategy.narrative_momentum import (
    STRATEGY_ID,
    NarrativeMomentumSettings,
    NarrativeMomentumStrategy,
    load_json_file,
    save_json_file,
)
from src.strategy import narrative_momentum_runner as runner
from src.utils.logger import logger

router = APIRouter(tags=["narrative-momentum"])

BASE_DIR = Path(__file__).resolve().parents[3]
THEME_MAP_PATH = BASE_DIR / "config" / "theme_map.json"
NARRATIVE_HISTORY_PATH = BASE_DIR / ".runtime" / "narrative_history.json"
LATEST_RESULT_PATH = BASE_DIR / ".runtime" / "narrative_momentum_latest.json"


@router.get("/narrative-momentum", response_class=FileResponse)
def read_narrative_momentum_dashboard():
    return FileResponse(WEB_DIR / "templates" / "narrative_momentum.html")


@router.get("/api/narrative-momentum/status")
def get_narrative_momentum_status():
    history, theme_map, errors = _load_inputs()
    strategy = _strategy()
    status = strategy.status(history, theme_map)
    signals = strategy.calculate_signals(history, theme_map)
    unmatched = strategy.unmatched_narratives(history, theme_map)
    settings = NarrativeMomentumSettings()
    status.update(
        {
            "ok": not errors,
            "errors": errors,
            "strategy_id": STRATEGY_ID,
            "candidate_count": len(signals),
            "unmatched_count": len(unmatched),
            "narrative_count": int(status.get("narrative_count") or 0),
            "shift_count": int(status.get("shift_count") or 0),
            "theme_count": len(theme_map),
            "approval_score_min": float(status.get("approval_score_min") or settings.approval_score_min),
            "latest_result_path": _display_path(LATEST_RESULT_PATH),
            "history_path": _display_path(NARRATIVE_HISTORY_PATH),
            "theme_map_path": _display_path(THEME_MAP_PATH),
            "safety": {
                "dry_run": bool(trader.runtime_flags().dry_run),
                "trading_env": trader.runtime_flags().trading_env,
                "enable_live_trading": bool(trader.runtime_flags().enable_live_trading),
                "require_approval": bool(trader.runtime_flags().require_approval),
                "online_access_blocked": bool(getattr(trader.config, "online_access_blocked", False)),
            },
        }
    )
    return status


@router.get("/api/narrative-momentum/latest")
def get_narrative_momentum_latest():
    history, theme_map, errors = _load_inputs()
    strategy = _strategy()
    status = strategy.status(history, theme_map)
    signals = strategy.calculate_signals(history, theme_map)
    unmatched = strategy.unmatched_narratives(history, theme_map)
    payload = {
        "ok": not errors,
        "errors": errors,
        "strategy_id": STRATEGY_ID,
        "status": status,
        "signals": signals,
        "unmatched": unmatched,
        "total_scanned": len(signals),
    }
    return payload


@router.post("/api/narrative-momentum/scan")
def scan_narrative_momentum(payload: dict | None = Body(default=None)):
    auto_collect = True if payload is None else bool(payload.get("auto_collect", True))
    result = runner.run_narrative_momentum_cycle(
        save_candidates=bool(payload and payload.get("save_candidates")),
        auto_collect=auto_collect,
        latest_path=LATEST_RESULT_PATH,
        history_path=NARRATIVE_HISTORY_PATH,
        theme_map_path=THEME_MAP_PATH,
    )
    if result.get("errors"):
        raise HTTPException(status_code=400, detail="; ".join(result["errors"]))
    return result


@router.post("/api/narrative-momentum/run-scheduled")
def run_narrative_momentum_scheduled(payload: dict | None = Body(default=None)):
    save_candidates = True if payload is None else bool(payload.get("save_candidates", True))
    auto_collect = True if payload is None else bool(payload.get("auto_collect", True))
    result = runner.run_narrative_momentum_cycle(
        save_candidates=save_candidates,
        auto_collect=auto_collect,
        latest_path=LATEST_RESULT_PATH,
        history_path=NARRATIVE_HISTORY_PATH,
        theme_map_path=THEME_MAP_PATH,
    )
    mode = "execute" if save_candidates else "analysis_only"
    save_scheduler_result(mode, trader.datetime.now(trader.KST).isoformat(), result)
    return result


@router.post("/api/narrative-momentum/collect")
def collect_narrative_momentum_history():
    result = collect_narrative_history(
        history_path=NARRATIVE_HISTORY_PATH,
        theme_map_path=THEME_MAP_PATH,
    )
    if not result.get("generated"):
        raise HTTPException(status_code=409, detail="; ".join(result.get("errors") or ["narrative collection did not generate history"]))
    return result


@router.get("/api/narrative-momentum/history")
def get_narrative_momentum_history(limit: int = 20):
    history = load_json_file(NARRATIVE_HISTORY_PATH, [])
    if not isinstance(history, list):
        raise HTTPException(status_code=400, detail="narrative_history must be a list")
    rows = sorted([row for row in history if isinstance(row, dict)], key=lambda item: str(item.get("date") or ""), reverse=True)
    return {"history": rows[: max(1, min(limit, 100))], "count": len(rows)}


@router.post("/api/narrative-momentum/history")
def save_narrative_momentum_history(payload: dict = Body(...)):
    history = payload.get("history")
    if not isinstance(history, list):
        raise HTTPException(status_code=400, detail="history must be a list")
    for entry in history:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail="each history entry must be an object")
        if not str(entry.get("date") or "").strip():
            raise HTTPException(status_code=400, detail="each history entry requires date")
        narratives = entry.get("dominant_narratives", [])
        if not isinstance(narratives, list):
            raise HTTPException(status_code=400, detail="dominant_narratives must be a list")
    save_json_file(NARRATIVE_HISTORY_PATH, history)
    return {"ok": True, "count": len(history), "path": _display_path(NARRATIVE_HISTORY_PATH)}


@router.get("/api/narrative-momentum/theme-map")
def get_narrative_theme_map():
    theme_map = load_json_file(THEME_MAP_PATH, {})
    if not isinstance(theme_map, dict):
        raise HTTPException(status_code=400, detail="theme_map must be an object")
    themes = []
    for theme, stocks in sorted(theme_map.items()):
        stock_rows = stocks if isinstance(stocks, list) else []
        themes.append(
            {
                "theme": theme,
                "stock_count": len(stock_rows),
                "stocks": stock_rows,
            }
        )
    return {"themes": themes, "count": len(themes)}


@router.post("/api/narrative-momentum/theme-map/reload")
def reload_narrative_theme_map():
    return get_narrative_theme_map()


@router.get("/api/narrative-momentum/schedule")
def get_narrative_momentum_schedule():
    from src.db.repository import load_strategy_schedule

    return {"ok": True, "schedule": load_strategy_schedule(STRATEGY_ID)}


@router.post("/api/narrative-momentum/schedule")
def save_narrative_momentum_schedule(payload: dict = Body(...)):
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
    schedule = save_strategy_schedule(STRATEGY_ID, **fields)
    return {"ok": True, "schedule": schedule}


@router.get("/api/narrative-momentum/schedule-history")
def get_narrative_momentum_schedule_history(limit: int = 20):
    init_db()
    rows = []
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        result_rows = conn.execute(
            """
            SELECT recorded_at, mode, result, strategy_id
            FROM scheduler_results
            WHERE strategy_id = ?
            ORDER BY recorded_at DESC
            LIMIT ?
            """,
            (STRATEGY_ID, max(1, min(int(limit or 20), 100))),
        ).fetchall()
    for row in result_rows:
        try:
            payload = json.loads(row["result"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        summary = payload.get("summary") if isinstance(payload, dict) else {}
        signals = payload.get("signals") if isinstance(payload, dict) and isinstance(payload.get("signals"), list) else []
        detail = {
            "status": payload.get("status", {}) if isinstance(payload, dict) else {},
            "collection": payload.get("collection") if isinstance(payload, dict) else None,
            "signals": signals[:50],
            "unmatched": payload.get("unmatched", []) if isinstance(payload, dict) else [],
            "total_scanned": payload.get("total_scanned", 0) if isinstance(payload, dict) else 0,
            "saved_count": payload.get("saved_count", 0) if isinstance(payload, dict) else 0,
            "ran_at": payload.get("ran_at") if isinstance(payload, dict) else None,
        }
        rows.append(
            {
                "recorded_at": row["recorded_at"],
                "mode": row["mode"],
                "strategy_id": row["strategy_id"],
                "summary": summary or runner.build_summary(payload if isinstance(payload, dict) else {}),
                "detail": detail,
                "ok": bool(payload.get("ok", True)) if isinstance(payload, dict) else False,
                "errors": payload.get("errors", []) if isinstance(payload, dict) else [],
            }
        )
    return {"ok": True, "history": rows, "count": len(rows)}


@router.post("/api/narrative-momentum/queue-approval")
def queue_narrative_approval(payload: dict = Body(...)):
    ticker = str(payload.get("ticker") or payload.get("symbol") or "").strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")
    name = str(payload.get("name") or ticker)
    settings = NarrativeMomentumSettings()

    history, theme_map, errors = _load_inputs()
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    strategy = _strategy()
    status = strategy.status(history, theme_map)
    if status.get("state") != "fresh":
        raise HTTPException(status_code=409, detail="fresh narrative data is required")
    signals = strategy.calculate_signals(history, theme_map)
    signal = next((item for item in signals if str(item.get("ticker")) == ticker), None)
    if signal is None:
        raise HTTPException(status_code=404, detail="ticker is not in current narrative signals")
    server_score = _to_float(signal.get("final_score"))
    if server_score < settings.approval_score_min:
        raise HTTPException(status_code=400, detail=f"server score must be >= {settings.approval_score_min:g}")
    if _has_pending_buy(ticker):
        raise HTTPException(status_code=409, detail="pending buy approval already exists")

    qty = int(_to_float(payload.get("qty")))
    price = int(_to_float(payload.get("price")) or 0)
    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be greater than 0")
    if price <= 0:
        raise HTTPException(status_code=400, detail="price must be greater than 0")
    name = str(signal.get("name") or name)
    reason = f"내러티브 모멘텀 {server_score:g}점: {', '.join(signal.get('narratives', [])[:2])}"
    client_reason = str(payload.get("reason") or "").strip()
    if client_reason:
        reason = f"{reason} / 요청메모: {client_reason[:120]}"
    approval_id = _create_approval_row(
        {
            "symbol": ticker,
            "name": name,
            "action": "buy",
            "qty": qty,
            "price": price,
            "reason": reason,
            "source": "narrative_momentum",
            "strategy_id": STRATEGY_ID,
        }
    )
    return {"ok": True, "id": approval_id, "status": "pending"}


def _strategy() -> NarrativeMomentumStrategy:
    return NarrativeMomentumStrategy()


def _load_inputs() -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[str]]:
    return runner.load_inputs(NARRATIVE_HISTORY_PATH, THEME_MAP_PATH)


def _save_candidates(signals: list[dict[str, Any]]) -> int:
    return runner.save_candidates_from_signals(signals)


def _create_approval_row(payload: dict[str, Any]) -> int:
    action = str(payload.get("action", "")).lower()
    if action not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="action must be buy or sell")
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    qty = int(_to_float(payload.get("qty")))
    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be greater than 0")
    price = int(_to_float(payload.get("price")))
    now = trader.datetime.now(trader.KST).strftime("%Y-%m-%d %H:%M:%S")
    init_db()
    with trader.connect_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO approvals
            (
                created_at, updated_at, symbol, name, action, qty, price, reason, source,
                status, response_msg, strategy_id, strategy_version, profile_hash, source_candidate_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?, ?, ?)
            """,
            (
                now,
                now,
                symbol,
                str(payload.get("name") or symbol),
                action,
                qty,
                price,
                str(payload.get("reason") or ""),
                str(payload.get("source") or "narrative_momentum"),
                str(payload.get("strategy_id") or STRATEGY_ID),
                None,
                None,
                None,
            ),
        )
        return int(cursor.lastrowid)


def _has_pending_buy(symbol: str) -> bool:
    init_db()
    try:
        with trader.connect_db() as conn:
            row = conn.execute(
                """
                SELECT id FROM approvals
                WHERE symbol = ? AND action = 'buy' AND status = 'pending'
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            return row is not None
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        logger.warning(f"Failed to check pending narrative approval: {exc}")
        return False


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(BASE_DIR))
    except ValueError:
        return str(path)
