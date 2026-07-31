from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
_TERMINAL_COMPLETION_STAGE = "execution_plan"
_cycle_write_locks_guard = threading.Lock()
_cycle_write_locks: dict[str, threading.Lock] = {}


def _connect():
    # Lazy import avoids a cycle while repository.init_db() initializes this schema.
    from src.db.repository import connect_db

    conn = connect_db()
    conn.row_factory = sqlite3.Row
    return conn


def init_analysis_cycle_tables(conn=None) -> None:
    owns_connection = conn is None
    db = conn or _connect()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_cycles (
                id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                trading_env TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                account_captured_at TEXT,
                stages_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_cycle_results (
                cycle_id TEXT NOT NULL REFERENCES analysis_cycles(id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
                payload TEXT,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (cycle_id, stage)
            )
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_analysis_cycles_strategy_created
            ON analysis_cycles(strategy_id, trading_env, created_at DESC)
            """
        )
        # Candidate and signal collection are alternative inputs depending on the
        # selected strategy.  A completed execution plan is the common terminal
        # stage, so repair cycles left running by the former all-stages rule.
        db.execute(
            """
            UPDATE analysis_cycles
            SET status = 'completed'
            WHERE status = 'running'
              AND EXISTS (
                  SELECT 1
                  FROM analysis_cycle_results
                  WHERE analysis_cycle_results.cycle_id = analysis_cycles.id
                    AND analysis_cycle_results.stage = ?
                    AND analysis_cycle_results.status = 'completed'
              )
            """,
            (_TERMINAL_COMPLETION_STAGE,),
        )
        if owns_connection:
            db.commit()
    finally:
        if owns_connection:
            db.close()


def _row_to_cycle(row) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    try:
        item["stages"] = json.loads(item.pop("stages_json") or "{}")
    except (TypeError, ValueError):
        item["stages"] = {}
    return item


def create_analysis_cycle(
    strategy_id: str,
    trading_env: str,
    *,
    mode: str = "analysis",
    account_captured_at: str | None = None,
) -> dict:
    now = datetime.now(KST).isoformat()
    cycle_id = f"{trading_env}-{now[:10].replace('-', '')}-{uuid.uuid4().hex[:12]}"
    with _connect() as conn:
        init_analysis_cycle_tables(conn)
        conn.execute(
            """
            INSERT INTO analysis_cycles (
                id, strategy_id, trading_env, mode, status, created_at,
                updated_at, account_captured_at, stages_json
            ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, '{}')
            """,
            (cycle_id, strategy_id, trading_env, mode, now, now, account_captured_at),
        )
        conn.commit()
    return get_analysis_cycle(cycle_id)


def get_analysis_cycle(cycle_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM analysis_cycles WHERE id = ?",
            (cycle_id,),
        ).fetchone()
    return _row_to_cycle(row)


def get_latest_analysis_cycle(strategy_id: str, trading_env: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM analysis_cycles
            WHERE strategy_id = ? AND trading_env = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (strategy_id, trading_env),
        ).fetchone()
    return _row_to_cycle(row)


def record_analysis_cycle_stage(
    cycle_id: str,
    stage: str,
    *,
    status: str = "completed",
    details: dict | None = None,
    payload: dict | None = None,
) -> dict | None:
    now = datetime.now(KST).isoformat()
    with _cycle_write_locks_guard:
        lock = _cycle_write_locks.setdefault(cycle_id, threading.Lock())
    with lock:
        with _connect() as conn:
            cycle_row = conn.execute(
                "SELECT status, stages_json FROM analysis_cycles WHERE id = ?",
                (cycle_id,),
            ).fetchone()
            if cycle_row is None:
                return None
            current_status = str(cycle_row["status"])
            try:
                stages = json.loads(cycle_row["stages_json"] or "{}")
            except (TypeError, ValueError):
                stages = {}
            stages[stage] = {
                "status": status,
                "recorded_at": now,
                **(details or {}),
            }
            conn.execute(
                """
                INSERT INTO analysis_cycle_results (
                    cycle_id, stage, status, payload, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id, stage) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    recorded_at = excluded.recorded_at
                """,
                (
                    cycle_id,
                    stage,
                    status,
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                    now,
                ),
            )
            result_rows = conn.execute(
                "SELECT stage, status FROM analysis_cycle_results WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchall()
            result_statuses = {str(row["stage"]): str(row["status"]) for row in result_rows}
            if current_status == "failed" or any(value == "failed" for value in result_statuses.values()):
                next_status = "failed"
            elif result_statuses.get(_TERMINAL_COMPLETION_STAGE) == "completed":
                next_status = "completed"
            else:
                next_status = "running"
            conn.execute(
                """
                UPDATE analysis_cycles
                SET stages_json = ?, status = ?, updated_at = ?,
                    account_captured_at = CASE
                        WHEN ? = 'account_balance' THEN ?
                        ELSE account_captured_at
                    END
                WHERE id = ?
                """,
                (json.dumps(stages, ensure_ascii=False), next_status, now, stage, now, cycle_id),
            )
            conn.commit()
    return get_analysis_cycle(cycle_id)


def get_analysis_cycle_stage(cycle_id: str, stage: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT cycle_id, stage, status, payload, recorded_at
            FROM analysis_cycle_results
            WHERE cycle_id = ? AND stage = ?
            """,
            (cycle_id, stage),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["payload"] = json.loads(item.get("payload") or "null")
    except (TypeError, ValueError):
        item["payload"] = None
    return item


def set_analysis_cycle_status(cycle_id: str, status: str) -> dict | None:
    now = datetime.now(KST).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE analysis_cycles SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, cycle_id),
        )
        conn.commit()
    return get_analysis_cycle(cycle_id)


__all__ = [
    "create_analysis_cycle",
    "get_analysis_cycle",
    "get_latest_analysis_cycle",
    "get_analysis_cycle_stage",
    "init_analysis_cycle_tables",
    "record_analysis_cycle_stage",
    "set_analysis_cycle_status",
]
