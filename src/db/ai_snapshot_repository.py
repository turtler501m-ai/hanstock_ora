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

def save_performance(candidate_id: int, data: dict[str, Any]) -> None:
    row = dict(data)
    row["candidate_id"] = int(candidate_id)
    row["updated_at"] = _now()
    if "rule_only_result" in row:
        row["rule_only_result"] = dumps_json(row.get("rule_only_result"))
    cols = [
        "candidate_id", "market", "base_price", "base_date", "price_1d", "return_1d",
        "price_5d", "return_5d", "price_20d", "return_20d", "mfe", "mae",
        "benchmark_return", "rule_only_result", "actually_entered", "trade_id",
        "evaluation_complete", "updated_at",
    ]
    with _connect() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO ai_stock_performance ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})",
            tuple(row.get(c) for c in cols),
        )
        conn.commit()

def list_performance(market: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    with _connect() as conn:
        if market and str(market).upper() != "ALL":
            rows = conn.execute(
                "SELECT * FROM ai_stock_performance WHERE market=? ORDER BY updated_at DESC LIMIT ?",
                (require_storable_market(market), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ai_stock_performance ORDER BY updated_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["rule_only_result"] = loads_json(d.get("rule_only_result"), {})
            out.append(d)
        return out

def save_timing_signal(signal: dict[str, Any]) -> int:
    row = dict(signal)
    row["market"] = require_storable_market(row.get("market"))
    row.setdefault("created_at", _now())
    cols = [
        "strategy_id", "market", "candidate_id", "symbol", "instrument_type",
        "signal_type", "trigger", "ref_price", "signal_price", "ai_timing_confidence",
        "decision", "blocked_reason", "automation_level", "data_as_of", "created_at",
    ]
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO ai_stock_timing_signals ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})",
            tuple(row.get(c) for c in cols),
        )
        conn.commit()
        return int(cur.lastrowid)

def list_timing_signals(market: str | None = None, candidate_id: int | None = None,
                        limit: int = 100) -> list[dict[str, Any]]:
    where, params = [], []
    if market and str(market).upper() != "ALL":
        where.append("market=?")
        params.append(require_storable_market(market))
    if candidate_id is not None:
        where.append("candidate_id=?")
        params.append(int(candidate_id))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_stock_timing_signals {clause} ORDER BY id DESC LIMIT ?", tuple(params)
        ).fetchall()
        return [dict(r) for r in rows]

def _snapshot_payload(payload: Any) -> tuple[str, str]:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def create_market_snapshot(data: dict[str, Any]) -> int:
    """Persist an immutable market input snapshot.

    Reusing a snapshot key with byte-equivalent canonical content is
    idempotent. Reusing it for different content is rejected.
    """
    row = dict(data)
    row["market"] = require_storable_market(row.get("market"))
    for required in ("snapshot_key", "source", "data_as_of", "payload"):
        if row.get(required) in (None, ""):
            raise ValueError(f"{required} is required")
    payload, payload_hash = _snapshot_payload(row["payload"])
    with _connect() as conn:
        _begin_write(conn)
        existing = conn.execute(
            "SELECT * FROM ai_market_snapshots WHERE snapshot_key=?",
            (str(row["snapshot_key"]),),
        ).fetchone()
        if existing:
            same_identity = (
                str(existing["market"]) == row["market"]
                and str(existing["source"]) == str(row["source"])
                and str(existing["data_as_of"]) == str(row["data_as_of"])
                and (existing["regime"] or None) == (row.get("regime") or None)
                and str(existing["payload_hash"]) == payload_hash
            )
            if not same_identity:
                conn.rollback()
                raise ValueError("snapshot_key already identifies different market data")
            conn.commit()
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO ai_market_snapshots
            (snapshot_key, market, source, data_as_of, regime, payload,
             payload_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(row["snapshot_key"]), row["market"], str(row["source"]),
                str(row["data_as_of"]), row.get("regime"), payload, payload_hash,
                str(row.get("created_at") or _now()),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

def get_market_snapshot(snapshot_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_market_snapshots WHERE id=?", (int(snapshot_id),)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = loads_json(result.get("payload"), {})
        return result

def list_market_snapshots(*, market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), 1000))
    with _connect() as conn:
        if market:
            rows = conn.execute(
                """
                SELECT * FROM ai_market_snapshots
                WHERE market=? ORDER BY id DESC LIMIT ?
                """,
                (require_storable_market(market), bounded_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ai_market_snapshots ORDER BY id DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = loads_json(item.get("payload"), {})
            result.append(item)
        return result

def create_portfolio_snapshot(data: dict[str, Any]) -> int:
    """Persist an immutable account/portfolio input snapshot."""
    row = dict(data)
    row["market"] = require_storable_market(row.get("market"))
    row["account_id"] = str(row.get("account_id") or "").strip()
    for required in ("snapshot_key", "account_id", "source", "data_as_of", "payload"):
        if row.get(required) in (None, ""):
            raise ValueError(f"{required} is required")
    numbers: dict[str, float] = {}
    for field in ("cash", "total_eval", "stock_eval"):
        value = float(row.get(field, 0))
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{field} must be finite and non-negative")
        numbers[field] = value
    payload, payload_hash = _snapshot_payload(row["payload"])
    with _connect() as conn:
        _begin_write(conn)
        existing = conn.execute(
            "SELECT * FROM ai_portfolio_snapshots WHERE snapshot_key=?",
            (str(row["snapshot_key"]),),
        ).fetchone()
        if existing:
            same_identity = (
                str(existing["account_id"]) == row["account_id"]
                and str(existing["market"]) == row["market"]
                and str(existing["source"]) == str(row["source"])
                and str(existing["data_as_of"]) == str(row["data_as_of"])
                and float(existing["cash"]) == numbers["cash"]
                and float(existing["total_eval"]) == numbers["total_eval"]
                and float(existing["stock_eval"]) == numbers["stock_eval"]
                and str(existing["payload_hash"]) == payload_hash
            )
            if not same_identity:
                conn.rollback()
                raise ValueError("snapshot_key already identifies different portfolio data")
            conn.commit()
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO ai_portfolio_snapshots
            (snapshot_key, account_id, market, source, data_as_of, cash,
             total_eval, stock_eval, payload, payload_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(row["snapshot_key"]), row["account_id"], row["market"],
                str(row["source"]), str(row["data_as_of"]), numbers["cash"],
                numbers["total_eval"], numbers["stock_eval"], payload, payload_hash,
                str(row.get("created_at") or _now()),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

def get_portfolio_snapshot(snapshot_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_portfolio_snapshots WHERE id=?", (int(snapshot_id),)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = loads_json(result.get("payload"), {})
        return result

def list_portfolio_snapshots(
    *,
    account_id: str | None = None,
    market: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if account_id is not None:
        where.append("account_id=?")
        params.append(str(account_id))
    if market:
        where.append("market=?")
        params.append(require_storable_market(market))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(int(limit), 1000)))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_portfolio_snapshots{clause} ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = loads_json(item.get("payload"), {})
            result.append(item)
        return result

def get_or_create_daily_equity_baseline(
    *,
    account_id: str,
    market: str,
    trading_date: str,
    baseline_equity: float,
    snapshot_id: str,
    data_as_of: str,
) -> tuple[dict[str, Any], bool]:
    """Atomically persist the day's first trusted equity; it is never updated."""
    market = require_storable_market(market)
    equity = float(baseline_equity)
    if not account_id or not trading_date or not snapshot_id or equity <= 0:
        raise ValueError("complete positive equity baseline is required")
    with _connect() as conn:
        _begin_write(conn)
        existing = conn.execute(
            """
            SELECT * FROM ai_daily_equity_baselines
            WHERE account_id=? AND market=? AND trading_date=?
            """,
            (account_id, market, trading_date),
        ).fetchone()
        if existing:
            conn.commit()
            return dict(existing), False
        cur = conn.execute(
            """
            INSERT INTO ai_daily_equity_baselines
            (account_id, market, trading_date, baseline_equity, snapshot_id,
             data_as_of, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, market, trading_date, equity, snapshot_id,
                data_as_of, _now(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM ai_daily_equity_baselines WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        conn.commit()
        return dict(row), True

def daily_cashflow_reconciliation(
    *, account_id: str, market: str, trading_date: str
) -> dict[str, Any]:
    """Return reconciled net external cashflow and any unresolved ledger rows."""
    market = require_storable_market(market)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN reconciled=1 THEN amount ELSE 0 END), 0)
                AS reconciled_amount,
              COALESCE(SUM(CASE WHEN reconciled=0 THEN 1 ELSE 0 END), 0)
                AS unresolved_count
            FROM ai_daily_account_cashflows
            WHERE account_id=? AND market=? AND trading_date=?
            """,
            (account_id, market, trading_date),
        ).fetchone()
        return dict(row)

def record_daily_account_cashflow(
    *,
    account_id: str,
    market: str,
    trading_date: str,
    external_ref: str,
    amount: float,
    kind: str,
    occurred_at: str,
    reconciled: bool = False,
) -> int:
    """Idempotently record a broker-observed deposit/withdrawal for review."""
    market = require_storable_market(market)
    if not account_id or not trading_date or not external_ref or not kind:
        raise ValueError("complete cashflow identity is required")
    value = float(amount)
    if not math.isfinite(value) or value == 0:
        raise ValueError("cashflow amount must be finite and non-zero")
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO ai_daily_account_cashflows
            (account_id, market, trading_date, external_ref, amount, kind,
             reconciled, occurred_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, market, trading_date, external_ref, value, kind,
                int(bool(reconciled)), occurred_at, _now(),
            ),
        )
        row = conn.execute(
            """
            SELECT id FROM ai_daily_account_cashflows
            WHERE account_id=? AND market=? AND external_ref=?
            """,
            (account_id, market, external_ref),
        ).fetchone()
        conn.commit()
        return int(row["id"])

def mark_daily_account_cashflow_reconciled(cashflow_id: int) -> dict[str, Any]:
    """Mark an observed cashflow reconciled without altering its signed amount."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE ai_daily_account_cashflows SET reconciled=1 WHERE id=?",
            (int(cashflow_id),),
        )
        if cur.rowcount != 1:
            raise ValueError("cashflow not found")
        row = conn.execute(
            "SELECT * FROM ai_daily_account_cashflows WHERE id=?",
            (int(cashflow_id),),
        ).fetchone()
        conn.commit()
        return dict(row)

__all__ = [
    name for name, value in globals().items()
    if not name.startswith("_") and callable(value) and getattr(value, "__module__", None) == __name__
]
