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
    if row.get("status") in {"completed", "blocked", "failed"}:
        row.setdefault("completed_at", _now())
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

__all__ = [
    name for name, value in globals().items()
    if not name.startswith("_") and callable(value) and getattr(value, "__module__", None) == __name__
]
