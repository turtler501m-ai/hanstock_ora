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

def save_scheduler_result(mode: str, recorded_at: str, result: dict) -> None:
    try:
        init_db()
        
        # Robust conversion of sets/unserializable types to list/str
        def convert_unserializable(obj):
            if isinstance(obj, dict):
                return {k: convert_unserializable(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_unserializable(x) for x in obj]
            elif isinstance(obj, set):
                try:
                    return [convert_unserializable(x) for x in sorted(list(obj))]
                except (sqlite3.Error, OSError, ValueError, TypeError):
                    return [convert_unserializable(x) for x in list(obj)]
            return obj

        cleaned_result = convert_unserializable(result)
        strategy_id = cleaned_result.get("strategy_id") or cleaned_result.get("force_strategy_id") or "seven_split"
        
        with connect_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scheduler_results (recorded_at, mode, result, strategy_id) VALUES (?, ?, ?, ?)",
                (recorded_at, mode, json.dumps(cleaned_result, ensure_ascii=False, default=str), strategy_id)
            )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to save scheduler result to DB: {e}")


def load_latest_scheduler_result() -> dict | None:
    try:
        init_db()
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            c = conn.execute(
                "SELECT * FROM scheduler_results ORDER BY recorded_at DESC LIMIT 1"
            )
            row = c.fetchone()
            if row is not None:
                res_data = json.loads(row["result"])
                recorded_at_str = row["recorded_at"]
                time_part = recorded_at_str.replace("T", " ").split(" ")[1][:5]
                
                # Enrich with time and round
                if "results" in res_data:
                    for item in res_data["results"]:
                        item["time"] = time_part
                        item["round"] = 1
                if "auto_approved" in res_data:
                    for item in res_data["auto_approved"]:
                        item["time"] = time_part
                        item["round"] = 1
                if "auto_approval_errors" in res_data:
                    for item in res_data["auto_approval_errors"]:
                        item["time"] = time_part
                        item["round"] = 1

                return {
                    "mode": row["mode"],
                    "recorded_at": row["recorded_at"],
                    "result": res_data
                }
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to load scheduler result from DB: {e}")
    return None
 
 
def load_recent_scheduler_results(days: int = 30) -> dict | None:
    try:
        init_db()
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            
            days = max(1, int(days or 30))
            cutoff_str = (datetime.now(KST) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
            
            c = conn.execute(
                "SELECT * FROM scheduler_results WHERE substr(recorded_at, 1, 10) >= ? ORDER BY replace(recorded_at, 'T', ' ') ASC",
                (cutoff_str,)
            )
            rows = c.fetchall()
            if not rows:
                return None
                
            merged_results = []
            merged_approved = []
            merged_approval_errors = []
            merged_run_errors = []
            
            latest_recorded_at = rows[-1]["recorded_at"]
            latest_mode = rows[-1]["mode"]
            
            for idx, row in enumerate(rows):
                try:
                    res_data = json.loads(row["result"])
                except (sqlite3.Error, OSError, ValueError, TypeError):
                    continue
                
                round_num = idx + 1
                run_strategy_id = str(res_data.get("strategy_id") or row["strategy_id"] or "seven_split")
                recorded_at_str = row["recorded_at"]
                normalized_recorded_at = recorded_at_str.replace("T", " ")
                date_part = normalized_recorded_at[:10]
                time_part = normalized_recorded_at.split(" ")[1][:5]
                display_time = f"{date_part[5:]} {time_part}"
                
                # plans / results
                for item in res_data.get("results", []):
                    item_copy = dict(item)
                    item_copy.setdefault("strategy_id", run_strategy_id)
                    item_copy["time"] = display_time
                    item_copy["run_date"] = date_part
                    item_copy["run_recorded_at"] = recorded_at_str
                    item_copy["round"] = round_num
                    if "reason" in item_copy and item_copy["reason"]:
                        item_copy["reason"] = f"[{display_time}] {item_copy['reason']}"
                    else:
                        item_copy["reason"] = f"[{display_time}] 스케쥴 분석 결과"
                    merged_results.append(item_copy)
                    
                # approved / auto_approved
                for item in res_data.get("auto_approved", []):
                    item_copy = dict(item)
                    item_copy.setdefault("strategy_id", run_strategy_id)
                    item_copy["time"] = display_time
                    item_copy["run_date"] = date_part
                    item_copy["run_recorded_at"] = recorded_at_str
                    item_copy["round"] = round_num
                    if "response_msg" in item_copy and item_copy["response_msg"]:
                        item_copy["response_msg"] = f"[{display_time}] {item_copy['response_msg']}"
                    else:
                        item_copy["response_msg"] = f"[{display_time}] 정상 처리"
                    merged_approved.append(item_copy)
                    
                # approval errors
                for item in res_data.get("auto_approval_errors", []):
                    item_copy = dict(item)
                    item_copy.setdefault("strategy_id", run_strategy_id)
                    item_copy["time"] = display_time
                    item_copy["run_date"] = date_part
                    item_copy["run_recorded_at"] = recorded_at_str
                    item_copy["round"] = round_num
                    if "message" in item_copy and item_copy["message"]:
                        item_copy["message"] = f"[{display_time}] {item_copy['message']}"
                    else:
                        item_copy["message"] = f"[{display_time}] 오류 발생"
                    merged_approval_errors.append(item_copy)
                    
                # run errors
                errors = res_data.get("errors", []) or res_data.get("retry_errors", [])
                if isinstance(errors, list):
                    for err in errors:
                        merged_run_errors.append(f"[{display_time}] {err}")
                elif errors:
                    merged_run_errors.append(f"[{display_time}] {errors}")
                    
            return {
                "mode": latest_mode,
                "recorded_at": latest_recorded_at,
                "summary_label": f"최근 {days}일 전체 집계",
                "range_days": days,
                "result": {
                    "results": merged_results,
                    "auto_approved": merged_approved,
                    "auto_approval_errors": merged_approval_errors,
                    "errors": merged_run_errors,
                    "status": "success" if not merged_approval_errors and not merged_run_errors else "failed",
                    "ok": True
                }
            }
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to load recent scheduler results from DB: {e}")
    return None


def load_today_scheduler_results() -> dict | None:
    return load_recent_scheduler_results(days=1)


def load_auto_approval_state() -> bool:
    try:
        init_db()
        with connect_db() as conn:
            c = conn.execute("SELECT value FROM auto_approval WHERE key = 'enabled'")
            row = c.fetchone()
            if row is not None:
                return row[0] == "1"
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to load auto approval state: {e}")
    return False


def save_auto_approval_state(enabled: bool) -> None:
    try:
        init_db()
        with connect_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO auto_approval (key, value) VALUES ('enabled', ?)",
                ("1" if enabled else "0",)
            )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to save auto approval state: {e}")


def save_account_snapshot(
    account_key: str,
    trading_env: str,
    kind: str,
    payload: dict,
    captured_at: str | None = None,
) -> None:
    """대시보드 라이브 데이터의 마지막 성공본을 DB에 저장(write-through)한다.

    account_key/trading_env/kind 조합당 1행만 유지하며 항상 최신본으로 덮어쓴다.
    """
    try:
        init_db()
        captured_at = captured_at or datetime.now(KST).isoformat()
        with connect_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO account_snapshots
                    (account_key, trading_env, kind, payload, captured_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    account_key,
                    trading_env,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    captured_at,
                ),
            )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to save account snapshot ({kind}): {e}")


def load_account_snapshot(account_key: str, trading_env: str, kind: str) -> dict | None:
    """저장된 마지막 스냅샷을 반환한다. 없거나 오류면 None."""
    try:
        init_db()
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            c = conn.execute(
                """
                SELECT payload, captured_at FROM account_snapshots
                WHERE account_key = ? AND trading_env = ? AND kind = ?
                """,
                (account_key, trading_env, kind),
            )
            row = c.fetchone()
            if row is not None:
                return {
                    "payload": json.loads(row["payload"]),
                    "captured_at": row["captured_at"],
                }
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to load account snapshot ({kind}): {e}")
    return None


def delete_account_snapshot(account_key: str, trading_env: str, kind: str) -> None:
    """지정 스냅샷을 삭제한다(강제 갱신용)."""
    try:
        init_db()
        with connect_db() as conn:
            conn.execute(
                """
                DELETE FROM account_snapshots
                WHERE account_key = ? AND trading_env = ? AND kind = ?
                """,
                (account_key, trading_env, kind),
            )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to delete account snapshot ({kind}): {e}")


# ---------------------------------------------------------------------------
# 전략 스케쥴 (대시보드 등록/제어 + VM 디스패처가 읽어 실행)
# ---------------------------------------------------------------------------
_SCHEDULE_DEFAULTS = {
    "enabled": 0,
    "interval_minutes": 15,
    "start_hm": "0900",
    "end_hm": "1530",
    "weekdays": "1-5",
    "mode": "execute",
    "auto_approve": 1,
    "last_run_at": None,
}


def _schedule_row_to_dict(row) -> dict:
    d = dict(row)
    d["enabled"] = bool(d.get("enabled"))
    d["auto_approve"] = bool(d.get("auto_approve"))
    d["interval_minutes"] = int(d.get("interval_minutes") or 15)
    return d


def load_strategy_schedule(strategy_id: str) -> dict:
    """전략 스케쥴을 반환한다. 없으면 기본값(enabled=False)으로 채운다."""
    init_db()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM strategy_schedules WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()
    if row is None:
        return {"strategy_id": strategy_id, **_SCHEDULE_DEFAULTS}
    return _schedule_row_to_dict(row)


def list_strategy_schedules(enabled_only: bool = False) -> list[dict]:
    init_db()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM strategy_schedules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        rows = conn.execute(sql).fetchall()
    return [_schedule_row_to_dict(r) for r in rows]


def save_strategy_schedule(strategy_id: str, **fields) -> dict:
    """전략 스케쥴을 upsert 한다. 전달된 필드만 갱신."""
    init_db()
    current = load_strategy_schedule(strategy_id)
    merged = {**current, **{k: v for k, v in fields.items() if v is not None}}
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    
    # 콜론 제거를 통한 시간 포맷 방어
    start_hm = str(merged.get("start_hm") or "0900").replace(":", "")
    end_hm = str(merged.get("end_hm") or "1530").replace(":", "")
    
    with connect_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO strategy_schedules
                (strategy_id, enabled, interval_minutes, start_hm, end_hm, weekdays,
                 mode, auto_approve, last_run_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                1 if merged.get("enabled") else 0,
                int(merged.get("interval_minutes") or 15),
                start_hm,
                end_hm,
                str(merged.get("weekdays") or "1-5"),
                str(merged.get("mode") or "execute"),
                1 if merged.get("auto_approve") else 0,
                merged.get("last_run_at"),
                now,
            ),
        )
        conn.commit()
    return load_strategy_schedule(strategy_id)


def mark_strategy_schedule_run(strategy_id: str, ts: str | None = None) -> None:
    init_db()
    ts = ts or datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with connect_db() as conn:
        conn.execute(
            "UPDATE strategy_schedules SET last_run_at = ? WHERE strategy_id = ?",
            (ts, strategy_id),
        )
        conn.commit()


def _weekday_matches(weekdays: str, dow: int) -> bool:
    """dow: 1(Mon)~7(Sun). weekdays 예: '1-5', '1,3,5', '6,7'."""
    for part in str(weekdays or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                lo, hi = (int(x) for x in part.split("-", 1))
                if lo <= dow <= hi:
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(part) == dow:
                    return True
            except ValueError:
                continue
    return False


def is_schedule_due(schedule: dict, now=None) -> bool:
    """현재 시각(KST)이 스케쥴 실행 윈도우 안이고, interval만큼 경과했으면 True."""
    if not schedule.get("enabled"):
        return False
    now = now or datetime.now(KST)
    dow = now.isoweekday()  # 1~7
    if not _weekday_matches(schedule.get("weekdays", "1-5"), dow):
        return False
    hm = now.strftime("%H%M")
    
    # 콜론 제거를 통해 대시보드 저장 포맷 유연화
    start_hm = str(schedule.get("start_hm") or "0900").replace(":", "")
    end_hm = str(schedule.get("end_hm") or "1530").replace(":", "")
    
    if not (start_hm <= hm <= end_hm):
        return False
    last = schedule.get("last_run_at")
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except ValueError:
        return True
    interval = int(schedule.get("interval_minutes") or 15)
    return (now - last_dt).total_seconds() >= interval * 60 - 1


# ---------------------------------------------------------------------------
# 전략 전용 유니버스(스캔 대상 종목)
# ---------------------------------------------------------------------------
def load_strategy_universe(strategy_id: str) -> list[dict]:
    init_db()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT symbol, name, created_at FROM strategy_universe WHERE strategy_id = ? ORDER BY created_at ASC",
            (strategy_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def load_strategy_universe_symbols(strategy_id: str) -> list[str]:
    return [r["symbol"] for r in load_strategy_universe(strategy_id)]


def add_strategy_universe_symbol(strategy_id: str, symbol: str, name: str = "") -> None:
    init_db()
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with connect_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO strategy_universe (strategy_id, symbol, name, created_at)
            VALUES (?, ?, ?, COALESCE((SELECT created_at FROM strategy_universe WHERE strategy_id = ? AND symbol = ?), ?))
            """,
            (strategy_id, symbol, name, strategy_id, symbol, now),
        )
        conn.commit()


def remove_strategy_universe_symbol(strategy_id: str, symbol: str) -> int:
    init_db()
    with connect_db() as conn:
        cur = conn.execute(
            "DELETE FROM strategy_universe WHERE strategy_id = ? AND symbol = ?",
            (strategy_id, symbol),
        )
        conn.commit()
        return cur.rowcount or 0


def reconstruct_strategy_positions(strategy_id: str, env: str | None = None) -> list[dict]:
    """trades(strategy_id 태깅)로부터 해당 전략의 보유 포지션을 재구성한다."""
    init_db()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM trades WHERE strategy_id = ? AND ok = 1"
        params: list = [strategy_id]
        if env:
            sql += " AND env = ?"
            params.append(env)
        sql += " ORDER BY ts ASC"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    positions: dict[str, dict] = {}
    for t in rows:
        sym = t.get("symbol")
        if not sym:
            continue
        qty = int(t.get("qty") or 0)
        price = float(t.get("price") or 0)
        pos = positions.setdefault(sym, {"symbol": sym, "name": t.get("name", sym), "qty": 0, "avg_cost": 0.0, "realized_pnl": 0.0})
        pos["name"] = t.get("name", pos["name"])
        if t.get("action") == "buy":
            total_qty = pos["qty"] + qty
            total_cost = pos["qty"] * pos["avg_cost"] + qty * price
            pos["qty"] = total_qty
            pos["avg_cost"] = (total_cost / total_qty) if total_qty > 0 else 0.0
        elif t.get("action") == "sell":
            sell_qty = min(qty, pos["qty"])
            pos["realized_pnl"] += (price - pos["avg_cost"]) * sell_qty
            pos["qty"] -= sell_qty
            if pos["qty"] <= 0:
                pos["qty"] = 0
                pos["avg_cost"] = 0.0
    return [p for p in positions.values() if p["qty"] > 0 or p["realized_pnl"]]
__all__ = ['KST', 'save_scheduler_result', 'load_latest_scheduler_result', 'load_recent_scheduler_results', 'load_today_scheduler_results', 'load_auto_approval_state', 'save_auto_approval_state', 'save_account_snapshot', 'load_account_snapshot', 'delete_account_snapshot', '_SCHEDULE_DEFAULTS', '_schedule_row_to_dict', 'load_strategy_schedule', 'list_strategy_schedules', 'save_strategy_schedule', 'mark_strategy_schedule_run', '_weekday_matches', 'is_schedule_due', 'load_strategy_universe', 'load_strategy_universe_symbols', 'add_strategy_universe_symbol', 'remove_strategy_universe_symbol', 'reconstruct_strategy_positions']
