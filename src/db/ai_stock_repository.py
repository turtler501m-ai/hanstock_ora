# -*- coding: utf-8 -*-
"""AI스톡 전용 저장소 (§6).

테이블은 기존 메인 DB(`connect_db`)에 두고, 생성은 `repository.init_db()`가
`init_ai_stock_tables(conn)`을 호출하는 방식으로 연결한다(§6.0).
순환 import를 피하려고 `connect_db`는 함수 내에서 지연 import한다.

market 저장값은 KR/US만 허용하고 ALL은 거부한다(§6.0).
"""
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


class ScanConflict(RuntimeError):
    """동일 (market, strategy_id) 활성 스캔 중복."""


def _scan_stale_min() -> int:
    try:
        return max(1, int(os.environ.get("AI_STOCK_SCAN_STALE_MIN", "30")))
    except ValueError:
        return 30


# --------------------------------------------------------------------------- #
# 테이블 생성 (§6.1~6.8)
# --------------------------------------------------------------------------- #
def init_ai_stock_tables(conn) -> None:
    """repository.init_db()의 conn 컨텍스트 안에서 호출된다."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_stock_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version INTEGER,
            model TEXT,
            feature_version TEXT,
            prompt_version TEXT,
            profile_hash TEXT,
            status TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            data_as_of TEXT,
            candidate_count INTEGER DEFAULT 0,
            fallback_count INTEGER DEFAULT 0,
            error_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_stock_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            instrument_type TEXT DEFAULT 'stock',
            currency TEXT,
            current_price REAL,
            change_pct REAL,
            strategy_id TEXT,
            strategy_version INTEGER,
            model TEXT,
            feature_version TEXT,
            prompt_version TEXT,
            profile_hash TEXT,
            market_regime TEXT,
            rule_score REAL,
            technical_score REAL,
            momentum_score REAL,
            narrative_score REAL,
            ai_score REAL,
            risk_score REAL,
            final_score REAL,
            confidence REAL,
            decision TEXT,
            positive_factors TEXT,
            negative_factors TEXT,
            related_narratives TEXT,
            warnings TEXT,
            invalidation_conditions TEXT,
            data_quality TEXT,
            fallback_used INTEGER DEFAULT 0,
            fallback_reason TEXT,
            data_as_of TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_cand_unique ON ai_stock_candidates(scan_id, market, symbol)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_stock_watchlist (
            candidate_id INTEGER PRIMARY KEY,
            market TEXT NOT NULL,
            symbol TEXT,
            status TEXT NOT NULL,
            initial_score REAL,
            current_score REAL,
            initial_price REAL,
            current_price REAL,
            related_narratives TEXT,
            market_regime TEXT,
            confirmation_conditions TEXT,
            invalidation_conditions TEXT,
            expires_at TEXT,
            rejection_reason TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_stock_watch_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_stock_performance (
            candidate_id INTEGER PRIMARY KEY,
            market TEXT,
            base_price REAL,
            base_date TEXT,
            price_1d REAL, return_1d REAL,
            price_5d REAL, return_5d REAL,
            price_20d REAL, return_20d REAL,
            mfe REAL, mae REAL,
            benchmark_return REAL,
            rule_only_result TEXT,
            actually_entered INTEGER DEFAULT 0,
            trade_id INTEGER,
            evaluation_complete INTEGER DEFAULT 0,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_stock_execution_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER,
            market TEXT NOT NULL,
            symbol TEXT,
            strategy_id TEXT,
            strategy_version INTEGER,
            action TEXT,
            entry_price REAL,
            stop_price REAL,
            take_profit REAL,
            risk_budget REAL,
            quantity INTEGER,
            estimated_cost REAL,
            safety_checks TEXT,
            status TEXT,
            approval_market TEXT,
            approval_db TEXT,
            approval_id INTEGER,
            approval_status TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_stock_automation_policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            market TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            automation_level INTEGER DEFAULT 4,
            auto_approve INTEGER DEFAULT 0,
            auto_execute INTEGER DEFAULT 0,
            max_daily_orders INTEGER DEFAULT 3,
            max_daily_loss_pct REAL DEFAULT 2.0,
            max_risk_per_trade_pct REAL DEFAULT 1.0,
            max_position_pct REAL DEFAULT 10.0,
            max_market_exposure_pct REAL DEFAULT 50.0,
            min_final_score REAL DEFAULT 65.0,
            min_rule_score REAL DEFAULT 40.0,
            max_risk_score REAL DEFAULT 60.0,
            allow_fallback_trade INTEGER DEFAULT 0,
            allow_stale_data_trade INTEGER DEFAULT 0,
            min_market_cap REAL,
            min_avg_trading_value REAL,
            min_price REAL,
            include_etf INTEGER DEFAULT 1,
            exclude_small_cap INTEGER DEFAULT 1,
            universe_source TEXT,
            excluded_types TEXT,
            briefing_freshness_min INTEGER DEFAULT 1440,
            timing_min_confidence REAL DEFAULT 0.6,
            realtime_poll_seconds INTEGER DEFAULT 5,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_policy_unique ON ai_stock_automation_policies(strategy_id, market)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_stock_execution_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT,
            market TEXT,
            scan_id INTEGER,
            candidate_id INTEGER,
            plan_id INTEGER,
            run_type TEXT,
            automation_level INTEGER,
            status TEXT,
            blocked_stage TEXT,
            blocked_reason TEXT,
            policy_snapshot TEXT,
            safety_checks TEXT,
            approval_market TEXT,
            approval_db TEXT,
            approval_id INTEGER,
            order_id INTEGER,
            broker_order_id TEXT,
            started_at TEXT,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_stock_timing_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT,
            market TEXT NOT NULL,
            candidate_id INTEGER NOT NULL,
            symbol TEXT,
            instrument_type TEXT,
            signal_type TEXT,
            trigger TEXT,
            ref_price REAL,
            signal_price REAL,
            ai_timing_confidence REAL,
            decision TEXT,
            blocked_reason TEXT,
            automation_level INTEGER,
            data_as_of TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_strategy_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market TEXT NOT NULL,
            account_id TEXT NOT NULL DEFAULT '',
            symbol TEXT NOT NULL,
            name TEXT,
            strategy_id TEXT NOT NULL,
            strategy_version INTEGER,
            profile_hash TEXT,
            status TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'long',
            opened_at TEXT,
            closed_at TEXT,
            entry_thesis TEXT,
            invalidation_conditions TEXT,
            entry_price REAL,
            average_price REAL,
            filled_qty INTEGER NOT NULL DEFAULT 0,
            remaining_qty INTEGER NOT NULL DEFAULT 0,
            initial_stop_price REAL,
            current_stop_price REAL,
            target_plan TEXT,
            trailing_stop TEXT,
            max_holding_until TEXT,
            initial_risk_amount REAL,
            current_risk_amount REAL,
            realized_pnl REAL NOT NULL DEFAULT 0,
            unrealized_pnl REAL NOT NULL DEFAULT 0,
            last_decision_id INTEGER,
            last_evaluated_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_strategy_position_active
        ON ai_strategy_positions(market, account_id, symbol, strategy_id)
        WHERE status IN ('pending_entry', 'open', 'exit_pending')
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_strategy_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_key TEXT NOT NULL UNIQUE,
            ts TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version INTEGER,
            profile_hash TEXT,
            model_provider TEXT,
            model_name TEXT,
            prompt_version TEXT,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            position_id INTEGER,
            market_snapshot_id TEXT,
            portfolio_snapshot_id TEXT,
            input_feature_hash TEXT,
            data_as_of TEXT,
            action TEXT NOT NULL,
            confidence REAL,
            thesis TEXT,
            invalidation_conditions TEXT,
            intent_payload TEXT NOT NULL,
            risk_decision TEXT,
            final_action TEXT,
            rejection_reason TEXT,
            order_id INTEGER,
            token_usage TEXT,
            latency_ms INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_managed_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_order_key TEXT NOT NULL UNIQUE,
            decision_id INTEGER NOT NULL,
            position_id INTEGER,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            action TEXT NOT NULL,
            order_type TEXT NOT NULL,
            requested_qty INTEGER NOT NULL,
            requested_price REAL,
            filled_qty INTEGER NOT NULL DEFAULT 0,
            average_fill_price REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            broker_order_id TEXT,
            approval_id INTEGER,
            expires_at TEXT,
            last_error TEXT,
            submitted_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_managed_order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            filled_qty INTEGER,
            fill_price REAL,
            broker_payload TEXT,
            reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_managed_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            fill_key TEXT NOT NULL,
            fill_qty INTEGER NOT NULL,
            fill_price REAL NOT NULL,
            broker_payload TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(order_id, fill_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_key TEXT NOT NULL UNIQUE,
            market TEXT NOT NULL,
            source TEXT NOT NULL,
            data_as_of TEXT NOT NULL,
            regime TEXT,
            payload TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_key TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL,
            market TEXT NOT NULL,
            source TEXT NOT NULL,
            data_as_of TEXT NOT NULL,
            cash REAL NOT NULL,
            total_eval REAL NOT NULL,
            stock_eval REAL NOT NULL,
            payload TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_risk_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            market TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            position_id INTEGER NOT NULL DEFAULT 0,
            order_id INTEGER NOT NULL DEFAULT 0,
            cash_amount REAL NOT NULL,
            risk_amount REAL NOT NULL,
            symbol TEXT,
            sector_key TEXT,
            exposure_amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            reason TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            released_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_risk_reservation_active_key
        ON ai_risk_reservations(
            account_id, market, strategy_id, position_id, order_id
        )
        WHERE status='active'
        """
    )
    for column, definition in (
        ("symbol", "TEXT"),
        ("sector_key", "TEXT"),
        ("exposure_amount", "REAL NOT NULL DEFAULT 0"),
    ):
        try:
            conn.execute(
                f"ALTER TABLE ai_risk_reservations ADD COLUMN {column} {definition}"
            )
        except Exception as exc:
            if "duplicate" not in str(exc).lower() and "exists" not in str(exc).lower():
                raise
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_risk_reservation_budget
        ON ai_risk_reservations(account_id, market, strategy_id, status)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_position_protections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL UNIQUE,
            market TEXT NOT NULL,
            account_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'long',
            required_qty INTEGER NOT NULL,
            protected_qty INTEGER NOT NULL DEFAULT 0,
            initial_stop_price REAL NOT NULL,
            current_stop_price REAL NOT NULL,
            status TEXT NOT NULL,
            broker_order_id TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            activated_at TEXT,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_position_protection_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            protection_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            required_qty INTEGER,
            protected_qty INTEGER,
            stop_price REAL,
            broker_order_id TEXT,
            payload TEXT,
            reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_position_protection_status
        ON ai_position_protections(status, market, account_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_daily_equity_baselines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            market TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            baseline_equity REAL NOT NULL,
            snapshot_id TEXT NOT NULL,
            data_as_of TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(account_id, market, trading_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_daily_account_cashflows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            market TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            external_ref TEXT NOT NULL,
            amount REAL NOT NULL,
            kind TEXT NOT NULL,
            reconciled INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(account_id, market, external_ref)
        )
        """
    )


# --------------------------------------------------------------------------- #
# 스캔 (§5.3·§6.1)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 후보 (§5.3·§6.2)
# --------------------------------------------------------------------------- #
_CAND_JSON_FIELDS = (
    "positive_factors", "negative_factors", "related_narratives",
    "warnings", "invalidation_conditions",
)


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


# --------------------------------------------------------------------------- #
# 관찰종목 (§5.5·§6.3)
# --------------------------------------------------------------------------- #
_WATCH_JSON_FIELDS = ("related_narratives", "confirmation_conditions", "invalidation_conditions")


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


# --------------------------------------------------------------------------- #
# 자동화 정책 (§6.6)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 성과 / 실행 계획 / 실행 이력 / 타이밍 신호
# --------------------------------------------------------------------------- #
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


def save_execution_plan(plan: dict[str, Any]) -> int:
    row = dict(plan)
    row["market"] = require_storable_market(row.get("market"))
    row.setdefault("created_at", _now())
    row["updated_at"] = _now()
    if "safety_checks" in row:
        row["safety_checks"] = dumps_json(row.get("safety_checks"))
    cols = [
        "candidate_id", "market", "symbol", "strategy_id", "strategy_version", "action",
        "entry_price", "stop_price", "take_profit", "risk_budget", "quantity",
        "estimated_cost", "safety_checks", "status", "approval_market", "approval_db",
        "approval_id", "approval_status", "created_at", "updated_at",
    ]
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO ai_stock_execution_plans ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})",
            tuple(row.get(c) for c in cols),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_execution_plans(market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as conn:
        if market and str(market).upper() != "ALL":
            rows = conn.execute(
                "SELECT * FROM ai_stock_execution_plans WHERE market=? ORDER BY id DESC LIMIT ?",
                (require_storable_market(market), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ai_stock_execution_plans ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["safety_checks"] = loads_json(d.get("safety_checks"), [])
            out.append(d)
        return out


def get_execution_plan(plan_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM ai_stock_execution_plans WHERE id=?", (int(plan_id),)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["safety_checks"] = loads_json(d.get("safety_checks"), [])
        return d


def update_execution_plan_approval(
    plan_id: int,
    *,
    approval_market: str,
    approval_db: str,
    approval_id: int,
    approval_status: str = "pending",
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE ai_stock_execution_plans
            SET approval_market=?, approval_db=?, approval_id=?, approval_status=?,
                status=?, updated_at=?
            WHERE id=?
            """,
            (
                require_storable_market(approval_market),
                approval_db,
                int(approval_id),
                approval_status,
                "approval_queued",
                _now(),
                int(plan_id),
            ),
        )
        conn.commit()


def update_execution_plan_status(
    plan_id: int,
    *,
    status: str,
    approval_status: str | None = None,
) -> None:
    fields = {"status": status, "updated_at": _now()}
    if approval_status is not None:
        fields["approval_status"] = approval_status
    sets = ", ".join(f"{k}=?" for k in fields)
    with _connect() as conn:
        conn.execute(
            f"UPDATE ai_stock_execution_plans SET {sets} WHERE id=?",
            (*fields.values(), int(plan_id)),
        )
        conn.commit()


def log_execution_run(data: dict[str, Any]) -> int:
    row = dict(data)
    row.setdefault("started_at", _now())
    for f in ("policy_snapshot", "safety_checks"):
        if f in row:
            row[f] = dumps_json(row.get(f))
    cols = [
        "strategy_id", "market", "scan_id", "candidate_id", "plan_id", "run_type",
        "automation_level", "status", "blocked_stage", "blocked_reason",
        "policy_snapshot", "safety_checks", "approval_market", "approval_db",
        "approval_id", "order_id", "broker_order_id", "started_at", "completed_at",
    ]
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO ai_stock_execution_runs ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})",
            tuple(row.get(c) for c in cols),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_execution_runs(market: str | None = None, strategy_id: str | None = None,
                        limit: int = 100) -> list[dict[str, Any]]:
    where, params = [], []
    if market and str(market).upper() != "ALL":
        where.append("market=?")
        params.append(require_storable_market(market))
    if strategy_id:
        where.append("strategy_id=?")
        params.append(strategy_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_stock_execution_runs {clause} ORDER BY id DESC LIMIT ?", tuple(params)
        ).fetchall()
        return [dict(r) for r in rows]


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


_POSITION_JSON_FIELDS = {
    "invalidation_conditions", "target_plan", "trailing_stop",
}
_DECISION_JSON_FIELDS = {
    "invalidation_conditions", "intent_payload", "risk_decision", "token_usage",
}


def _decode_fields(row: sqlite3.Row | dict[str, Any], fields: set[str]) -> dict[str, Any]:
    result = dict(row)
    for field in fields:
        result[field] = loads_json(result.get(field), [] if field == "invalidation_conditions" else {})
    return result


def create_strategy_position(data: dict[str, Any]) -> int:
    """Create one strategy-owned virtual position.

    Active ownership is unique per (market, account, symbol, strategy).  Broker
    holdings may aggregate multiple rows, but a strategy may only reduce the
    quantity recorded in its own row.
    """
    row = dict(data)
    row["market"] = require_storable_market(row.get("market"))
    row["account_id"] = str(row.get("account_id") or "")
    row["strategy_id"] = str(row.get("strategy_id") or "").strip()
    row["symbol"] = str(row.get("symbol") or "").strip()
    if not row["strategy_id"] or not row["symbol"]:
        raise ValueError("strategy_id and symbol are required")
    row.setdefault("status", "pending_entry")
    row.setdefault("side", "long")
    row.setdefault("filled_qty", 0)
    row.setdefault("remaining_qty", row["filled_qty"])
    row.setdefault("realized_pnl", 0.0)
    row.setdefault("unrealized_pnl", 0.0)
    row.setdefault("created_at", _now())
    row["updated_at"] = _now()
    for field in _POSITION_JSON_FIELDS:
        if field in row:
            row[field] = dumps_json(row.get(field))
    cols = [
        "market", "account_id", "symbol", "name", "strategy_id", "strategy_version",
        "profile_hash", "status", "side", "opened_at", "closed_at", "entry_thesis",
        "invalidation_conditions", "entry_price", "average_price", "filled_qty",
        "remaining_qty", "initial_stop_price", "current_stop_price", "target_plan",
        "trailing_stop", "max_holding_until", "initial_risk_amount",
        "current_risk_amount", "realized_pnl", "unrealized_pnl", "last_decision_id",
        "last_evaluated_at", "created_at", "updated_at",
    ]
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO ai_strategy_positions ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})",
            tuple(row.get(c) for c in cols),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_strategy_position(position_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_strategy_positions WHERE id=?", (int(position_id),)
        ).fetchone()
        return _decode_fields(row, _POSITION_JSON_FIELDS) if row else None


def list_strategy_positions(
    *,
    market: str | None = None,
    strategy_id: str | None = None,
    symbol: str | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if market:
        where.append("market=?")
        params.append(require_storable_market(market))
    if strategy_id:
        where.append("strategy_id=?")
        params.append(strategy_id)
    if symbol:
        where.append("symbol=?")
        params.append(symbol)
    if active_only:
        where.append("status IN ('pending_entry', 'open', 'exit_pending')")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_strategy_positions{clause} ORDER BY id", tuple(params)
        ).fetchall()
        return [_decode_fields(row, _POSITION_JSON_FIELDS) for row in rows]


def abandon_pending_strategy_position(position_id: int, *, reason: str = "") -> bool:
    """Close an unfilled entry shell after downstream planning failed."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE ai_strategy_positions
            SET status='closed', closed_at=?, entry_thesis=CASE
                    WHEN ?='' THEN entry_thesis
                    ELSE entry_thesis || ' [abandoned: ' || ? || ']'
                END,
                updated_at=?
            WHERE id=? AND status='pending_entry'
              AND filled_qty=0 AND remaining_qty=0
            """,
            (_now(), reason, reason, _now(), int(position_id)),
        )
        conn.commit()
        return int(cur.rowcount or 0) == 1


def save_strategy_decision(data: dict[str, Any]) -> int:
    row = dict(data)
    row["market"] = require_storable_market(row.get("market"))
    for required in ("decision_key", "strategy_id", "symbol", "action", "intent_payload"):
        if row.get(required) in (None, ""):
            raise ValueError(f"{required} is required")
    row.setdefault("ts", _now())
    row.setdefault("created_at", _now())
    for field in _DECISION_JSON_FIELDS:
        if field in row and not isinstance(row.get(field), str):
            row[field] = dumps_json(row.get(field))
    cols = [
        "decision_key", "ts", "strategy_id", "strategy_version", "profile_hash",
        "model_provider", "model_name", "prompt_version", "market", "symbol",
        "position_id", "market_snapshot_id", "portfolio_snapshot_id",
        "input_feature_hash", "data_as_of", "action", "confidence", "thesis",
        "invalidation_conditions", "intent_payload", "risk_decision", "final_action",
        "rejection_reason", "order_id", "token_usage", "latency_ms", "created_at",
    ]
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO ai_strategy_decisions ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})",
            tuple(row.get(c) for c in cols),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_strategy_decision_by_key(decision_key: str) -> dict[str, Any] | None:
    """Return an already persisted decision for idempotent cycle processing."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_strategy_decisions WHERE decision_key=?",
            (str(decision_key),),
        ).fetchone()
        return _decode_fields(row, _DECISION_JSON_FIELDS) if row else None


def get_strategy_decision(decision_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_strategy_decisions WHERE id=?", (int(decision_id),)
        ).fetchone()
        return _decode_fields(row, _DECISION_JSON_FIELDS) if row else None


def list_strategy_decisions(
    *,
    market: str | None = None,
    strategy_id: str | None = None,
    symbol: str | None = None,
    final_action: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return recent autonomous strategy decisions for dashboard audit views."""
    where: list[str] = []
    params: list[Any] = []
    if market:
        where.append("market=?")
        params.append(require_storable_market(market))
    if strategy_id:
        where.append("strategy_id=?")
        params.append(str(strategy_id))
    if symbol:
        where.append("symbol=?")
        params.append(str(symbol))
    if final_action:
        where.append("final_action=?")
        params.append(str(final_action))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(int(limit), 1000)))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_strategy_decisions{clause} ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [_decode_fields(row, _DECISION_JSON_FIELDS) for row in rows]


def update_strategy_decision_result(
    decision_id: int,
    *,
    risk_decision: dict[str, Any],
    final_action: str,
    rejection_reason: str | None = None,
    order_id: int | None = None,
    position_id: int | None = None,
) -> bool:
    """Attach a deterministic result without changing the original intent."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE ai_strategy_decisions
            SET risk_decision=?, final_action=?, rejection_reason=?,
                order_id=?, position_id=COALESCE(position_id, ?)
            WHERE id=?
            """,
            (
                dumps_json(risk_decision),
                str(final_action),
                rejection_reason,
                order_id,
                position_id,
                int(decision_id),
            ),
        )
        conn.commit()
        return int(cur.rowcount or 0) == 1


def create_managed_order(data: dict[str, Any]) -> int:
    row = dict(data)
    row["market"] = require_storable_market(row.get("market"))
    for required in (
        "client_order_key", "decision_id", "symbol", "strategy_id", "action",
        "order_type", "requested_qty",
    ):
        if row.get(required) in (None, ""):
            raise ValueError(f"{required} is required")
    qty = int(row["requested_qty"])
    if qty <= 0:
        raise ValueError("requested_qty must be positive")
    row["requested_qty"] = qty
    row.setdefault("filled_qty", 0)
    row.setdefault("average_fill_price", 0.0)
    row.setdefault("status", "intent_created")
    row.setdefault("created_at", _now())
    row["updated_at"] = _now()
    cols = [
        "client_order_key", "decision_id", "position_id", "market", "symbol",
        "strategy_id", "action", "order_type", "requested_qty", "requested_price",
        "filled_qty", "average_fill_price", "status", "broker_order_id",
        "approval_id", "expires_at", "last_error", "submitted_at", "completed_at",
        "created_at", "updated_at",
    ]
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO ai_managed_orders ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})",
            tuple(row.get(c) for c in cols),
        )
        order_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO ai_managed_order_events
            (order_id, ts, from_status, to_status, reason)
            VALUES (?, ?, NULL, ?, ?)
            """,
            (order_id, _now(), row["status"], "managed order created"),
        )
        conn.commit()
        return order_id


def get_managed_order(order_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_managed_orders WHERE id=?", (int(order_id),)
        ).fetchone()
        return dict(row) if row else None


def bind_managed_order_approval(
    order_id: int, *, approval_id: int, expected_status: str = "risk_approved"
) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE ai_managed_orders SET approval_id=?, updated_at=?
            WHERE id=? AND status=? AND approval_id IS NULL
            """,
            (int(approval_id), _now(), int(order_id), expected_status),
        )
        conn.commit()
        return int(cur.rowcount or 0) == 1


def get_managed_order_by_key(client_order_key: str) -> dict[str, Any] | None:
    """Return an existing managed order without submitting it to a broker."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_managed_orders WHERE client_order_key=?",
            (str(client_order_key),),
        ).fetchone()
        return dict(row) if row else None


def list_unsettled_managed_orders(
    *, market: str | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    statuses = (
        "submitting", "submitted", "partially_filled",
        "cancel_pending", "broker_unknown",
    )
    params: list[Any] = list(statuses)
    where = f"status IN ({', '.join('?' for _ in statuses)})"
    if market:
        where += " AND market=?"
        params.append(require_storable_market(market))
    params.append(max(1, min(int(limit), 5000)))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_managed_orders WHERE {where} ORDER BY id LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


def list_managed_orders(
    *,
    market: str | None = None,
    strategy_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return recent autonomous managed orders for dashboard audit views."""
    where: list[str] = []
    params: list[Any] = []
    if market:
        where.append("market=?")
        params.append(require_storable_market(market))
    if strategy_id:
        where.append("strategy_id=?")
        params.append(str(strategy_id))
    if status:
        where.append("status=?")
        params.append(str(status))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(int(limit), 1000)))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_managed_orders{clause} ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


def count_daily_new_risk_managed_orders(
    *,
    account_id: str,
    market: str,
    strategy_id: str,
    day_start: str,
    day_end: str,
) -> int:
    """Count authoritative broker-reached buy orders for one trading day."""
    market = require_storable_market(market)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM ai_managed_orders o
            JOIN ai_strategy_positions p ON p.id=o.position_id
            WHERE p.account_id=? AND o.market=? AND o.strategy_id=?
              AND o.action='buy'
              AND o.status IN ('submitted', 'partially_filled', 'filled')
              AND COALESCE(o.submitted_at, o.created_at) >= ?
              AND COALESCE(o.submitted_at, o.created_at) < ?
            """,
            (str(account_id), market, str(strategy_id), str(day_start), str(day_end)),
        ).fetchone()
        return int(row["count"] or 0)


def transition_managed_order(
    order_id: int,
    *,
    expected_status: str,
    new_status: str,
    reason: str = "",
    broker_payload: dict[str, Any] | None = None,
    broker_order_id: str | None = None,
    last_error: str | None = None,
) -> bool:
    """Compare-and-set an order state so concurrent workers cannot skip states."""
    with _connect() as conn:
        _begin_write(conn)
        cur = conn.execute(
            """
            UPDATE ai_managed_orders
            SET status=?, broker_order_id=COALESCE(?, broker_order_id),
                last_error=COALESCE(?, last_error),
                submitted_at=CASE WHEN ?='submitted' THEN COALESCE(submitted_at, ?) ELSE submitted_at END,
                completed_at=CASE WHEN ? IN ('filled', 'rejected', 'expired', 'canceled')
                                  THEN COALESCE(completed_at, ?) ELSE completed_at END,
                updated_at=?
            WHERE id=? AND status=?
            """,
            (
                new_status, broker_order_id, last_error,
                new_status, _now(), new_status, _now(), _now(),
                int(order_id), expected_status,
            ),
        )
        if int(cur.rowcount or 0) != 1:
            conn.rollback()
            return False
        conn.execute(
            """
            INSERT INTO ai_managed_order_events
            (order_id, ts, from_status, to_status, broker_payload, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(order_id), _now(), expected_status, new_status,
                dumps_json(broker_payload or {}), reason,
            ),
        )
        conn.commit()
        return True


def apply_managed_fill(
    order_id: int,
    *,
    fill_qty: int,
    fill_price: float,
    broker_payload: dict[str, Any] | None = None,
    fill_key: str | None = None,
) -> dict[str, Any]:
    """Atomically attribute a fill to the order's owning virtual position."""
    qty = int(fill_qty)
    price = float(fill_price)
    if qty <= 0 or price <= 0:
        raise ValueError("fill_qty and fill_price must be positive")
    with _connect() as conn:
        _begin_write(conn)
        order = conn.execute(
            "SELECT * FROM ai_managed_orders WHERE id=?", (int(order_id),)
        ).fetchone()
        if not order:
            conn.rollback()
            raise ValueError("managed order not found")
        order = dict(order)
        normalized_fill_key = str(fill_key or "").strip()
        if normalized_fill_key:
            prior_fill = conn.execute(
                """
                SELECT * FROM ai_managed_fills
                WHERE order_id=? AND fill_key=?
                """,
                (int(order_id), normalized_fill_key),
            ).fetchone()
            if prior_fill:
                if (
                    int(prior_fill["fill_qty"]) != qty
                    or float(prior_fill["fill_price"]) != price
                ):
                    conn.rollback()
                    raise ValueError("fill_key already identifies a different fill")
                conn.commit()
                return {
                    "order_id": int(order_id),
                    "position_id": int(order["position_id"]),
                    "filled_qty": int(order["filled_qty"]),
                    "order_status": str(order["status"]),
                    "position_remaining_qty": int(
                        conn.execute(
                            "SELECT remaining_qty FROM ai_strategy_positions WHERE id=?",
                            (int(order["position_id"]),),
                        ).fetchone()["remaining_qty"]
                    ),
                    "duplicate": True,
                }
        if order["status"] not in {
            "submitting", "submitted", "partially_filled",
            "broker_unknown", "cancel_pending"
        }:
            conn.rollback()
            raise ValueError(f"fill is not allowed from status {order['status']}")
        remaining_order_qty = int(order["requested_qty"]) - int(order["filled_qty"])
        if qty > remaining_order_qty:
            conn.rollback()
            raise ValueError("fill exceeds remaining order quantity")
        position_id = order.get("position_id")
        if not position_id:
            conn.rollback()
            raise ValueError("managed order has no strategy position owner")
        position = conn.execute(
            "SELECT * FROM ai_strategy_positions WHERE id=?", (int(position_id),)
        ).fetchone()
        if not position:
            conn.rollback()
            raise ValueError("strategy position owner not found")
        position = dict(position)
        if (
            str(position["strategy_id"]) != str(order["strategy_id"])
            or str(position["symbol"]) != str(order["symbol"])
            or str(position["market"]) != str(order["market"])
        ):
            conn.rollback()
            raise ValueError("order does not match strategy position ownership")

        old_filled = int(order["filled_qty"])
        new_filled = old_filled + qty
        average_fill = (
            (float(order["average_fill_price"]) * old_filled) + (price * qty)
        ) / new_filled
        new_status = "filled" if new_filled == int(order["requested_qty"]) else "partially_filled"
        now = _now()

        if order["action"] == "buy":
            old_position_qty = int(position["remaining_qty"])
            new_position_qty = old_position_qty + qty
            average_price = (
                (float(position["average_price"] or 0) * old_position_qty) + (price * qty)
            ) / new_position_qty
            conn.execute(
                """
                UPDATE ai_strategy_positions
                SET filled_qty=filled_qty+?, remaining_qty=?, average_price=?,
                    entry_price=COALESCE(entry_price, ?), opened_at=COALESCE(opened_at, ?),
                    status='open', updated_at=?
                WHERE id=?
                """,
                (qty, new_position_qty, average_price, price, now, now, int(position_id)),
            )
            stop_price = float(position["current_stop_price"] or 0)
            if stop_price <= 0:
                conn.rollback()
                raise ValueError("filled buy position has no hard stop price")
            protection = conn.execute(
                "SELECT * FROM ai_position_protections WHERE position_id=?",
                (int(position_id),),
            ).fetchone()
            if protection:
                if stop_price < float(protection["current_stop_price"]):
                    conn.rollback()
                    raise ValueError("hard stop cannot move in loss-expanding direction")
                protection_status = (
                    "amend_pending"
                    if int(protection["protected_qty"] or 0) > 0
                    else "pending"
                )
                conn.execute(
                    """
                    UPDATE ai_position_protections
                    SET required_qty=?, current_stop_price=?, status=?,
                        last_error=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (
                        new_position_qty, stop_price, protection_status, now,
                        int(protection["id"]),
                    ),
                )
                protection_id = int(protection["id"])
                protection_from = str(protection["status"])
            else:
                protection_cur = conn.execute(
                    """
                    INSERT INTO ai_position_protections
                    (position_id, market, account_id, symbol, strategy_id, side,
                     required_qty, protected_qty, initial_stop_price,
                     current_stop_price, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'long', ?, 0, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        int(position_id), position["market"], position["account_id"],
                        position["symbol"], position["strategy_id"], new_position_qty,
                        stop_price, stop_price, now, now,
                    ),
                )
                protection_id = int(protection_cur.lastrowid)
                protection_from = None
                protection_status = "pending"
            conn.execute(
                """
                INSERT INTO ai_position_protection_events
                (protection_id, ts, event_type, from_status, to_status,
                 required_qty, protected_qty, stop_price, reason)
                VALUES (?, ?, 'fill_protection_required', ?, ?, ?, ?, ?, ?)
                """,
                (
                    protection_id, now, protection_from, protection_status,
                    new_position_qty,
                    int(protection["protected_qty"] or 0) if protection else 0,
                    stop_price, f"atomic entry fill qty={qty}",
                ),
            )
        elif order["action"] == "sell":
            owned_qty = int(position["remaining_qty"])
            if qty > owned_qty:
                conn.rollback()
                raise ValueError("sell fill exceeds strategy-owned quantity")
            new_position_qty = owned_qty - qty
            realized_delta = (price - float(position["average_price"] or 0)) * qty
            position_status = "closed" if new_position_qty == 0 else "open"
            conn.execute(
                """
                UPDATE ai_strategy_positions
                SET remaining_qty=?, realized_pnl=realized_pnl+?, status=?,
                    closed_at=CASE WHEN ?=0 THEN ? ELSE closed_at END, updated_at=?
                WHERE id=?
                """,
                (
                    new_position_qty, realized_delta, position_status,
                    new_position_qty, now, now, int(position_id),
                ),
            )
            protection = conn.execute(
                "SELECT * FROM ai_position_protections WHERE position_id=?",
                (int(position_id),),
            ).fetchone()
            if protection:
                protection_status = (
                    "cancel_pending" if new_position_qty == 0 else "amend_pending"
                )
                conn.execute(
                    """
                    UPDATE ai_position_protections
                    SET required_qty=CASE WHEN ?>0 THEN ? ELSE required_qty END,
                        status=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        new_position_qty, new_position_qty, protection_status, now,
                        int(protection["id"]),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO ai_position_protection_events
                    (protection_id, ts, event_type, from_status, to_status,
                     required_qty, protected_qty, stop_price, broker_order_id,
                     reason)
                    VALUES (?, ?, 'fill_protection_reconcile_required', ?, ?,
                            ?, ?, ?, ?, ?)
                    """,
                    (
                        int(protection["id"]), now, protection["status"],
                        protection_status, new_position_qty,
                        int(protection["protected_qty"] or 0),
                        protection["current_stop_price"],
                        protection["broker_order_id"],
                        f"atomic sell fill qty={qty}",
                    ),
                )
        else:
            conn.rollback()
            raise ValueError("managed fill action must be buy or sell")

        conn.execute(
            """
            UPDATE ai_managed_orders
            SET filled_qty=?, average_fill_price=?, status=?,
                completed_at=CASE WHEN ?='filled' THEN ? ELSE completed_at END,
                updated_at=?
            WHERE id=?
            """,
            (new_filled, average_fill, new_status, new_status, now, now, int(order_id)),
        )
        if normalized_fill_key:
            conn.execute(
                """
                INSERT INTO ai_managed_fills
                (order_id, fill_key, fill_qty, fill_price, broker_payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(order_id), normalized_fill_key, qty, price,
                    dumps_json(broker_payload or {}), now,
                ),
            )
        conn.execute(
            """
            INSERT INTO ai_managed_order_events
            (order_id, ts, from_status, to_status, filled_qty, fill_price,
             broker_payload, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(order_id), now, order["status"], new_status, qty, price,
                dumps_json(broker_payload or {}), "broker fill reconciled",
            ),
        )
        conn.commit()
        return {
            "order_id": int(order_id),
            "position_id": int(position_id),
            "filled_qty": new_filled,
            "order_status": new_status,
            "position_remaining_qty": new_position_qty,
        }


# --------------------------------------------------------------------------- #
# 불변 판단 스냅샷과 원자적 위험 예약
# --------------------------------------------------------------------------- #
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


def reserve_risk_budget(
    data: dict[str, Any],
    *,
    available_cash: float,
    risk_budget_limit: float,
) -> dict[str, Any]:
    """Atomically reserve cash and portfolio risk for a future order.

    The active composite key is idempotent. Budget checks include every active
    strategy reservation for the same account and market, preventing parallel
    workers and different strategies from spending the same limits twice.
    """
    row = dict(data)
    row["market"] = require_storable_market(row.get("market"))
    row["account_id"] = str(row.get("account_id") or "").strip()
    row["strategy_id"] = str(row.get("strategy_id") or "").strip()
    if not row["account_id"] or not row["strategy_id"]:
        raise ValueError("account_id and strategy_id are required")
    row["position_id"] = int(row.get("position_id") or 0)
    row["order_id"] = int(row.get("order_id") or 0)
    if row["position_id"] <= 0 and row["order_id"] <= 0:
        raise ValueError("position_id or order_id is required")

    for field in ("cash_amount", "risk_amount"):
        value = float(row.get(field, 0))
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{field} must be finite and non-negative")
        row[field] = value
    row["symbol"] = str(row.get("symbol") or "").strip()
    row["sector_key"] = str(row.get("sector_key") or "").strip()
    row["exposure_amount"] = float(row.get("exposure_amount", row["cash_amount"]))
    if not math.isfinite(row["exposure_amount"]) or row["exposure_amount"] < 0:
        raise ValueError("exposure_amount must be finite and non-negative")
    if row["cash_amount"] == 0 and row["risk_amount"] == 0:
        raise ValueError("reservation must consume cash or risk budget")
    cash_limit = float(available_cash)
    risk_limit = float(risk_budget_limit)
    if (
        not math.isfinite(cash_limit)
        or not math.isfinite(risk_limit)
        or cash_limit < 0
        or risk_limit < 0
    ):
        raise ValueError("available_cash and risk_budget_limit must be finite and non-negative")

    key = (
        row["account_id"], row["market"], row["strategy_id"],
        row["position_id"], row["order_id"],
    )
    with _connect() as conn:
        _begin_write(conn)
        existing = conn.execute(
            """
            SELECT * FROM ai_risk_reservations
            WHERE account_id=? AND market=? AND strategy_id=?
              AND position_id=? AND order_id=? AND status='active'
            """,
            key,
        ).fetchone()
        if existing:
            if (
                float(existing["cash_amount"]) != row["cash_amount"]
                or float(existing["risk_amount"]) != row["risk_amount"]
                or float(existing["exposure_amount"] or 0) != row["exposure_amount"]
                or str(existing["symbol"] or "") != row["symbol"]
                or str(existing["sector_key"] or "") != row["sector_key"]
            ):
                conn.rollback()
                raise ValueError("active reservation key already has different amounts")
            conn.commit()
            result = dict(existing)
            result["created"] = False
            return result

        totals = conn.execute(
            """
            SELECT COALESCE(SUM(cash_amount), 0) AS reserved_cash,
                   COALESCE(SUM(risk_amount), 0) AS reserved_risk
            FROM ai_risk_reservations
            WHERE account_id=? AND market=? AND status='active'
            """,
            (row["account_id"], row["market"]),
        ).fetchone()
        reserved_cash = float(totals["reserved_cash"])
        reserved_risk = float(totals["reserved_risk"])
        if reserved_cash + row["cash_amount"] > cash_limit:
            conn.rollback()
            raise ValueError("cash reservation exceeds available cash")
        if reserved_risk + row["risk_amount"] > risk_limit:
            conn.rollback()
            raise ValueError("risk reservation exceeds risk budget")
        exposure_limits = row.get("exposure_limits")
        if exposure_limits is not None:
            if not row["symbol"] or not row["sector_key"]:
                conn.rollback()
                raise ValueError("symbol and sector_key are required for exposure reservation")
            limits = {
                key: float(exposure_limits[key])
                for key in ("position", "market", "sector", "strategy")
            }
            if any(not math.isfinite(value) or value < 0 for value in limits.values()):
                conn.rollback()
                raise ValueError("exposure reservation limits must be finite and non-negative")
            exposure_rows = conn.execute(
                """
                SELECT r.strategy_id, r.symbol, r.sector_key,
                       COALESCE(
                           (
                               SELECT CASE
                                   WHEN o.requested_qty > o.filled_qty
                                   THEN (o.requested_qty - o.filled_qty)
                                        * COALESCE(o.requested_price, 0)
                                   ELSE 0
                               END
                               FROM ai_managed_orders o
                               WHERE o.position_id=r.position_id
                                 AND o.action='buy'
                                 AND o.status IN (
                                     'intent_created', 'risk_approved',
                                     'approval_queued', 'approved', 'submitting',
                                     'submitted', 'partially_filled',
                                     'cancel_pending', 'broker_unknown'
                                 )
                               ORDER BY o.id DESC LIMIT 1
                           ),
                           r.exposure_amount
                       ) AS pending_exposure_value
                FROM ai_risk_reservations r
                WHERE r.account_id=? AND r.market=? AND r.status='active'
                """,
                (row["account_id"], row["market"]),
            ).fetchall()
            totals_exposure = {
                "position": sum(float(item["pending_exposure_value"]) for item in exposure_rows if item["symbol"] == row["symbol"]),
                "market": sum(float(item["pending_exposure_value"]) for item in exposure_rows),
                "sector": sum(float(item["pending_exposure_value"]) for item in exposure_rows if item["sector_key"] == row["sector_key"]),
                "strategy": sum(float(item["pending_exposure_value"]) for item in exposure_rows if item["strategy_id"] == row["strategy_id"]),
            }
            for dimension, used in totals_exposure.items():
                if used + row["exposure_amount"] > limits[dimension]:
                    conn.rollback()
                    raise ValueError(f"{dimension} exposure reservation exceeds limit")

        now = _now()
        cur = conn.execute(
            """
            INSERT INTO ai_risk_reservations
            (account_id, market, strategy_id, position_id, order_id,
             cash_amount, risk_amount, symbol, sector_key, exposure_amount,
             status, reason, expires_at,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                *key, row["cash_amount"], row["risk_amount"],
                row["symbol"], row["sector_key"], row["exposure_amount"], row.get("reason"),
                row.get("expires_at"), now, now,
            ),
        )
        reservation_id = int(cur.lastrowid)
        inserted = conn.execute(
            "SELECT * FROM ai_risk_reservations WHERE id=?", (reservation_id,)
        ).fetchone()
        conn.commit()
        if inserted is None:
            raise RuntimeError("risk reservation disappeared after commit")
        result = dict(inserted)
        result["created"] = True
        return result


def get_risk_reservation(reservation_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_risk_reservations WHERE id=?", (int(reservation_id),)
        ).fetchone()
        return dict(row) if row else None


def get_active_risk_reservation_for_position(
    position_id: int,
) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM ai_risk_reservations
            WHERE position_id=? AND status='active'
            ORDER BY id DESC LIMIT 1
            """,
            (int(position_id),),
        ).fetchone()
        return dict(row) if row else None


def list_risk_reservations(
    *,
    account_id: str | None = None,
    market: str | None = None,
    strategy_id: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    for field, value in (
        ("account_id", account_id),
        ("strategy_id", strategy_id),
        ("status", status),
    ):
        if value is not None:
            where.append(f"{field}=?")
            params.append(str(value))
    if market:
        where.append("market=?")
        params.append(require_storable_market(market))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(int(limit), 5000)))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_risk_reservations{clause} ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


def list_active_reserved_exposures(
    *, account_id: str, market: str
) -> list[dict[str, Any]]:
    """Return remaining pending buy exposure, not already-filled quantity."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT r.id AS reservation_id, r.strategy_id, r.position_id,
                   p.symbol,
                   COALESCE(
                       (
                           SELECT CASE
                               WHEN o.requested_qty > o.filled_qty
                               THEN (o.requested_qty - o.filled_qty)
                                    * COALESCE(o.requested_price, 0)
                               ELSE 0
                           END
                           FROM ai_managed_orders o
                           WHERE o.position_id=r.position_id
                             AND o.action='buy'
                             AND o.status IN (
                                 'intent_created', 'risk_approved',
                                 'approval_queued', 'approved', 'submitting',
                                 'submitted', 'partially_filled',
                                 'cancel_pending', 'broker_unknown'
                             )
                           ORDER BY o.id DESC LIMIT 1
                       ),
                       r.exposure_amount
                   ) AS pending_exposure_value
            FROM ai_risk_reservations r
            JOIN ai_strategy_positions p ON p.id=r.position_id
            WHERE r.account_id=? AND r.market=? AND r.status='active'
            """,
            (str(account_id), require_storable_market(market)),
        ).fetchall()
        return [dict(row) for row in rows]


def release_risk_reservation(
    reservation_id: int,
    *,
    final_status: str = "released",
    reason: str = "",
) -> dict[str, Any]:
    """Idempotently release or consume an active reservation."""
    if final_status not in {"released", "consumed", "expired"}:
        raise ValueError("invalid reservation final_status")
    with _connect() as conn:
        _begin_write(conn)
        existing = conn.execute(
            "SELECT * FROM ai_risk_reservations WHERE id=?", (int(reservation_id),)
        ).fetchone()
        if not existing:
            conn.rollback()
            raise ValueError("risk reservation not found")
        if existing["status"] != "active":
            conn.commit()
            return dict(existing)
        now = _now()
        conn.execute(
            """
            UPDATE ai_risk_reservations
            SET status=?, reason=CASE WHEN ?='' THEN reason ELSE ? END,
                released_at=?, updated_at=?
            WHERE id=? AND status='active'
            """,
            (final_status, reason, reason, now, now, int(reservation_id)),
        )
        updated = conn.execute(
            "SELECT * FROM ai_risk_reservations WHERE id=?", (int(reservation_id),)
        ).fetchone()
        conn.commit()
        return dict(updated)


# --------------------------------------------------------------------------- #
# 하드스톱 보호 원장
# --------------------------------------------------------------------------- #
def request_position_protection(
    position_id: int,
    *,
    required_qty: int,
    stop_price: float,
    reason: str = "entry fill protection requested",
) -> dict[str, Any]:
    """Create or expand the durable hard-stop request for a long position."""
    qty = int(required_qty)
    stop = float(stop_price)
    if qty <= 0 or not math.isfinite(stop) or stop <= 0:
        raise ValueError("required_qty and stop_price must be positive")
    with _connect() as conn:
        _begin_write(conn)
        position = conn.execute(
            "SELECT * FROM ai_strategy_positions WHERE id=?", (int(position_id),)
        ).fetchone()
        if not position:
            conn.rollback()
            raise ValueError("strategy position not found")
        if str(position["side"] or "long") != "long":
            conn.rollback()
            raise ValueError("hard-stop protection currently supports long positions only")
        open_qty = int(position["remaining_qty"] or 0)
        if qty != open_qty:
            conn.rollback()
            raise ValueError("required_qty must equal strategy position open quantity")
        existing = conn.execute(
            "SELECT * FROM ai_position_protections WHERE position_id=?",
            (int(position_id),),
        ).fetchone()
        now = _now()
        if existing:
            current_stop = float(existing["current_stop_price"])
            if stop < current_stop:
                conn.rollback()
                raise ValueError("hard stop cannot move in loss-expanding direction")
            next_status = "amend_pending" if int(existing["protected_qty"] or 0) > 0 else "pending"
            conn.execute(
                """
                UPDATE ai_position_protections
                SET required_qty=?, current_stop_price=?, status=?,
                    last_error=NULL, updated_at=?
                WHERE id=?
                """,
                (qty, stop, next_status, now, int(existing["id"])),
            )
            protection_id = int(existing["id"])
            from_status = str(existing["status"])
            event_type = "protection_amend_requested"
        else:
            cur = conn.execute(
                """
                INSERT INTO ai_position_protections
                (position_id, market, account_id, symbol, strategy_id, side,
                 required_qty, protected_qty, initial_stop_price,
                 current_stop_price, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'long', ?, 0, ?, ?, 'pending', ?, ?)
                """,
                (
                    int(position_id), position["market"], position["account_id"],
                    position["symbol"], position["strategy_id"], qty, stop, stop,
                    now, now,
                ),
            )
            protection_id = int(cur.lastrowid)
            from_status = None
            next_status = "pending"
            event_type = "protection_requested"
        conn.execute(
            """
            INSERT INTO ai_position_protection_events
            (protection_id, ts, event_type, from_status, to_status,
             required_qty, protected_qty, stop_price, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                protection_id, now, event_type, from_status, next_status, qty,
                int(existing["protected_qty"] or 0) if existing else 0, stop, reason,
            ),
        )
        result = conn.execute(
            "SELECT * FROM ai_position_protections WHERE id=?", (protection_id,)
        ).fetchone()
        conn.commit()
        return dict(result)


def activate_position_protection(
    protection_id: int,
    *,
    broker_order_id: str,
    protected_qty: int,
    stop_price: float,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record broker acknowledgement without allowing weaker protection."""
    qty = int(protected_qty)
    stop = float(stop_price)
    if not broker_order_id or qty <= 0 or not math.isfinite(stop) or stop <= 0:
        raise ValueError("broker_order_id, protected_qty and stop_price are required")
    with _connect() as conn:
        _begin_write(conn)
        current = conn.execute(
            "SELECT * FROM ai_position_protections WHERE id=?", (int(protection_id),)
        ).fetchone()
        if not current:
            conn.rollback()
            raise ValueError("position protection not found")
        if stop < float(current["current_stop_price"]):
            conn.rollback()
            raise ValueError("broker stop is below requested hard stop")
        required = int(current["required_qty"])
        if qty > required:
            conn.rollback()
            raise ValueError("protected_qty exceeds required_qty")
        next_status = "active" if qty == required else "partial"
        now = _now()
        conn.execute(
            """
            UPDATE ai_position_protections
            SET protected_qty=?, current_stop_price=?, status=?,
                broker_order_id=?, last_error=NULL, activated_at=?,
                updated_at=?
            WHERE id=?
            """,
            (qty, stop, next_status, broker_order_id, now, now, int(protection_id)),
        )
        conn.execute(
            """
            INSERT INTO ai_position_protection_events
            (protection_id, ts, event_type, from_status, to_status,
             required_qty, protected_qty, stop_price, broker_order_id,
             payload, reason)
            VALUES (?, ?, 'broker_protection_active', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(protection_id), now, current["status"], next_status, required,
                qty, stop, broker_order_id, dumps_json(payload or {}),
                "broker acknowledged hard stop",
            ),
        )
        updated = conn.execute(
            "SELECT * FROM ai_position_protections WHERE id=?", (int(protection_id),)
        ).fetchone()
        conn.commit()
        return dict(updated)


def fail_position_protection(
    protection_id: int,
    *,
    error: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not str(error or "").strip():
        raise ValueError("protection error is required")
    with _connect() as conn:
        _begin_write(conn)
        current = conn.execute(
            "SELECT * FROM ai_position_protections WHERE id=?", (int(protection_id),)
        ).fetchone()
        if not current:
            conn.rollback()
            raise ValueError("position protection not found")
        now = _now()
        conn.execute(
            """
            UPDATE ai_position_protections
            SET status='failed', last_error=?, updated_at=? WHERE id=?
            """,
            (str(error), now, int(protection_id)),
        )
        conn.execute(
            """
            INSERT INTO ai_position_protection_events
            (protection_id, ts, event_type, from_status, to_status,
             required_qty, protected_qty, stop_price, broker_order_id,
             payload, reason)
            VALUES (?, ?, 'broker_protection_failed', ?, 'failed', ?, ?, ?, ?, ?, ?)
            """,
            (
                int(protection_id), now, current["status"], current["required_qty"],
                current["protected_qty"], current["current_stop_price"],
                current["broker_order_id"], dumps_json(payload or {}), str(error),
            ),
        )
        updated = conn.execute(
            "SELECT * FROM ai_position_protections WHERE id=?", (int(protection_id),)
        ).fetchone()
        conn.commit()
        return dict(updated)


def cancel_position_protection(
    protection_id: int,
    *,
    reason: str,
) -> dict[str, Any]:
    """Cancel only after the strategy-owned position has no open quantity."""
    with _connect() as conn:
        _begin_write(conn)
        current = conn.execute(
            "SELECT * FROM ai_position_protections WHERE id=?", (int(protection_id),)
        ).fetchone()
        if not current:
            conn.rollback()
            raise ValueError("position protection not found")
        position = conn.execute(
            "SELECT remaining_qty FROM ai_strategy_positions WHERE id=?",
            (int(current["position_id"]),),
        ).fetchone()
        if not position or int(position["remaining_qty"] or 0) > 0:
            conn.rollback()
            raise ValueError("cannot cancel hard stop while position quantity is open")
        now = _now()
        conn.execute(
            """
            UPDATE ai_position_protections
            SET status='canceled', protected_qty=0, completed_at=?, updated_at=?
            WHERE id=?
            """,
            (now, now, int(protection_id)),
        )
        conn.execute(
            """
            INSERT INTO ai_position_protection_events
            (protection_id, ts, event_type, from_status, to_status,
             required_qty, protected_qty, stop_price, broker_order_id, reason)
            VALUES (?, ?, 'protection_canceled', ?, 'canceled', ?, 0, ?, ?, ?)
            """,
            (
                int(protection_id), now, current["status"], current["required_qty"],
                current["current_stop_price"], current["broker_order_id"], reason,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM ai_position_protections WHERE id=?", (int(protection_id),)
        ).fetchone()
        conn.commit()
        return dict(updated)


def request_position_protection_cancel(
    protection_id: int,
    *,
    reason: str,
) -> dict[str, Any]:
    """Persist a broker cancellation request, only after the position is flat."""
    with _connect() as conn:
        _begin_write(conn)
        current = conn.execute(
            "SELECT * FROM ai_position_protections WHERE id=?", (int(protection_id),)
        ).fetchone()
        if not current:
            conn.rollback()
            raise ValueError("position protection not found")
        position = conn.execute(
            "SELECT remaining_qty FROM ai_strategy_positions WHERE id=?",
            (int(current["position_id"]),),
        ).fetchone()
        if not position or int(position["remaining_qty"] or 0) > 0:
            conn.rollback()
            raise ValueError("cannot cancel hard stop while position quantity is open")
        if current["status"] in {"cancel_pending", "canceled"}:
            conn.commit()
            return dict(current)
        now = _now()
        conn.execute(
            """
            UPDATE ai_position_protections
            SET status='cancel_pending', updated_at=? WHERE id=?
            """,
            (now, int(protection_id)),
        )
        conn.execute(
            """
            INSERT INTO ai_position_protection_events
            (protection_id, ts, event_type, from_status, to_status,
             required_qty, protected_qty, stop_price, broker_order_id, reason)
            VALUES (?, ?, 'protection_cancel_requested', ?, 'cancel_pending',
                    ?, ?, ?, ?, ?)
            """,
            (
                int(protection_id), now, current["status"], current["required_qty"],
                current["protected_qty"], current["current_stop_price"],
                current["broker_order_id"], reason,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM ai_position_protections WHERE id=?", (int(protection_id),)
        ).fetchone()
        conn.commit()
        return dict(updated)


def get_position_protection(
    *,
    protection_id: int | None = None,
    position_id: int | None = None,
) -> dict[str, Any] | None:
    if protection_id is None and position_id is None:
        raise ValueError("protection_id or position_id is required")
    field, value = (
        ("id", int(protection_id))
        if protection_id is not None
        else ("position_id", int(position_id))
    )
    with _connect() as conn:
        row = conn.execute(
            f"SELECT * FROM ai_position_protections WHERE {field}=?", (value,)
        ).fetchone()
        return dict(row) if row else None


def list_position_protections(
    *,
    market: str | None = None,
    account_id: str | None = None,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if market:
        where.append("market=?")
        params.append(require_storable_market(market))
    if account_id is not None:
        where.append("account_id=?")
        params.append(str(account_id))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_position_protections{clause} ORDER BY id",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


def list_position_protection_events(protection_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM ai_position_protection_events
            WHERE protection_id=? ORDER BY id
            """,
            (int(protection_id),),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = loads_json(item.get("payload"), {})
            result.append(item)
        return result


def list_unprotected_strategy_positions(
    *, market: str | None = None
) -> list[dict[str, Any]]:
    """Return every open long quantity not fully covered by an active stop."""
    with _connect() as conn:
        market_clause = " AND p.market=?" if market else ""
        params = (require_storable_market(market),) if market else ()
        rows = conn.execute(
            f"""
            SELECT p.id AS position_id, p.market, p.account_id, p.symbol,
                   p.strategy_id, p.remaining_qty,
                   COALESCE(g.protected_qty, 0) AS protected_qty,
                   COALESCE(g.status, 'missing') AS protection_status,
                   g.id AS protection_id, g.current_stop_price, g.last_error
            FROM ai_strategy_positions p
            LEFT JOIN ai_position_protections g ON g.position_id=p.id
            WHERE p.side='long'
              AND p.status IN ('open', 'exit_pending')
              AND p.remaining_qty > 0
              AND (
                    g.id IS NULL
                    OR g.status NOT IN ('active', 'amend_pending')
                    OR g.protected_qty != p.remaining_qty
                  )
              {market_clause}
            ORDER BY p.id
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]


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
