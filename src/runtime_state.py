from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from src.db.connection import open_sqlite
from src.utils.processes import process_exists


KST = timezone(timedelta(hours=9))
DEFAULT_STATE_PATH = Path(".runtime/runtime_state.sqlite")


def _now_text() -> str:
    return datetime.now(KST).isoformat()


class RuntimeStateStore:
    def __init__(self, path: str | Path = DEFAULT_STATE_PATH) -> None:
        self.path = Path(path)
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self):
        conn = open_sqlite(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with closing(self._connect()) as conn:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS runtime_state (
                            state_key TEXT PRIMARY KEY,
                            payload TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
            self._initialized = True

    def get(self, state_key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        self.init_db()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload FROM runtime_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        if row is None:
            return dict(default or {})
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return dict(default or {})
        return payload if isinstance(payload, dict) else dict(default or {})

    def set(self, state_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.init_db()
        saved = dict(payload)
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO runtime_state (state_key, payload, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(state_key) DO UPDATE SET
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (state_key, json.dumps(saved, ensure_ascii=False, default=str), _now_text()),
                )
        return saved

    def claim(self, state_key: str, payload: dict[str, Any]) -> bool:
        self.init_db()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM runtime_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
            if row is not None:
                try:
                    current = json.loads(row["payload"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    current = {}
                if isinstance(current, dict) and current.get("is_running"):
                    conn.rollback()
                    return False
            self.set_with_connection(conn, state_key, payload)
            conn.commit()
        return True

    def set_with_connection(
        self,
        conn,
        state_key: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO runtime_state (state_key, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (
                state_key,
                json.dumps(dict(payload), ensure_ascii=False, default=str),
                _now_text(),
            ),
        )


runtime_state_store = RuntimeStateStore()


class PersistentRuntimeState(dict[str, Any]):
    def __init__(
        self,
        state_key: str,
        defaults: dict[str, Any],
        *,
        store: RuntimeStateStore = runtime_state_store,
    ) -> None:
        self.state_key = state_key
        self.defaults = dict(defaults)
        self.store = store
        super().__init__(self.store.get(state_key, self.defaults) or self.defaults)
        owner_pid = self.get("owner_pid")
        if self.get("is_running") and not _process_exists(owner_pid):
            super().update({
                "is_running": False,
                "completed_at": _now_text(),
                "error": "interrupted by process restart",
                "owner_pid": None,
            })
            self.persist()

    def refresh(self) -> "PersistentRuntimeState":
        current = self.store.get(self.state_key, self.defaults)
        super().clear()
        super().update(current)
        return self

    def persist(self) -> None:
        self.store.set(self.state_key, dict(self))

    def replace(self, payload: dict[str, Any]) -> None:
        super().clear()
        super().update(payload)
        self.persist()

    def claim(self, payload: dict[str, Any]) -> bool:
        if not self.store.claim(self.state_key, payload):
            self.refresh()
            return False
        super().clear()
        super().update(payload)
        return True

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self.persist()

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self.persist()

    def clear(self) -> None:
        super().clear()
        self.persist()

    def pop(self, key: str, default: Any = None) -> Any:
        value = super().pop(key, default)
        self.persist()
        return value

    def popitem(self) -> tuple[str, Any]:
        value = super().popitem()
        self.persist()
        return value

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key in self:
            return self[key]
        value = super().setdefault(key, default)
        self.persist()
        return value

    def update(self, *args, **kwargs) -> None:
        super().update(*args, **kwargs)
        self.persist()

    def __iter__(self) -> Iterator[str]:
        return super().__iter__()


def _process_exists(pid: Any) -> bool:
    return process_exists(pid)
