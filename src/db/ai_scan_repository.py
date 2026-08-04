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

class ScanConflict(RuntimeError):
    """동일 (market, strategy_id) 활성 스캔 중복."""

def _scan_stale_min() -> int:
    try:
        return max(1, int(os.environ.get("AI_STOCK_SCAN_STALE_MIN", "30")))
    except ValueError:
        return 30

def get_active_scan(market: str, strategy_id: str) -> dict[str, Any] | None:
    market = require_storable_market(market)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_stock_scans WHERE market=? AND strategy_id=? "
            "AND status IN (?, ?) ORDER BY id DESC LIMIT 1",
            (market, strategy_id, SCAN_QUEUED, SCAN_RUNNING),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        # stale-running TTL 정리 (§6.1)
        from src.ai_stock.freshness import age_minutes

        age = age_minutes(data.get("started_at"))
        if age is not None and age > _scan_stale_min():
            conn.execute(
                "UPDATE ai_stock_scans SET status='failed', error_message=?, completed_at=? WHERE id=?",
                ("stale-running auto-cleanup", _now(), data["id"]),
            )
            conn.commit()
            return None
        return data

def create_scan(
    *,
    market: str,
    strategy_id: str,
    strategy_version: int | None = None,
    model: str | None = None,
    feature_version: str | None = None,
    prompt_version: str | None = None,
    profile_hash: str | None = None,
    data_as_of: str | None = None,
) -> int:
    """중복 활성 스캔이 있으면 ScanConflict (§5.3·§6.1)."""
    market = require_storable_market(market)
    now = _now()
    with _connect() as conn:
        _begin_write(conn)
        active_rows = conn.execute(
            "SELECT id, started_at FROM ai_stock_scans WHERE market=? AND strategy_id=? "
            "AND status IN (?, ?)",
            (market, strategy_id, SCAN_QUEUED, SCAN_RUNNING),
        ).fetchall()
        stale_cutoff = datetime.now(KST) - timedelta(minutes=_scan_stale_min())
        for row in active_rows:
            started_at = None
            try:
                started_at = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
            except Exception:
                pass
            if started_at and started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=KST)
            if started_at is not None and started_at < stale_cutoff:
                conn.execute(
                    "UPDATE ai_stock_scans SET status='failed', error_message=?, completed_at=? WHERE id=?",
                    ("stale-running auto-cleanup", now, row["id"]),
                )
        active = conn.execute(
            "SELECT id FROM ai_stock_scans WHERE market=? AND strategy_id=? "
            "AND status IN (?, ?) ORDER BY id DESC LIMIT 1",
            (market, strategy_id, SCAN_QUEUED, SCAN_RUNNING),
        ).fetchone()
        if active is not None:
            conn.execute("ROLLBACK")
            raise ScanConflict(f"active scan exists for ({market}, {strategy_id})")
        cur = conn.execute(
            "INSERT INTO ai_stock_scans (market, strategy_id, strategy_version, model, "
            "feature_version, prompt_version, profile_hash, status, started_at, data_as_of) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (market, strategy_id, strategy_version, model, feature_version,
             prompt_version, profile_hash, SCAN_RUNNING, now, data_as_of),
        )
        conn.commit()
        return int(cur.lastrowid)

def finish_scan(scan_id: int, *, status: str, candidate_count: int = 0,
                fallback_count: int = 0, error_message: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE ai_stock_scans SET status=?, completed_at=?, candidate_count=?, "
            "fallback_count=?, error_message=? WHERE id=?",
            (status, _now(), candidate_count, fallback_count, error_message, scan_id),
        )
        conn.commit()

def get_scan(scan_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM ai_stock_scans WHERE id=?", (scan_id,)).fetchone()
        return dict(row) if row else None

def list_scans(market: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 20), 200))
    with _connect() as conn:
        if market and str(market).upper() != "ALL":
            rows = conn.execute(
                "SELECT * FROM ai_stock_scans WHERE market=? ORDER BY id DESC LIMIT ?",
                (require_storable_market(market), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ai_stock_scans ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

def save_candidate(candidate: dict[str, Any]) -> int:
    market = require_storable_market(candidate.get("market"))
    row = dict(candidate)
    row["market"] = market
    row.setdefault("created_at", _now())
    for f in _CAND_JSON_FIELDS:
        row[f] = dumps_json(row.get(f) or [])
    row["fallback_used"] = 1 if row.get("fallback_used") else 0
    cols = [
        "scan_id", "market", "symbol", "name", "instrument_type", "currency",
        "current_price", "change_pct", "strategy_id", "strategy_version", "model",
        "feature_version", "prompt_version", "profile_hash", "market_regime",
        "rule_score", "technical_score", "momentum_score", "narrative_score",
        "ai_score", "risk_score", "final_score", "confidence", "decision",
        "positive_factors", "negative_factors", "related_narratives", "warnings",
        "invalidation_conditions", "data_quality", "fallback_used", "fallback_reason",
        "data_as_of", "created_at",
    ]
    placeholders = ", ".join(["?"] * len(cols))
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT OR REPLACE INTO ai_stock_candidates ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(row.get(c) for c in cols),
        )
        conn.commit()
        return int(cur.lastrowid)

def _candidate_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["scan_id"] = d.pop("id", None) if False else d.get("scan_id")
    d["candidate_id"] = row["id"]
    for f in _CAND_JSON_FIELDS:
        d[f] = loads_json(d.get(f), [])
    d["fallback_used"] = bool(d.get("fallback_used"))
    return d

def list_candidates(
    *, market: str | None = None, scan_id: int | None = None,
    decision: str | None = None, min_score: float | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    where, params = [], []
    if market and str(market).upper() != "ALL":
        where.append("market=?")
        params.append(require_storable_market(market))
    if scan_id is not None:
        where.append("scan_id=?")
        params.append(int(scan_id))
    if decision:
        where.append("decision=?")
        params.append(str(decision))
    if min_score is not None:
        where.append("final_score >= ?")
        params.append(float(min_score))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_stock_candidates {clause} ORDER BY final_score DESC, symbol LIMIT ?",
            tuple(params),
        ).fetchall()
        return [_candidate_to_dict(r) for r in rows]

def get_candidate(candidate_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_stock_candidates WHERE id=?", (int(candidate_id),)
        ).fetchone()
        return _candidate_to_dict(row) if row else None

__all__ = [
    name for name, value in globals().items()
    if not name.startswith("_") and callable(value) and getattr(value, "__module__", None) == __name__
]
