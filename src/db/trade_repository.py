from __future__ import annotations

import functools
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import config
from src.utils.logger import logger
from src.db import repository as _root

KST = timezone(timedelta(hours=9))

def connect_db():
    return _root.connect_db()

def init_db() -> None:
    _root.init_db()


def init_trade_sync_runs() -> None:
    """Create durable broker-sync run storage without depending on runtime files."""
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_sync_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                ok INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def save_trade_sync_run(run: dict) -> dict:
    init_trade_sync_runs()
    run_id = str(run.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("trade sync run_id is required")
    now = datetime.now(KST).isoformat()
    payload = json.dumps(run, ensure_ascii=False, default=str)
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO trade_sync_runs (
                run_id, started_at, completed_at, status, ok, payload, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                started_at=excluded.started_at,
                completed_at=excluded.completed_at,
                status=excluded.status,
                ok=excluded.ok,
                payload=excluded.payload,
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                str(run.get("started_at") or now),
                run.get("completed_at"),
                str(run.get("status") or ("completed" if run.get("ok") else "failed")),
                1 if run.get("ok") else 0,
                payload,
                now,
            ),
        )
        old_rows = conn.execute(
            "SELECT run_id FROM trade_sync_runs ORDER BY started_at DESC LIMIT 1000 OFFSET 50"
        ).fetchall()
        for row in old_rows:
            conn.execute("DELETE FROM trade_sync_runs WHERE run_id=?", (str(row[0]),))
    return run


def list_trade_sync_runs(limit: int = 50) -> list[dict]:
    init_trade_sync_runs()
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT payload FROM trade_sync_runs ORDER BY started_at DESC LIMIT ?",
            (max(1, min(int(limit or 50), 200)),),
        ).fetchall()
    runs = []
    for row in rows:
        try:
            payload = json.loads(row[0])
            if isinstance(payload, dict):
                runs.append(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return runs


def get_trade_sync_run(run_id: str) -> dict | None:
    init_trade_sync_runs()
    with connect_db() as conn:
        row = conn.execute(
            "SELECT payload FROM trade_sync_runs WHERE run_id=?",
            (str(run_id),),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
        return payload if isinstance(payload, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

def _extract_broker_order_id(broker_result: dict | None) -> str:
    if not isinstance(broker_result, dict):
        return ""
    output = broker_result.get("output")
    if isinstance(output, dict):
        for key in ("ODNO", "odno", "order_no", "ord_no"):
            value = output.get(key)
            if value:
                return str(value)
    for key in ("ODNO", "odno", "order_no", "ord_no"):
        value = broker_result.get(key)
        if value:
            return str(value)
    return ""


def save_trade(
    symbol: str,
    name: str,
    action: str,
    qty: int,
    price: int,
    reason: str,
    ok: bool,
    order_submission_enabled: bool,
    *,
    broker_result: dict | None = None,
    order_status: str | None = None,
    response_msg: str | None = None,
    broker_order_id: str | None = None,
    filled_qty: int | None = None,
    filled_price: int | None = None,
    pre_order_qty: int | None = None,
    strategy_id: str | None = None,
    strategy_version: int | None = None,
    profile_hash: str | None = None,
    source_approval_id: int | None = None,
) -> None:
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    broker_order_id = broker_order_id if broker_order_id is not None else _extract_broker_order_id(broker_result)
    order_status = order_status or ("submitted" if ok and order_submission_enabled else "simulated" if ok else "failed")
    filled_qty = qty if filled_qty is None and order_status in {"filled", "simulated"} else int(filled_qty or 0)
    filled_price = price if filled_price is None and filled_qty > 0 else int(filled_price or 0)
    if response_msg is None and isinstance(broker_result, dict):
        response_msg = str(broker_result.get("msg1", ""))
    response_msg = response_msg or ""
    pre_order_qty = int(pre_order_qty or 0)
    broker_result_json = json.dumps(broker_result or {}, ensure_ascii=False)
    try:
        init_db()
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO trades (
                    ts, symbol, name, action, qty, price, reason, ok, env, dry_run,
                    broker_order_id, order_status, filled_qty, filled_price, pre_order_qty, response_msg, broker_result,
                    strategy_id, strategy_version, profile_hash, source_approval_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    symbol,
                    name,
                    action,
                    qty,
                    price,
                    reason,
                    int(ok),
                    config.trading_env,
                    int(not order_submission_enabled),
                    broker_order_id,
                    order_status,
                    filled_qty,
                    filled_price,
                    pre_order_qty,
                    response_msg,
                    broker_result_json,
                    strategy_id,
                    strategy_version,
                    profile_hash,
                    source_approval_id,
                ),
            )
            
            # Export to JSON for cloud sync
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT ts, symbol, name, action, qty, price, reason, ok, env, dry_run,
                       broker_order_id, order_status, filled_qty, filled_price, pre_order_qty, response_msg, broker_result,
                       strategy_id, strategy_version, profile_hash, source_approval_id
                FROM trades ORDER BY ts ASC
                """
            ).fetchall()
            trades = [dict(row) for row in rows]
            
        # Use data/trades.json for GitHub Actions
        data_json_path = Path("data/trades.json")
        data_json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(data_json_path, "w", encoding="utf-8") as f:
            json.dump(trades, f, ensure_ascii=False, indent=2)

        logger.info(
            "[TRADE_CREATE] "
            f"symbol={symbol} name={name} action={action} qty={int(qty or 0)} "
            f"price={int(price or 0)} ok={bool(ok)} status={order_status} "
            f"filled_qty={filled_qty} filled_price={filled_price} "
            f"broker_order_id={broker_order_id or '-'} strategy_id={strategy_id or '-'} "
            f"response={response_msg or '-'}"
        )
            
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to save trade history: {e}")


def update_trade_order_status(
    broker_order_id: str,
    *,
    trade_id: int | None = None,
    order_status: str,
    filled_qty: int = 0,
    filled_price: int = 0,
    response_msg: str = "",
    broker_result: dict | None = None,
) -> int:
    if not broker_order_id:
        return 0
    init_db()
    broker_result_json = json.dumps(
        broker_result or {},
        ensure_ascii=False,
        sort_keys=True,
    )
    with connect_db() as conn:
        where_sql = "id = ?" if trade_id is not None else "broker_order_id = ?"
        where_value = int(trade_id) if trade_id is not None else broker_order_id
        existing = conn.execute(
            f"""
            SELECT id, symbol, action, qty, order_status,
                   filled_qty, filled_price, response_msg, broker_result
            FROM trades
            WHERE {where_sql}
            ORDER BY id DESC
            LIMIT 1
            """,
            (where_value,),
        ).fetchone()
        if existing is None:
            return 0
        current = tuple(existing)
        requested_state = (
            str(order_status or ""),
            int(filled_qty or 0),
            int(filled_price or 0),
            str(response_msg or ""),
            broker_result_json,
        )
        current_broker_result = str(current[8] or "{}")
        try:
            current_broker_result = json.dumps(
                json.loads(current_broker_result),
                ensure_ascii=False,
                sort_keys=True,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        current_state = (
            str(current[4] or ""),
            int(current[5] or 0),
            int(current[6] or 0),
            str(current[7] or ""),
            current_broker_result,
        )
        if current_state == requested_state:
            return 0
        cursor = conn.execute(
            f"""
            UPDATE trades
            SET order_status = ?,
                filled_qty = ?,
                filled_price = ?,
                response_msg = ?,
                broker_result = ?
            WHERE {where_sql}
            """,
            (
                order_status,
                int(filled_qty or 0),
                int(filled_price or 0),
                response_msg,
                broker_result_json,
                where_value,
            ),
        )
        updated_count = int(cursor.rowcount)

    return updated_count


def list_local_trade_cleanup_candidates(limit: int = 200) -> list[dict]:
    """Return unresolved local rows that an operator may remove after review."""
    safe_limit = max(1, min(int(limit or 200), 1000))
    init_db()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, ts, symbol, name, action, qty, price, broker_order_id,
                   order_status, filled_qty, filled_price, response_msg, strategy_id
            FROM trades
            WHERE COALESCE(order_status, '') IN ('failed', 'submitted', 'broker_unknown')
              AND COALESCE(filled_qty, 0) = 0
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    candidates = []
    for row in rows:
        item = dict(row)
        status = str(item.get("order_status") or "")
        symbol = str(item.get("symbol") or "")
        if status == "failed" and not item.get("broker_order_id"):
            cleanup_reason = "broker rejected before order number was issued"
            risk = "low"
        elif symbol.startswith("Q") and symbol[1:].isdigit():
            cleanup_reason = f"symbol alias requires reconciliation with {symbol[1:]}"
            risk = "high"
        else:
            cleanup_reason = "broker order exists but local status is unresolved"
            risk = "high"
        item["cleanup_reason"] = cleanup_reason
        item["cleanup_risk"] = risk
        candidates.append(item)
    return candidates


def delete_local_trade_record(trade_id: int) -> dict:
    """Delete one unresolved local record, never a filled/open/partial order."""
    init_db()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (int(trade_id),)).fetchone()
        if row is None:
            raise LookupError(f"local trade {trade_id} was not found")
        item = dict(row)
        status = str(item.get("order_status") or "")
        filled_qty = int(item.get("filled_qty") or 0)
        if filled_qty > 0 or status not in {"failed", "submitted", "broker_unknown"}:
            raise ValueError(
                f"local trade {trade_id} is protected (status={status or '-'}, filled_qty={filled_qty})"
            )
        conn.execute("DELETE FROM trades WHERE id = ?", (int(trade_id),))

    logger.warning(
        "[TRADE_LOCAL_DELETE] "
        f"trade_id={trade_id} symbol={item.get('symbol') or '-'} "
        f"action={item.get('action') or '-'} qty={int(item.get('qty') or 0)} "
        f"status={status} broker_order_id={item.get('broker_order_id') or '-'} "
        "scope=local_only"
    )
    return item

def save_decision_log(symbol: str, name: str, action: str, qty: int, price: int, reason: str, indicators: dict, approved: bool) -> None:
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO decision_logs (ts, symbol, name, action, qty, price, reason, indicators, approved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, symbol, name, action, qty, price, reason, json.dumps(indicators, ensure_ascii=False), int(approved))
            )
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to save decision log: {e}")


def save_scanned_candidate(
    symbol: str,
    name: str,
    score: int,
    reasons: list | str,
    price: int,
    env: str,
    indicators: dict | None = None,
    strategy: dict | None = None,
    ranker_model: str | None = None,
    optimizer: str | None = None,
    scoring: dict | None = None,
) -> int | None:
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    reasons_str = ",".join(reasons) if isinstance(reasons, list) else str(reasons)
    indicators = indicators or {}
    
    rsi = indicators.get("rsi")
    rsi2 = indicators.get("rsi2")
    macd_hist = indicators.get("macd_hist")
    sma20 = indicators.get("sma20")
    sma60 = indicators.get("sma60")
    strategy = strategy or {}
    scoring = scoring or {}
    top_features = scoring.get("top_features")
    top_features_json = (
        json.dumps(top_features, ensure_ascii=False)
        if isinstance(top_features, (list, dict))
        else None
    )
    
    try:
        init_db()
        with connect_db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO scanned_candidates (
                    scanned_at, symbol, name, score, reasons, price, env,
                    rsi, rsi2, macd_hist, sma20, sma60,
                    strategy_id, strategy_version, profile_hash, ranker_model, optimizer,
                    rule_score, ml_score, final_score, ai_model_status,
                    ai_fallback_reason, top_features_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts, symbol, name, score, reasons_str, price, env,
                    rsi, rsi2, macd_hist, sma20, sma60,
                    strategy.get("id"),
                    strategy.get("strategy_version"),
                    strategy.get("profile_hash"),
                    ranker_model,
                    optimizer,
                    scoring.get("rule_score"),
                    scoring.get("ml_score"),
                    scoring.get("final_score"),
                    scoring.get("ai_model_status"),
                    scoring.get("ai_fallback_reason"),
                    top_features_json,
                )
            )
            return int(cursor.lastrowid)
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to save scanned candidate: {e}")
    return None


def get_scanned_candidates_history(
    limit: int = 100,
    days: int = 30,
    strategy_id: str | None = None,
) -> list[dict]:
    init_db()
    since_date = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            if strategy_id:
                rows = conn.execute(
                    """
                    SELECT * FROM scanned_candidates
                    WHERE scanned_at >= ? AND strategy_id = ?
                    ORDER BY scanned_at DESC
                    LIMIT ?
                    """,
                    (since_date, strategy_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM scanned_candidates
                    WHERE scanned_at >= ?
                    ORDER BY scanned_at DESC
                    LIMIT ?
                    """,
                    (since_date, limit),
                ).fetchall()
            return [dict(row) for row in rows]
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to fetch scanned candidates history: {e}")
        return []


def get_latest_scanned_candidates(strategy_id: str | None = None) -> list[dict]:
    init_db()
    try:
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            if strategy_id:
                row = conn.execute(
                    """
                    SELECT scanned_at FROM scanned_candidates
                    WHERE strategy_id = ?
                    ORDER BY scanned_at DESC
                    LIMIT 1
                    """,
                    (strategy_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT scanned_at FROM scanned_candidates ORDER BY scanned_at DESC LIMIT 1"
                ).fetchone()
            if not row:
                return []
            latest_time = row["scanned_at"]
            if strategy_id:
                rows = conn.execute(
                    """
                    SELECT * FROM scanned_candidates
                    WHERE scanned_at = ? AND strategy_id = ?
                    ORDER BY score DESC, symbol ASC
                    """,
                    (latest_time, strategy_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM scanned_candidates
                    WHERE scanned_at = ?
                    ORDER BY score DESC, symbol ASC
                    """,
                    (latest_time,),
                ).fetchall()
            return [dict(row) for row in rows]
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to fetch latest scanned candidates: {e}")
        return []


def delete_scanned_candidate(candidate_id: int) -> int:
    init_db()
    try:
        with connect_db() as conn:
            cursor = conn.execute(
                "DELETE FROM scanned_candidates WHERE id = ?",
                (candidate_id,)
            )
            return int(cursor.rowcount)
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to delete scanned candidate: {e}")
        return 0


def _candidate_date(scanned_at: str) -> str:
    return str(scanned_at or "")[:10]


def _chart_close_on_or_after(conn: DBWrapper, symbol: str, date_text: str) -> tuple[str, float] | None:
    row = conn.execute(
        """
        SELECT date, close
        FROM daily_charts
        WHERE symbol = ?
          AND date >= ?
          AND close > 0
        ORDER BY date ASC
        LIMIT 1
        """,
        (symbol, date_text),
    ).fetchone()
    if not row:
        return None
    return str(row[0]), float(row[1])


def _target_date(date_text: str, days: int) -> str:
    return (datetime.fromisoformat(date_text) + timedelta(days=days)).strftime("%Y-%m-%d")


def refresh_scanned_candidate_forward_returns(
    *,
    days: tuple[int, ...] = (1, 5, 20),
    limit: int = 500,
) -> dict:
    init_db()
    supported_days = tuple(day for day in days if day in {1, 5, 20})
    if not supported_days:
        return {"ok": True, "checked_count": 0, "updated_count": 0, "days": []}

    null_checks = " OR ".join(f"forward_return_{day}d IS NULL" for day in supported_days)
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, scanned_at, symbol, price
            FROM scanned_candidates
            WHERE ({null_checks})
            ORDER BY scanned_at ASC
            LIMIT ?
            """,
            (max(1, min(int(limit or 500), 5000)),),
        ).fetchall()

        updated_count = 0
        skipped_count = 0
        now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        for row in rows:
            item = dict(row)
            scanned_date = _candidate_date(item.get("scanned_at", ""))
            symbol = str(item.get("symbol") or "")
            if not scanned_date or not symbol:
                skipped_count += 1
                continue

            base = _chart_close_on_or_after(conn, symbol, scanned_date)
            if base is None:
                skipped_count += 1
                continue
            _, base_close = base
            if base_close <= 0:
                skipped_count += 1
                continue

            values: dict[str, float] = {}
            for day in supported_days:
                target = _chart_close_on_or_after(conn, symbol, _target_date(scanned_date, day))
                if target is None:
                    continue
                _, target_close = target
                values[f"forward_return_{day}d"] = round(((target_close - base_close) / base_close) * 100, 4)

            if not values:
                skipped_count += 1
                continue

            assignments = ", ".join(f"{key} = ?" for key in values)
            params = [*values.values(), now, int(item["id"])]
            cursor = conn.execute(
                f"""
                UPDATE scanned_candidates
                SET {assignments},
                    return_updated_at = ?
                WHERE id = ?
                """,
                tuple(params),
            )
            updated_count += int(cursor.rowcount)

    return {
        "ok": True,
        "checked_count": len(rows),
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "days": list(supported_days),
    }
__all__ = ['KST', '_extract_broker_order_id', 'save_trade', 'update_trade_order_status', 'list_local_trade_cleanup_candidates', 'delete_local_trade_record', 'save_decision_log', 'save_scanned_candidate', 'get_scanned_candidates_history', 'get_latest_scanned_candidates', 'delete_scanned_candidate', '_candidate_date', '_chart_close_on_or_after', '_target_date', 'refresh_scanned_candidate_forward_returns']
