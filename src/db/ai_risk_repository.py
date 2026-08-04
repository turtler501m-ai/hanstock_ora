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

__all__ = [
    name for name, value in globals().items()
    if not name.startswith("_") and callable(value) and getattr(value, "__module__", None) == __name__
]
