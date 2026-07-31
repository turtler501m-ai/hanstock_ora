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
    conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn
