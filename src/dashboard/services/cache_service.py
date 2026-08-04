from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.db.scheduler_repository import (
    delete_account_snapshot,
    load_account_snapshot,
    save_account_snapshot,
)
from src.utils.logger import logger


def snapshot_read_through(
    kind: str,
    builder,
    *,
    ttl: int,
    env: str,
    account_key: str,
    now_fn,
    recoverable_errors: tuple[type[BaseException], ...],
):
    """Read a persisted snapshot, refresh it, and fall back to stale data."""
    from src.db.repository import load_account_snapshot, save_account_snapshot
    from src.online_access import is_online_access_blocked, require_online_access

    snapshot = None
    try:
        snapshot = load_account_snapshot(account_key, env, kind)
    except recoverable_errors:
        snapshot = None

    if snapshot is not None:
        captured_at = snapshot.get("captured_at", "")
        try:
            age = (now_fn() - datetime.fromisoformat(captured_at)).total_seconds()
        except (TypeError, ValueError):
            age = None
        if is_online_access_blocked() or (age is not None and age < ttl):
            payload = dict(snapshot["payload"])
            payload["_snapshot"] = {
                "stale": bool(is_online_access_blocked()),
                "captured_at": captured_at,
                "source": "db",
                **({"offline": True} if is_online_access_blocked() else {}),
            }
            return payload

    require_online_access(f"{kind} refresh")
    try:
        payload = builder()
        if not isinstance(payload, dict):
            return payload
        captured_at = now_fn().isoformat()
        try:
            save_account_snapshot(account_key, env, kind, payload, captured_at)
        except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as exc:
            logger.warning(f"Failed to persist {kind} snapshot: {exc}")
        result = dict(payload)
        result["_snapshot"] = {
            "stale": False, "captured_at": captured_at, "source": "live"
        }
        return result
    except recoverable_errors as exc:
        if snapshot is None:
            raise
        payload = dict(snapshot["payload"])
        payload["_snapshot"] = {
            "stale": True,
            "captured_at": snapshot.get("captured_at", ""),
            "source": "db",
            "error": f"{type(exc).__name__}: {exc}",
        }
        return payload


class DashboardCacheService:
    def __init__(
        self,
        balance_cache_path: Path,
        *,
        account_key_fn: Callable[[], str],
        trading_env_fn: Callable[[], str],
        captured_at_fn: Callable[[], str],
        derived_kinds: tuple[str, ...],
    ) -> None:
        self.balance_cache_path = balance_cache_path
        self.account_key_fn = account_key_fn
        self.trading_env_fn = trading_env_fn
        self.captured_at_fn = captured_at_fn
        self.derived_kinds = derived_kinds

    def save_balance(self, balance_data: dict) -> None:
        envelope = {
            "cached_at": self.captured_at_fn(),
            "trading_env": self.trading_env_fn(),
            "account_key": self.account_key_fn(),
            "data": balance_data,
        }
        self.balance_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.balance_cache_path.write_text(
            json.dumps(envelope, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            save_account_snapshot(
                envelope["account_key"],
                envelope["trading_env"],
                "balance",
                envelope,
                envelope["cached_at"],
            )
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(f"Failed to persist balance snapshot: {exc}")

    def clear_balance(self) -> None:
        try:
            self.balance_cache_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"Failed to remove balance cache: {exc}")
        for kind in self.derived_kinds:
            try:
                delete_account_snapshot(
                    self.account_key_fn(),
                    self.trading_env_fn(),
                    kind,
                )
            except (OSError, ValueError, TypeError) as exc:
                logger.warning(f"Failed to clear {kind} snapshot: {exc}")

    def balance_envelope_to_data(self, envelope) -> dict | None:
        if not isinstance(envelope, dict):
            return None
        if envelope.get("trading_env") != self.trading_env_fn():
            return None
        if envelope.get("account_key") != self.account_key_fn():
            return None
        data = envelope.get("data")
        if not isinstance(data, dict):
            return None
        result = dict(data)
        result["_cache"] = {
            "stale": True,
            "cached_at": envelope.get("cached_at", ""),
        }
        return result

    def load_balance(self) -> dict | None:
        if self.balance_cache_path.exists():
            try:
                envelope = json.loads(
                    self.balance_cache_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                logger.warning(f"Failed to read balance cache: {exc}")
            else:
                data = self.balance_envelope_to_data(envelope)
                if data is not None:
                    return data

        try:
            snapshot = load_account_snapshot(
                self.account_key_fn(),
                self.trading_env_fn(),
                "balance",
            )
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(f"Failed to load balance snapshot: {exc}")
            return None
        if snapshot is None:
            return None
        return self.balance_envelope_to_data(snapshot["payload"])


def _refresh_candidate_dependencies() -> None:
    from src.dashboard import core
    protected = {
        "_refresh_candidate_dependencies", "_candidate_public_override",
        "_candidate_strategy_cache_signature", "_get_candidate_cache_path",
        "_load_candidate_cache", "_candidate_snapshot_kind",
        "_candidate_envelope_to_result", "_save_candidate_cache",
    }
    globals().update({
        name: value for name, value in vars(core).items() if name not in protected
    })


def _candidate_public_override(name: str, current):
    import sys
    from src.dashboard import core
    module = sys.modules.get("src.dashboard")
    value = getattr(module, name, None) if module is not None else None
    wrapper = getattr(core, name, None)
    if value is not None and value is not current and value is not wrapper:
        return value
    return None


def _candidate_strategy_cache_signature(ranker: str) -> dict | None:
    try:
        from src.db.repository import load_ai_strategies

        strategy = next((item for item in load_ai_strategies() if item.get("id") == ranker), None)
    except DashboardOperationError:
        strategy = None
    if not strategy:
        return None
    return {
        "strategy_id": strategy.get("id"),
        "strategy_version": int(strategy.get("strategy_version") or 1),
        "profile_hash": strategy.get("profile_hash") or "",
    }


def _get_candidate_cache_path(ranker: str, optimizer: str):
    """전략·옵티마이저 조합별로 독립된 캐시 파일 경로를 반환한다.

    CANDIDATE_CACHE가 테스트용 MemoryCachePath로 교체된 경우에는
    그 객체를 그대로 반환하여 기존 테스트 패턴과의 호환성을 유지한다.
    """
    if not isinstance(CANDIDATE_CACHE, Path):
        return CANDIDATE_CACHE
    safe = re.sub(r"[^\w-]", "_", f"{ranker}__{optimizer}")
    return CANDIDATE_CACHE.parent / f"candidate_snapshot_{safe}.json"


def _load_candidate_cache(
    min_score: int,
    ranker: str = "gpt_5_mini",
    optimizer: str = "score_tilted_inverse_vol",
    allow_stale: bool = False,
) -> dict | None:
    override = _candidate_public_override("_load_candidate_cache", _load_candidate_cache)
    if override is not None:
        if ranker == "gpt_5_mini" and optimizer == "score_tilted_inverse_vol":
            return override(min_score)
        return override(min_score, ranker, optimizer)

    # 1) 파일 캐시 우선 (테스트의 MemoryCachePath 포함)
    cache_path = _get_candidate_cache_path(ranker, optimizer)
    try:
        if cache_path.exists():
            result = _candidate_envelope_to_result(
                json.loads(cache_path.read_text(encoding="utf-8")),
                min_score,
                ranker,
                optimizer,
                allow_stale=allow_stale,

            )
            if result is not None:
                return result
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(f"Failed to read candidate cache: {exc}")

    # 2) DB 스냅샷 폴백 (.runtime 유실/재배포 등으로 파일이 없을 때)
    try:
        from src.db.repository import load_account_snapshot

        snap = load_account_snapshot(
            "_candidates_", trader.runtime_flags().trading_env, _candidate_snapshot_kind(min_score, ranker, optimizer)
        )
        if snap is not None:
            return _candidate_envelope_to_result(
                snap["payload"], min_score, ranker, optimizer, allow_stale=allow_stale
            )
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as exc:
        logger.warning(f"Failed to load candidate snapshot: {exc}")
    return None


def _candidate_snapshot_kind(min_score: int, ranker: str, optimizer: str) -> str:
    return f"candidates:{ranker}:{optimizer}:{min_score}"


def _candidate_envelope_to_result(
    cached, min_score: int, ranker: str, optimizer: str, *, allow_stale: bool = False
) -> dict | None:
    """파일/DB 어느 쪽 envelope든 동일하게 검증해 후보 결과를 복원한다."""
    if not isinstance(cached, dict):
        return None
    expected_ai_signature = {
        "enabled": bool(getattr(trader.config, "ai_strategy_enabled", False)),
        "model": getattr(trader.config, "openai_model", "gpt-5-mini"),
        "candidate_limit": int(getattr(trader.config, "ai_candidate_limit", 5) or 5),
        "api_configured": bool(str(getattr(trader.config, "openai_api_key", "") or "").strip()),
        "strategy": _candidate_strategy_cache_signature(ranker),
    }
    if (
        cached.get("trading_env") != trader.runtime_flags().trading_env
        or cached.get("min_score") != min_score
        or cached.get("ranker") != ranker
        or cached.get("optimizer") != optimizer
        or cached.get("ai_signature") != expected_ai_signature
    ):
        return None
    cached_at = cached.get("cached_at")
    if not cached_at:
        return None

    try:
        age = (trader.datetime.now(trader.KST) - trader.datetime.fromisoformat(cached_at)).total_seconds()
    except ValueError:
        return None
    is_stale = age > CANDIDATE_CACHE_TTL_SECONDS
    if is_stale and not allow_stale:
        return None
    rows = cached.get("rows")
    if not isinstance(rows, list):
        return None
    return {
        "candidates": rows,
        "scan_summary": cached.get("scan_summary", []),
        "scanned": cached.get("scanned", len(rows)),
        "min_score": min_score,
        "_cache": {"stale": is_stale, "cached_at": cached_at},
    }


def _save_candidate_cache(
    min_score: int,
    rows: list[dict],
    scan_summary: list[dict],
    scanned: int,
    ranker: str = "gpt_5_mini",
    optimizer: str = "score_tilted_inverse_vol",
) -> str | None:
    override = _candidate_public_override("_save_candidate_cache", _save_candidate_cache)
    if override is not None:
        if ranker == "gpt_5_mini" and optimizer == "score_tilted_inverse_vol":
            return override(min_score, rows, scan_summary, scanned)
        return override(min_score, rows, scan_summary, scanned, ranker, optimizer)
    envelope = {
        "cached_at": trader.datetime.now(trader.KST).isoformat(),
        "trading_env": trader.runtime_flags().trading_env,
        "min_score": min_score,
        "ranker": ranker,
        "optimizer": optimizer,
        "ai_signature": {
            "enabled": bool(getattr(trader.config, "ai_strategy_enabled", False)),
            "model": getattr(trader.config, "openai_model", "gpt-5-mini"),
            "candidate_limit": int(getattr(trader.config, "ai_candidate_limit", 5) or 5),
            "api_configured": bool(str(getattr(trader.config, "openai_api_key", "") or "").strip()),
            "strategy": _candidate_strategy_cache_signature(ranker),
        },
        "rows": rows,
        "scan_summary": scan_summary,
        "scanned": scanned,
    }
    cache_path = _get_candidate_cache_path(ranker, optimizer)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    # DB write-through: 파일 캐시가 유실되어도 마지막 성공본을 DB에서 복구한다.
    try:
        from src.db.repository import save_account_snapshot

        save_account_snapshot(
            "_candidates_",
            trader.runtime_flags().trading_env,
            _candidate_snapshot_kind(min_score, ranker, optimizer),
            envelope,
            envelope["cached_at"],
        )
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as exc:
        logger.warning(f"Failed to persist candidate snapshot: {exc}")
    return envelope["cached_at"]
