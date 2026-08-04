from __future__ import annotations

import os
import sqlite3
from pathlib import Path


DEFAULT_BUSY_TIMEOUT_MS = 5_000


class ClosingConnection(sqlite3.Connection):
    """SQLite connection whose context manager also releases the handle.

    ``sqlite3.Connection.__exit__`` only commits or rolls back; it does not
    close the connection.  Repository code consistently uses ``with
    open_sqlite(...) as conn``, so closing here makes that established pattern
    safe without requiring every call site to add a second context manager.
    """

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class DBWrapper:
    """Normalize SQLite and PostgreSQL connection behavior for repositories."""

    def __init__(self, conn, is_pg: bool = False, close_on_exit: bool = False):
        self.conn = conn
        self.is_pg = is_pg
        self.close_on_exit = close_on_exit

    def __enter__(self):
        self.conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return self.conn.__exit__(exc_type, exc_value, traceback)
        finally:
            if self.close_on_exit:
                self.conn.close()

    def execute(self, sql, params=()):
        if self.is_pg:
            from psycopg2.extras import DictCursor

            sql = sql.replace("?", "%s")
            if "AUTOINCREMENT" in sql:
                sql = sql.replace(
                    "INTEGER PRIMARY KEY AUTOINCREMENT",
                    "SERIAL PRIMARY KEY",
                )
            cursor = self.conn.cursor(cursor_factory=DictCursor)
        else:
            cursor = self.conn.cursor()
        cursor.execute(sql, params)
        return cursor

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        return self.conn.rollback()

    def close(self):
        return self.conn.close()

    @property
    def row_factory(self):
        return None if self.is_pg else self.conn.row_factory

    @row_factory.setter
    def row_factory(self, factory):
        if not self.is_pg:
            self.conn.row_factory = factory


def _busy_timeout_ms() -> int:
    raw = os.environ.get("SQLITE_BUSY_TIMEOUT_MS", str(DEFAULT_BUSY_TIMEOUT_MS))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_BUSY_TIMEOUT_MS


def open_sqlite(
    path: str | Path,
    *,
    row_factory=None,
) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_ms = _busy_timeout_ms()
    conn = sqlite3.connect(
        db_path,
        timeout=timeout_ms / 1_000,
        check_same_thread=False,
        factory=ClosingConnection,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if row_factory is not None:
            conn.row_factory = row_factory
        return conn
    except BaseException:
        conn.close()
        raise
