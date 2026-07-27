from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Callable


def _close_connection(conn: sqlite3.Connection) -> None:
    conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {row[1] for row in rows}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


@dataclass(frozen=True)
class ApprovalRecord:
    id: int
    created_at: str
    updated_at: str
    symbol: str
    name: str
    action: str
    qty: int
    price: float
    reason: str
    source: str
    status: str
    response_msg: str
    strategy_id: str = ""
    strategy_version: int | None = None
    profile_hash: str = ""
    source_candidate_id: int | None = None
    managed_order_id: int | None = None
    decision_id: int | None = None
    position_id: int | None = None
    client_order_key: str = ""


def _approval_record_from_row(row: sqlite3.Row) -> ApprovalRecord:
    return ApprovalRecord(
        id=int(row["id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        symbol=str(row["symbol"]),
        name=str(row["name"]),
        action=str(row["action"]),
        qty=int(row["qty"]),
        price=float(row["price"]),
        reason=str(row["reason"] or ""),
        source=str(row["source"] or ""),
        status=str(row["status"]),
        response_msg=str(row["response_msg"] or ""),
        strategy_id=str(row["strategy_id"] or "") if "strategy_id" in row.keys() else "",
        strategy_version=int(row["strategy_version"]) if "strategy_version" in row.keys() and row["strategy_version"] is not None else None,
        profile_hash=str(row["profile_hash"] or "") if "profile_hash" in row.keys() else "",
        source_candidate_id=int(row["source_candidate_id"]) if "source_candidate_id" in row.keys() and row["source_candidate_id"] is not None else None,
        managed_order_id=int(row["managed_order_id"]) if "managed_order_id" in row.keys() and row["managed_order_id"] is not None else None,
        decision_id=int(row["decision_id"]) if "decision_id" in row.keys() and row["decision_id"] is not None else None,
        position_id=int(row["position_id"]) if "position_id" in row.keys() and row["position_id"] is not None else None,
        client_order_key=str(row["client_order_key"] or "") if "client_order_key" in row.keys() else "",
    )


class ApprovalRepository:
    def __init__(self, connect_fn: Callable[[], sqlite3.Connection]) -> None:
        self._connect_fn = connect_fn

    def init_db(self) -> None:
        conn = self._connect_fn()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS approvals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT NOT NULL,
                        action TEXT NOT NULL,
                        qty INTEGER NOT NULL,
                        price INTEGER NOT NULL,
                        reason TEXT,
                        source TEXT,
                        status TEXT NOT NULL,
                        response_msg TEXT
                    )
                    """
                )
                _ensure_column(conn, "approvals", "strategy_id", "TEXT")
                _ensure_column(conn, "approvals", "strategy_version", "INTEGER")
                _ensure_column(conn, "approvals", "profile_hash", "TEXT")
                _ensure_column(conn, "approvals", "source_candidate_id", "INTEGER")
                _ensure_column(conn, "approvals", "managed_order_id", "INTEGER")
                _ensure_column(conn, "approvals", "decision_id", "INTEGER")
                _ensure_column(conn, "approvals", "position_id", "INTEGER")
                _ensure_column(conn, "approvals", "client_order_key", "TEXT")
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_managed_order_unique
                    ON approvals(managed_order_id)
                    WHERE managed_order_id IS NOT NULL
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_client_order_key_unique
                    ON approvals(client_order_key)
                    WHERE client_order_key IS NOT NULL AND client_order_key <> ''
                    """
                )
        finally:
            _close_connection(conn)

    def create_approval(
        self,
        *,
        created_at: str,
        updated_at: str,
        symbol: str,
        name: str,
        action: str,
        qty: int,
        price: float,
        reason: str,
        source: str,
        status: str = "pending",
        response_msg: str = "",
        strategy_id: str = "",
        strategy_version: int | None = None,
        profile_hash: str = "",
        source_candidate_id: int | None = None,
        managed_order_id: int | None = None,
        decision_id: int | None = None,
        position_id: int | None = None,
        client_order_key: str = "",
    ) -> int:
        conn = self._connect_fn()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO approvals
                    (
                        created_at, updated_at, symbol, name, action, qty, price, reason, source,
                        status, response_msg, strategy_id, strategy_version, profile_hash,
                        source_candidate_id, managed_order_id, decision_id, position_id,
                        client_order_key
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        created_at,
                        updated_at,
                        symbol,
                        name,
                        action,
                        qty,
                        price,
                        reason,
                        source,
                        status,
                        response_msg,
                        strategy_id,
                        strategy_version,
                        profile_hash,
                        source_candidate_id,
                        managed_order_id,
                        decision_id,
                        position_id,
                        client_order_key,
                    ),
                )
                if int(cursor.rowcount or 0) == 1:
                    return int(cursor.lastrowid)
                if managed_order_id is not None:
                    existing = conn.execute(
                        "SELECT id FROM approvals WHERE managed_order_id=?",
                        (int(managed_order_id),),
                    ).fetchone()
                elif client_order_key:
                    existing = conn.execute(
                        "SELECT id FROM approvals WHERE client_order_key=?",
                        (str(client_order_key),),
                    ).fetchone()
                else:
                    existing = None
                if existing is None:
                    raise sqlite3.IntegrityError("approval insert was ignored")
                return int(existing[0])
        finally:
            _close_connection(conn)

    def get_approval(self, approval_id: int) -> ApprovalRecord | None:
        conn = self._connect_fn()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        finally:
            _close_connection(conn)
        if row is None:
            return None
        return _approval_record_from_row(row)

    def list_approvals(self, *, limit: int) -> list[ApprovalRecord]:
        conn = self._connect_fn()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM approvals ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            _close_connection(conn)
        return [_approval_record_from_row(row) for row in rows]

    def update_approval_status(
        self,
        approval_id: int,
        *,
        status: str,
        response_msg: str,
        updated_at: str,
    ) -> bool:
        conn = self._connect_fn()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE approvals
                    SET status = ?, response_msg = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, response_msg, updated_at, approval_id),
                )
                return cursor.rowcount > 0
        finally:
            _close_connection(conn)

    def transition_approval_status(
        self,
        approval_id: int,
        *,
        expected_status: str,
        status: str,
        response_msg: str,
        updated_at: str,
    ) -> bool:
        conn = self._connect_fn()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE approvals
                    SET status=?, response_msg=?, updated_at=?
                    WHERE id=? AND status=?
                    """,
                    (status, response_msg, updated_at, approval_id, expected_status),
                )
                return cursor.rowcount == 1
        finally:
            _close_connection(conn)
