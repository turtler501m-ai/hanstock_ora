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

__all__ = [
    name for name, value in globals().items()
    if not name.startswith("_") and callable(value) and getattr(value, "__module__", None) == __name__
]
