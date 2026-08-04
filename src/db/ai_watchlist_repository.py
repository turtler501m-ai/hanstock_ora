# -*- coding: utf-8 -*-
"""Bounded AI stock persistence implementation."""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta
from typing import Any

from src.ai_stock.constants import SCAN_ACTIVE, SCAN_QUEUED, SCAN_RUNNING
from src.ai_stock.markets import require_storable_market
from src.ai_stock.schemas import dumps_json, loads_json
from src.db.ai_stock_support import (
    KST,
    begin_write as _begin_write,
    connect_ai_stock as _connect,
    now_kst as _now,
)

_CAND_JSON_FIELDS = (
    "positive_factors", "negative_factors", "related_narratives",
    "warnings", "invalidation_conditions",
)

_WATCH_JSON_FIELDS = ("related_narratives", "confirmation_conditions", "invalidation_conditions")

_POSITION_JSON_FIELDS = {
    "invalidation_conditions", "target_plan", "trailing_stop",
}

_DECISION_JSON_FIELDS = {
    "invalidation_conditions", "intent_payload", "risk_decision", "token_usage",
}

def upsert_watch(candidate_id: int, data: dict[str, Any]) -> None:
    row = dict(data)
    row["candidate_id"] = int(candidate_id)
    row["market"] = require_storable_market(row.get("market"))
    row.setdefault("created_at", _now())
    row["updated_at"] = _now()
    for f in _WATCH_JSON_FIELDS:
        row[f] = dumps_json(row.get(f) or [])
    cols = [
        "candidate_id", "market", "symbol", "status", "initial_score", "current_score",
        "initial_price", "current_price", "related_narratives", "market_regime",
        "confirmation_conditions", "invalidation_conditions", "expires_at",
        "rejection_reason", "created_at", "updated_at",
    ]
    placeholders = ", ".join(["?"] * len(cols))
    with _connect() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO ai_stock_watchlist ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(row.get(c) for c in cols),
        )
        conn.commit()

def get_watch(candidate_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_stock_watchlist WHERE candidate_id=?", (int(candidate_id),)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for f in _WATCH_JSON_FIELDS:
            d[f] = loads_json(d.get(f), [])
        return d

def list_watchlist(market: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    where, params = [], []
    if market and str(market).upper() != "ALL":
        where.append("market=?")
        params.append(require_storable_market(market))
    if status:
        where.append("status=?")
        params.append(str(status))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_stock_watchlist {clause} ORDER BY updated_at DESC", tuple(params)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for f in _WATCH_JSON_FIELDS:
                d[f] = loads_json(d.get(f), [])
            out.append(d)
        return out

def update_watch_status(candidate_id: int, to_status: str, *, reason: str | None = None) -> None:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT status FROM ai_stock_watchlist WHERE candidate_id=?", (int(candidate_id),)
        ).fetchone()
        from_status = cur["status"] if cur else None
        conn.execute(
            "UPDATE ai_stock_watchlist SET status=?, rejection_reason=COALESCE(?, rejection_reason), updated_at=? WHERE candidate_id=?",
            (to_status, reason, _now(), int(candidate_id)),
        )
        conn.execute(
            "INSERT INTO ai_stock_watch_events (candidate_id, ts, from_status, to_status, reason) VALUES (?, ?, ?, ?, ?)",
            (int(candidate_id), _now(), from_status, to_status, reason),
        )
        conn.commit()

def remove_watch(candidate_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM ai_stock_watchlist WHERE candidate_id=?", (int(candidate_id),))
        conn.commit()

def list_watch_events(candidate_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM ai_stock_watch_events WHERE candidate_id=? ORDER BY id DESC",
            (int(candidate_id),),
        ).fetchall()
        return [dict(r) for r in rows]

def get_policy(strategy_id: str, market: str) -> dict[str, Any] | None:
    market = require_storable_market(market)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_stock_automation_policies WHERE strategy_id=? AND market=?",
            (strategy_id, market),
        ).fetchone()
        return dict(row) if row else None

def upsert_policy(strategy_id: str, market: str, fields: dict[str, Any]) -> dict[str, Any]:
    market = require_storable_market(market)
    existing = get_policy(strategy_id, market)
    now = _now()
    allowed = {
        "enabled", "automation_level", "auto_approve", "auto_execute", "max_daily_orders",
        "max_daily_loss_pct", "max_risk_per_trade_pct", "max_position_pct",
        "max_market_exposure_pct", "min_final_score", "min_rule_score", "max_risk_score",
        "allow_fallback_trade", "allow_stale_data_trade",
        "min_market_cap", "min_avg_trading_value", "min_price", "include_etf",
        "exclude_small_cap", "universe_source", "excluded_types",
        "briefing_freshness_min", "timing_min_confidence", "realtime_poll_seconds",
    }
    data = {k: v for k, v in (fields or {}).items() if k in allowed}
    with _connect() as conn:
        if existing:
            sets = ", ".join(f"{k}=?" for k in data) + (", " if data else "") + "updated_at=?"
            conn.execute(
                f"UPDATE ai_stock_automation_policies SET {sets} WHERE strategy_id=? AND market=?",
                (*data.values(), now, strategy_id, market),
            )
        else:
            cols = ["strategy_id", "market", *data.keys(), "created_at", "updated_at"]
            vals = [strategy_id, market, *data.values(), now, now]
            conn.execute(
                f"INSERT INTO ai_stock_automation_policies ({', '.join(cols)}) "
                f"VALUES ({', '.join(['?'] * len(cols))})",
                tuple(vals),
            )
        conn.commit()
    return get_policy(strategy_id, market)

def list_policies(market: str | None = None) -> list[dict[str, Any]]:
    with _connect() as conn:
        if market and str(market).upper() != "ALL":
            rows = conn.execute(
                "SELECT * FROM ai_stock_automation_policies WHERE market=? ORDER BY strategy_id",
                (require_storable_market(market),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ai_stock_automation_policies ORDER BY market, strategy_id"
            ).fetchall()
        return [dict(r) for r in rows]

__all__ = [
    name for name, value in globals().items()
    if not name.startswith("_") and callable(value) and getattr(value, "__module__", None) == __name__
]
