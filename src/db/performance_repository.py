from __future__ import annotations

import sqlite3
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

from src.db import repository as _root
from src.config import config


KST = timezone(timedelta(hours=9))
REVIEW_DECISIONS = {"monitor", "pause", "reduce", "increase", "retire"}


def account_scope_key() -> str:
    raw = f"{config.trading_env}:{config.kistock_account or 'unknown'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _scope_key() -> str:
    return account_scope_key()


def connect_db():
    return _root.connect_db()


def init_performance_tables(conn=None) -> None:
    owns_connection = conn is None
    db = conn or connect_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_performance_reviews_v2 (
                scope_key TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT 'monitor',
                note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL,
                PRIMARY KEY (scope_key, strategy_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS performance_daily_nav (
                scope_key TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                session_date TEXT NOT NULL,
                cash REAL NOT NULL,
                market_value REAL NOT NULL,
                nav REAL,
                external_flow REAL NOT NULL DEFAULT 0,
                buy_amount REAL NOT NULL DEFAULT 0,
                sell_amount REAL NOT NULL DEFAULT 0,
                daily_return_pct REAL,
                twr_index REAL,
                drawdown_pct REAL,
                mdd_pct REAL,
                kospi_twr_index REAL,
                kospi_drawdown_pct REAL,
                kosdaq_twr_index REAL,
                kosdaq_drawdown_pct REAL,
                quality_issues_json TEXT NOT NULL DEFAULT '[]',
                calc_version TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope_key, scope_type, strategy_id, session_date, calc_version)
            )
            """
        )
        for column in (
            "kospi_twr_index", "kospi_drawdown_pct",
            "kosdaq_twr_index", "kosdaq_drawdown_pct",
        ):
            if getattr(db, "is_pg", False):
                db.execute(f"ALTER TABLE performance_daily_nav ADD COLUMN IF NOT EXISTS {column} REAL")
            else:
                existing_columns = {
                    row[1] for row in db.execute("PRAGMA table_info(performance_daily_nav)").fetchall()
                }
                if column not in existing_columns:
                    db.execute(f"ALTER TABLE performance_daily_nav ADD COLUMN {column} REAL")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS performance_account_cashflows (
                scope_key TEXT NOT NULL,
                external_ref TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                amount REAL NOT NULL,
                kind TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                confirmed INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (scope_key, external_ref)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS performance_account_equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_key TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                session_date TEXT NOT NULL,
                total_equity REAL NOT NULL,
                cash REAL NOT NULL,
                stock_value REAL NOT NULL,
                source TEXT NOT NULL,
                raw_summary_hash TEXT NOT NULL,
                UNIQUE(scope_key, captured_at, raw_summary_hash)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_performance_review_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_key TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL
            )
            """
        )
        if owns_connection:
            db.commit()
    finally:
        if owns_connection:
            db.close()


def save_strategy_performance_review(strategy_id: str, decision: str, note: str = "") -> dict:
    strategy_id = str(strategy_id or "").strip()
    decision = str(decision or "").strip().lower()
    if not strategy_id:
        raise ValueError("strategy_id is required")
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"unsupported review decision: {decision}")
    reviewed_at = datetime.now(KST).isoformat()
    scope_key = _scope_key()
    init_performance_tables()
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO strategy_performance_reviews_v2
                (scope_key, strategy_id, decision, note, reviewed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope_key, strategy_id) DO UPDATE SET
                decision=excluded.decision,
                note=excluded.note,
                reviewed_at=excluded.reviewed_at
            """,
            (scope_key, strategy_id, decision, str(note or "").strip(), reviewed_at),
        )
        conn.execute(
            """
            INSERT INTO strategy_performance_review_events
                (scope_key, strategy_id, decision, note, reviewed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scope_key, strategy_id, decision, str(note or "").strip(), reviewed_at),
        )
    return get_strategy_performance_review(strategy_id) or {}


def get_strategy_performance_review(strategy_id: str) -> dict | None:
    init_performance_tables()
    scope_key = _scope_key()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM strategy_performance_reviews_v2 WHERE scope_key=? AND strategy_id=?",
            (scope_key, str(strategy_id)),
        ).fetchone()
    return dict(row) if row else None


def list_strategy_performance_reviews() -> list[dict]:
    init_performance_tables()
    scope_key = _scope_key()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM strategy_performance_reviews_v2 WHERE scope_key=? ORDER BY reviewed_at DESC",
            (scope_key,),
        ).fetchall()
    return [dict(row) for row in rows]


def replace_daily_nav(
    strategy_id: str,
    rows: list[dict],
    *,
    scope_type: str = "strategy",
    input_hash: str,
) -> int:
    """Idempotently persist reproducible finalized-session NAV observations."""
    if scope_type not in {"account", "strategy"}:
        raise ValueError("scope_type must be account or strategy")
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id or not input_hash:
        raise ValueError("strategy_id and input_hash are required")
    init_performance_tables()
    scope_key = account_scope_key()
    now = datetime.now(KST).isoformat()
    with connect_db() as conn:
        existing = conn.execute(
            """SELECT input_hash FROM performance_daily_nav
               WHERE scope_key=? AND scope_type=? AND strategy_id=? AND calc_version=?
               ORDER BY session_date DESC LIMIT 1""",
            (scope_key, scope_type, strategy_id, str(rows[0].get("calc_version") or "") if rows else ""),
        ).fetchone()
        if existing and str(existing[0]) == input_hash:
            return 0
        if rows:
            conn.execute(
                """DELETE FROM performance_daily_nav
                   WHERE scope_key=? AND scope_type=? AND strategy_id=? AND calc_version=?""",
                (scope_key, scope_type, strategy_id, str(rows[0].get("calc_version") or "")),
            )
        for row in rows:
            conn.execute(
                """
                INSERT INTO performance_daily_nav (
                    scope_key, scope_type, strategy_id, session_date, cash,
                    market_value, nav, external_flow, buy_amount, sell_amount,
                    daily_return_pct, twr_index, drawdown_pct, mdd_pct,
                    kospi_twr_index, kospi_drawdown_pct,
                    kosdaq_twr_index, kosdaq_drawdown_pct,
                    quality_issues_json, calc_version, input_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_key, scope_type, strategy_id, session_date, calc_version)
                DO UPDATE SET
                    cash=excluded.cash, market_value=excluded.market_value,
                    nav=excluded.nav, external_flow=excluded.external_flow,
                    buy_amount=excluded.buy_amount, sell_amount=excluded.sell_amount,
                    daily_return_pct=excluded.daily_return_pct,
                    twr_index=excluded.twr_index, drawdown_pct=excluded.drawdown_pct,
                    mdd_pct=excluded.mdd_pct,
                    kospi_twr_index=excluded.kospi_twr_index,
                    kospi_drawdown_pct=excluded.kospi_drawdown_pct,
                    kosdaq_twr_index=excluded.kosdaq_twr_index,
                    kosdaq_drawdown_pct=excluded.kosdaq_drawdown_pct,
                    quality_issues_json=excluded.quality_issues_json,
                    input_hash=excluded.input_hash, updated_at=excluded.updated_at
                """,
                (
                    scope_key, scope_type, strategy_id, row["session_date"],
                    float(row.get("cash") or 0), float(row.get("market_value") or 0),
                    row.get("nav"), float(row.get("external_flow") or 0),
                    float(row.get("buy_amount") or 0), float(row.get("sell_amount") or 0),
                    row.get("daily_return_pct"), row.get("twr_index"),
                    row.get("drawdown_pct"), row.get("mdd_pct"),
                    row.get("kospi_twr_index"), row.get("kospi_drawdown_pct"),
                    row.get("kosdaq_twr_index"), row.get("kosdaq_drawdown_pct"),
                    json.dumps(row.get("quality_issues") or [], ensure_ascii=False),
                    str(row.get("calc_version") or ""), input_hash, now,
                ),
            )
    return len(rows)


def list_daily_nav(strategy_id: str, *, scope_type: str = "strategy") -> list[dict]:
    init_performance_tables()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM performance_daily_nav
            WHERE scope_key=? AND scope_type=? AND strategy_id=?
              AND calc_version=(
                  SELECT calc_version FROM performance_daily_nav
                  WHERE scope_key=? AND scope_type=? AND strategy_id=?
                  ORDER BY updated_at DESC LIMIT 1
              )
            ORDER BY session_date
            """,
            (account_scope_key(), scope_type, str(strategy_id),
             account_scope_key(), scope_type, str(strategy_id)),
        ).fetchall()
    result = []
    for raw in rows:
        row = dict(raw)
        row["quality_issues"] = json.loads(row.pop("quality_issues_json") or "[]")
        result.append(row)
    return result


def record_account_equity_snapshot(
    *, total_equity: float, cash: float, stock_value: float,
    captured_at: str | None = None, source: str = "kis_balance",
    raw_summary_hash: str,
) -> dict:
    parsed_at = datetime.fromisoformat(captured_at) if captured_at else datetime.now(KST)
    if parsed_at.tzinfo is None:
        parsed_at = parsed_at.replace(tzinfo=KST)
    parsed_at = parsed_at.astimezone(KST).replace(
        minute=(parsed_at.minute // 15) * 15, second=0, microsecond=0
    )
    captured_at = parsed_at.isoformat()
    session_date = captured_at[:10]
    if float(total_equity) < 0 or float(cash) < 0 or float(stock_value) < 0:
        raise ValueError("account snapshot values must be non-negative")
    if not raw_summary_hash:
        raise ValueError("raw_summary_hash is required")
    init_performance_tables()
    scope_key = account_scope_key()
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO performance_account_equity_snapshots
                (scope_key, captured_at, session_date, total_equity, cash,
                 stock_value, source, raw_summary_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_key, captured_at, raw_summary_hash) DO NOTHING
            """,
            (scope_key, captured_at, session_date, float(total_equity), float(cash),
             float(stock_value), str(source), raw_summary_hash),
        )
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT * FROM performance_account_equity_snapshots
               WHERE scope_key=? AND captured_at=? AND raw_summary_hash=?""",
            (scope_key, captured_at, raw_summary_hash),
        ).fetchone()
    return dict(row)


def record_account_cashflow(
    *, external_ref: str, occurred_at: str, amount: float, kind: str,
    confirmed: bool = False, note: str = "", source: str = "manual",
) -> dict:
    external_ref = str(external_ref or "").strip()
    kind = str(kind or "").strip().lower()
    if not external_ref or not occurred_at or kind not in {"deposit", "withdrawal", "dividend", "interest", "other"}:
        raise ValueError("valid external_ref, occurred_at and kind are required")
    try:
        parsed_at = datetime.fromisoformat(str(occurred_at))
    except ValueError as exc:
        raise ValueError("occurred_at must be an ISO datetime") from exc
    if parsed_at.tzinfo is None:
        raise ValueError("occurred_at must include timezone")
    occurred_at = parsed_at.astimezone(KST).isoformat()
    value = float(amount)
    if not math.isfinite(value) or value == 0:
        raise ValueError("cashflow amount must be finite and non-zero")
    if kind == "withdrawal" and value > 0:
        value = -value
    if kind == "deposit" and value < 0:
        raise ValueError("deposit amount must be positive")
    init_performance_tables()
    scope_key = account_scope_key()
    created_at = datetime.now(KST).isoformat()
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO performance_account_cashflows
                (scope_key, external_ref, occurred_at, amount, kind, source,
                 confirmed, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_key, external_ref) DO UPDATE SET
                occurred_at=excluded.occurred_at, amount=excluded.amount,
                kind=excluded.kind, source=excluded.source,
                confirmed=excluded.confirmed, note=excluded.note
            """,
            (scope_key, external_ref, occurred_at, value, kind, source,
             int(bool(confirmed)), str(note or "").strip(), created_at),
        )
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM performance_account_cashflows WHERE scope_key=? AND external_ref=?",
            (scope_key, external_ref),
        ).fetchone()
    return dict(row)


def list_account_cashflows() -> list[dict]:
    init_performance_tables()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM performance_account_cashflows WHERE scope_key=? ORDER BY occurred_at",
            (account_scope_key(),),
        ).fetchall()
    return [dict(row) for row in rows]


def build_account_equity_performance() -> dict:
    """Calculate broker-equity TWR from finalized snapshots and confirmed flows."""
    init_performance_tables()
    scope_key = account_scope_key()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        snapshots = conn.execute(
            """SELECT * FROM performance_account_equity_snapshots
               WHERE scope_key=? ORDER BY captured_at""",
            (scope_key,),
        ).fetchall()
        flows = conn.execute(
            """SELECT * FROM performance_account_cashflows
               WHERE scope_key=? ORDER BY occurred_at""",
            (scope_key,),
        ).fetchall()
    latest_by_day: dict[str, dict] = {}
    for raw in snapshots:
        row = dict(raw)
        # Domestic market observations before 15:30 are not finalized EOD NAV.
        if str(row.get("captured_at") or "")[11:16] < "15:30":
            continue
        latest_by_day[str(row["session_date"])] = row
    confirmed_by_day: dict[str, float] = {}
    unresolved_dates = set()
    for raw in flows:
        row = dict(raw)
        day = str(row.get("occurred_at") or "")[:10]
        if bool(row.get("confirmed")):
            confirmed_by_day[day] = confirmed_by_day.get(day, 0.0) + float(row["amount"])
        else:
            unresolved_dates.add(day)

    observations = []
    previous_equity = None
    twr_index = 100.0
    peak = 100.0
    mdd = 0.0
    blocked = False
    for day, snapshot in sorted(latest_by_day.items()):
        flow = confirmed_by_day.get(day, 0.0)
        equity = float(snapshot["total_equity"])
        daily_return = None
        if previous_equity is not None:
            denominator = previous_equity + flow
            if denominator <= 0 or day in unresolved_dates or blocked:
                blocked = True
            else:
                daily_return = equity / denominator - 1
                twr_index *= 1 + daily_return
                peak = max(peak, twr_index)
                mdd = min(mdd, (twr_index / peak - 1) * 100)
        previous_equity = equity
        observations.append({
            "session_date": day,
            "total_equity": round(equity, 2),
            "external_flow": round(flow, 2),
            "daily_return_pct": round(daily_return * 100, 6) if daily_return is not None else None,
            "twr_index": None if blocked else round(twr_index, 6),
            "mdd_pct": None if blocked else round(mdd, 6),
        })
    available = len(observations) >= 2 and not blocked
    return {
        "available": available,
        "twr_pct": round(twr_index - 100, 2) if available else None,
        "max_drawdown_pct": round(mdd, 2) if available else None,
        "observations": len(observations),
        "started_at": observations[0]["session_date"] if observations else None,
        "ended_at": observations[-1]["session_date"] if observations else None,
        "unconfirmed_cashflow_dates": sorted(unresolved_dates),
        "daily": observations,
        "method": "broker_equity_twr_confirmed_cashflows",
    }


__all__ = [
    "REVIEW_DECISIONS",
    "init_performance_tables",
    "save_strategy_performance_review",
    "get_strategy_performance_review",
    "list_strategy_performance_reviews",
    "account_scope_key",
    "replace_daily_nav",
    "list_daily_nav",
    "record_account_equity_snapshot",
    "record_account_cashflow",
    "list_account_cashflows",
    "build_account_equity_performance",
]
