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
    broker_result_json = json.dumps(broker_result or {}, ensure_ascii=False)
    with connect_db() as conn:
        where_sql = "id = ?" if trade_id is not None else "broker_order_id = ?"
        where_value = int(trade_id) if trade_id is not None else broker_order_id
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
        return int(cursor.rowcount)

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


def get_scanned_candidates_history(limit: int = 100, days: int = 30) -> list[dict]:
    init_db()
    since_date = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM scanned_candidates
                WHERE scanned_at >= ?
                ORDER BY scanned_at DESC
                LIMIT ?
                """,
                (since_date, limit)
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
__all__ = ['KST', '_extract_broker_order_id', 'save_trade', 'update_trade_order_status', 'save_decision_log', 'save_scanned_candidate', 'get_scanned_candidates_history', 'get_latest_scanned_candidates', 'delete_scanned_candidate', '_candidate_date', '_chart_close_on_or_after', '_target_date', 'refresh_scanned_candidate_forward_returns']
