from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


KST = timezone(timedelta(hours=9))


def now_kst() -> str:
    return datetime.now(KST).isoformat()


def connect_ai_stock():
    from src.db.repository import connect_db

    conn = connect_db()
    conn.row_factory = sqlite3.Row
    return conn


def begin_write(conn) -> None:
    """Serialize state-machine and risk-budget read/check/write operations."""
    conn.execute("BEGIN" if getattr(conn, "is_pg", False) else "BEGIN IMMEDIATE")
