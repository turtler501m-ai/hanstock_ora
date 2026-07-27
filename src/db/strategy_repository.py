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

AI_STRATEGIES_FILE = Path(".runtime/ai_strategies.json")

def _default_strategy_profile(strategy: dict) -> dict:
    provider = strategy.get("provider") or ("openai" if strategy.get("model") != "none" else "rule")
    model = strategy.get("model", "none")
    weight = max(0.0, min(1.0, float(strategy.get("weight", 0.0) or 0.0)))
    if provider == "none":
        provider = "rule" if model == "none" else str(model).split("_", 1)[0]
    return {
        "strategy_type": strategy.get("strategy_type", "rebound"),
        "risk_level": strategy.get("risk_level", "balanced"),
        "provider": provider,
        "model": model,
        "ai_weight": weight,
        "min_rule_score_for_ai": 1.5,
        "min_ai_confidence": 0.6,
        "allow_candidate_promotion": False,
        "focus": ["rsi2_oversold", "bollinger_lower_band", "volume_recovery"],
        "avoid": ["high_volatility_breakdown", "overheated_rsi", "weak_liquidity"],
        "market_regime_filter": ["neutral", "bull", "low_volatility"],
        "backtest": {
            "min_warmup_periods": 60,
            "commission_bps": 15,
            "slippage_bps": 5,
            "market_impact_bps": 5,
        },
        "risk": {
            "max_ai_weight": weight,
            "max_risk_per_trade_pct": 0.5,
            "max_total_open_risk_pct": 2.0,
            "max_sector_exposure_pct": 20.0,
            "max_liquidity_participation_pct": 0.5,
            "max_strategy_exposure_pct": 30.0,
            "max_data_age_seconds": 60,
            "min_cash_reserve_pct": 20.0,
            "max_daily_ai_orders": 3,
            "paper_trading_required_days": 20,
        },
    }


def _parse_strategy_profile(strategy: dict) -> dict:
    raw_profile = strategy.get("profile")
    if not raw_profile:
        raw_profile = strategy.get("profile_json")
    if isinstance(raw_profile, str) and raw_profile.strip():
        try:
            raw_profile = json.loads(raw_profile)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            raw_profile = {}
    if not isinstance(raw_profile, dict):
        raw_profile = {}
    profile = _default_strategy_profile(strategy)
    default_risk = dict(profile["risk"])
    default_backtest = dict(profile["backtest"])
    profile.update(raw_profile)
    profile["risk"] = {
        **default_risk,
        **(
            raw_profile.get("risk")
            if isinstance(raw_profile.get("risk"), dict)
            else {}
        ),
    }
    profile["backtest"] = {
        **default_backtest,
        **(
            raw_profile.get("backtest")
            if isinstance(raw_profile.get("backtest"), dict)
            else {}
        ),
    }
    profile["model"] = str(profile.get("model") or strategy.get("model") or "none")
    profile["ai_weight"] = max(0.0, min(1.0, float(profile.get("ai_weight", strategy.get("weight", 0.0)) or 0.0)))
    return profile


def strategy_profile_hash(profile: dict) -> str:
    payload = json.dumps(profile or {}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_ai_strategy(strategy: dict) -> dict:
    item = dict(strategy)
    item["provider"] = str(item.get("provider") or ("openai" if item.get("model") != "none" else "none"))
    item["model"] = str(item.get("model") or "none")
    item["weight"] = max(0.0, min(1.0, float(item.get("weight", 0.0) or 0.0)))
    item["selected"] = bool(item.get("selected", False))
    item["strategy_version"] = int(item.get("strategy_version") or 1)
    # Selection is not lifecycle approval.  Migrated/legacy records without an
    # explicit status must pass the normal verification lifecycle.
    item["status"] = str(item.get("status") or "draft")
    profile = _parse_strategy_profile(item)
    item["profile"] = profile
    item["profile_json"] = json.dumps(profile, ensure_ascii=False, sort_keys=True)
    item["profile_hash"] = strategy_profile_hash(profile)
    item["description"] = str(item.get("description") or "")
    for key in (
        "last_verified_at",
        "last_backtested_at",
        "last_paper_started_at",
        "last_paper_completed_at",
        "last_used_at",
        "last_validation_result",
    ):
        item[key] = item.get(key)
    return item


def load_ai_strategies() -> list[dict]:
    default_strategies = [
        {
            "id": "gpt_5_mini_default",
            "name": "🤖 GPT-5-mini 기본 추론 랭커",
            "provider": "openai",
            "model": "gpt-5-mini",
            "weight": 0.4,
            "description": "기술적 룰베이스 점수와 GPT-5-mini의 단기 반등 추론 점수를 6:4 비율로 결합하는 표준 AI 랭커입니다.",
            "selected": True
        },
        {
            "id": "rule_only_default",
            "name": "⚙️ 기본 기술 룰베이스 랭커",
            "provider": "none",
            "model": "none",
            "weight": 0.0,
            "description": "OpenAI API 호출 없이 정량적 기술 지표 규칙(RSI, MACD, SMA)만을 조합하여 0~5점 척도로 계산합니다.",
            "selected": False
        },
        {
            "id": "ranker_lgbm_v3",
            "name": "📊 LightGBM 순위 예측 랭커 (v3)",
            "provider": "none",
            "model": "ranker_lgbm_v3",
            "weight": 0.5,
            "description": "LightGBM 모델을 사용하여 종목의 상승 확률 및 순위를 고속으로 정밀 예측하는 단기 스코어링 엔진입니다.",
            "selected": False
        },
        {
            "id": "allocator_v2",
            "name": "⚖️ 리스크 예산 배분기 (v2)",
            "provider": "none",
            "model": "allocator_v2",
            "weight": 0.3,
            "description": "변동성 역수와 점수 분포 틸팅(MPT) 기법을 개량하여 포트폴리오의 리스크 부담을 지능적으로 예산화하여 배분합니다.",
            "selected": False
        },
        {
            "id": "ppo_policy_v1",
            "name": "🧠 PPO 강화학습 최적 정책 (v1)",
            "provider": "none",
            "model": "ppo_policy_v1",
            "weight": 0.6,
            "description": "Proximal Policy Optimization (PPO) 알고리즘으로 훈련된 강화학습 에이전트가 변동성 및 추세를 바탕으로 최적의 거래 액션을 도출합니다.",
            "selected": False
        }
    ]
    
    try:
        init_db()
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            c = conn.execute("SELECT * FROM ai_strategies ORDER BY id ASC")
            rows = c.fetchall()
            if len(rows) > 0:
                strategies = []
                for row in rows:
                    strategies.append(normalize_ai_strategy({
                        "id": row["id"],
                        "name": row["name"],
                        "provider": row["provider"],
                        "model": row["model"],
                        "weight": float(row["weight"]),
                        "description": row["description"],
                        "selected": row["selected"] == 1,
                        "status": row["status"] if "status" in row.keys() else None,
                        "profile_json": row["profile_json"] if "profile_json" in row.keys() else None,
                        "strategy_version": row["strategy_version"] if "strategy_version" in row.keys() else 1,
                        "profile_hash": row["profile_hash"] if "profile_hash" in row.keys() else None,
                        "last_verified_at": row["last_verified_at"] if "last_verified_at" in row.keys() else None,
                        "last_backtested_at": row["last_backtested_at"] if "last_backtested_at" in row.keys() else None,
                        "last_paper_started_at": row["last_paper_started_at"] if "last_paper_started_at" in row.keys() else None,
                        "last_paper_completed_at": row["last_paper_completed_at"] if "last_paper_completed_at" in row.keys() else None,
                        "last_used_at": row["last_used_at"] if "last_used_at" in row.keys() else None,
                        "last_validation_result": row["last_validation_result"] if "last_validation_result" in row.keys() else None,
                    }))
                return strategies
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning(f"Failed to load AI strategies from DB: {exc}")
        
    # Fallback/Migration: Load from JSON if exists, else defaults
    strategies = default_strategies
    if AI_STRATEGIES_FILE.exists():
        try:
            strategies = json.loads(AI_STRATEGIES_FILE.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            logger.warning(f"Failed to read AI strategy fallback: {exc}")
            
    # Migrate JSON/defaults into DB
    try:
        save_ai_strategies(strategies)
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as exc:
        logger.warning(f"Failed to migrate AI strategy fallback: {exc}")
    return [normalize_ai_strategy(s) for s in strategies]


def save_ai_strategies(strategies: list[dict]) -> None:
    # Save to JSON as backup
    try:
        AI_STRATEGIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        strategies = [normalize_ai_strategy(s) for s in strategies]
        AI_STRATEGIES_FILE.write_text(json.dumps(strategies, ensure_ascii=False, indent=2), encoding="utf-8")
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to save AI strategies to JSON: {e}")
        
    # Save to DB
    try:
        init_db()
        with connect_db() as conn:
            # Clear and rebuild
            conn.execute("DELETE FROM ai_strategies")
            for s in strategies:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ai_strategies (
                        id, name, provider, model, weight, description, selected,
                        status, profile_json, strategy_version, profile_hash,
                        last_verified_at, last_backtested_at, last_paper_started_at,
                        last_paper_completed_at, last_used_at, last_validation_result
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        s["id"],
                        s["name"],
                        s["provider"],
                        s["model"],
                        float(s["weight"]),
                        s.get("description", ""),
                        1 if s.get("selected", False) else 0,
                        s.get("status", "draft"),
                        s.get("profile_json"),
                        int(s.get("strategy_version") or 1),
                        s.get("profile_hash"),
                        s.get("last_verified_at"),
                        s.get("last_backtested_at"),
                        s.get("last_paper_started_at"),
                        s.get("last_paper_completed_at"),
                        s.get("last_used_at"),
                        s.get("last_validation_result"),
                    )
                )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to save AI strategies to DB: {e}")


def record_ai_strategy_event(
    strategy_id: str,
    event_type: str,
    payload: dict | None = None,
    strategy_version: int | None = None,
) -> None:
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    try:
        init_db()
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO ai_strategy_events (ts, strategy_id, strategy_version, event_type, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    strategy_id,
                    strategy_version,
                    event_type,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to record AI strategy event: {e}")


def halt_ai_strategy(
    strategy_id: str,
    *,
    target_status: str,
    reason: str,
    payload: dict | None = None,
) -> dict:
    """Atomically halt a strategy, deselect it, and record the audit event."""
    if target_status not in {"review_required", "suspended"}:
        raise ValueError("target_status must be review_required or suspended")
    init_db()
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM ai_strategies WHERE id=?", (str(strategy_id),)
        ).fetchone()
        if not current:
            conn.rollback()
            raise ValueError("strategy not found")
        previous = str(current["status"] or "draft")
        rank = {"review_required": 1, "suspended": 2, "retired": 3}
        effective = (
            previous
            if rank.get(previous, 0) >= rank[target_status]
            else target_status
        )
        conn.execute(
            "UPDATE ai_strategies SET status=?, selected=0 WHERE id=?",
            (effective, str(strategy_id)),
        )
        event_payload = {
            **(payload or {}),
            "reason": str(reason),
            "previous_status": previous,
            "new_status": effective,
        }
        conn.execute(
            """
            INSERT INTO ai_strategy_events
            (ts, strategy_id, strategy_version, event_type, payload)
            VALUES (?, ?, ?, 'autonomy_health_halt', ?)
            """,
            (
                datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
                str(strategy_id),
                current["strategy_version"],
                json.dumps(event_payload, ensure_ascii=False),
            ),
        )
        conn.commit()
        return {
            "strategy_id": str(strategy_id),
            "previous_status": previous,
            "new_status": effective,
            "changed": previous != effective or bool(current["selected"]),
            "selected": False,
        }


def get_ai_strategy_events(strategy_id: str, limit: int = 100) -> list[dict]:
    try:
        init_db()
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM ai_strategy_events
                WHERE strategy_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (strategy_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to fetch AI strategy events: {e}")
        return []


def get_ai_strategy_performance(strategy_id: str, days: int = 30) -> dict:
    init_db()
    since_date = (datetime.now(KST) - timedelta(days=max(1, int(days or 30)))).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM scanned_candidates
                WHERE strategy_id = ?
                  AND scanned_at >= ?
                ORDER BY scanned_at DESC
                """,
                (strategy_id, since_date),
            ).fetchall()
            candidates = [dict(row) for row in rows]
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to fetch AI strategy performance rows: {e}")
        candidates = []

    final_scores = [
        float(item.get("final_score"))
        for item in candidates
        if item.get("final_score") is not None
    ]
    rule_scores = [
        float(item.get("rule_score"))
        for item in candidates
        if item.get("rule_score") is not None
    ]
    ml_scores = [
        float(item.get("ml_score"))
        for item in candidates
        if item.get("ml_score") is not None
    ]
    return_1d = [
        float(item.get("forward_return_1d"))
        for item in candidates
        if item.get("forward_return_1d") is not None
    ]
    return_5d = [
        float(item.get("forward_return_5d"))
        for item in candidates
        if item.get("forward_return_5d") is not None
    ]
    return_20d = [
        float(item.get("forward_return_20d"))
        for item in candidates
        if item.get("forward_return_20d") is not None
    ]
    status_counts: dict[str, int] = {}
    optimizer_counts: dict[str, int] = {}
    for item in candidates:
        status = str(item.get("ai_model_status") or "unknown")
        optimizer = str(item.get("optimizer") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        optimizer_counts[optimizer] = optimizer_counts.get(optimizer, 0) + 1

    def avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    def win_rate(values: list[float]) -> float | None:
        return round((sum(1 for value in values if value > 0) / len(values)) * 100, 2) if values else None

    trade_summary = {
        "trade_count": 0,
        "order_count": 0,
        "approval_count": 0,
        "filled_count": 0,
        "fill_rate": None,
        "order_status_counts": {},
        "approval_status_counts": {},
    }
    try:
        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            approval_rows = conn.execute(
                """
                SELECT *
                FROM approvals
                WHERE strategy_id = ?
                  AND created_at >= ?
                ORDER BY created_at DESC
                """,
                (strategy_id, since_date),
            ).fetchall()
            approvals = [dict(row) for row in approval_rows]
            approval_status_counts: dict[str, int] = {}
            for approval in approvals:
                status = str(approval.get("status") or "unknown")
                approval_status_counts[status] = approval_status_counts.get(status, 0) + 1

            trade_rows = conn.execute(
                """
                SELECT *
                FROM trades
                WHERE strategy_id = ?
                  AND ts >= ?
                ORDER BY ts DESC
                """,
                (strategy_id, since_date),
            ).fetchall()
            trades = [dict(row) for row in trade_rows]
            status_counts_trade: dict[str, int] = {}
            for trade in trades:
                status = str(trade.get("order_status") or "unknown")
                status_counts_trade[status] = status_counts_trade.get(status, 0) + 1
            filled_count = sum(
                1
                for trade in trades
                if str(trade.get("order_status") or "") in {"filled", "simulated"}
                or int(trade.get("filled_qty") or 0) > 0
            )
            approval_count = len(approvals)
            order_count = approval_count if approval_count else len(trades)
            trade_summary = {
                "trade_count": len(trades),
                "approval_count": approval_count,
                "order_count": order_count,
                "filled_count": filled_count,
                "fill_rate": round((filled_count / order_count) * 100, 2) if order_count else None,
                "order_status_counts": status_counts_trade,
                "approval_status_counts": approval_status_counts,
                "recent_approvals": approvals[:20],
                "recent_trades": trades[:20],
            }
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to fetch AI strategy trade performance: {e}")

    return {
        "strategy_id": strategy_id,
        "days": days,
        "candidate_count": len(candidates),
        "avg_final_score": avg(final_scores),
        "avg_rule_score": avg(rule_scores),
        "avg_ml_score": avg(ml_scores),
        "avg_return_1d": avg(return_1d),
        "avg_return_5d": avg(return_5d),
        "avg_return_20d": avg(return_20d),
        "win_rate_1d": win_rate(return_1d),
        "win_rate_5d": win_rate(return_5d),
        "win_rate_20d": win_rate(return_20d),
        "return_sample_count_1d": len(return_1d),
        "return_sample_count_5d": len(return_5d),
        "return_sample_count_20d": len(return_20d),
        "trade_summary": trade_summary,
        "ai_model_status_counts": status_counts,
        "optimizer_counts": optimizer_counts,
        "recent_candidates": candidates[:20],
    }


def review_ai_strategy_performance(strategy_id: str, days: int = 30) -> dict:
    strategies = load_ai_strategies()
    target = next((item for item in strategies if item.get("id") == strategy_id), None)
    if target is None:
        return {"ok": False, "reason": "strategy_not_found", "strategy_id": strategy_id}

    performance = get_ai_strategy_performance(strategy_id, days=days)
    candidate_count = int(performance.get("candidate_count") or 0)
    status_counts = performance.get("ai_model_status_counts") or {}
    fallback_count = int(status_counts.get("fallback", 0)) + int(status_counts.get("disabled", 0))
    fallback_rate = (fallback_count / candidate_count) if candidate_count else 0.0
    avg_final_score = performance.get("avg_final_score")
    avg_return_5d = performance.get("avg_return_5d")
    fill_rate = (performance.get("trade_summary") or {}).get("fill_rate")
    warnings = []

    if candidate_count == 0:
        warnings.append("no candidates in review window")
    if candidate_count >= 5 and avg_final_score is not None and float(avg_final_score) < 2.5:
        warnings.append("low average final score")
    if candidate_count >= 5 and fallback_rate >= 0.5:
        warnings.append("high AI fallback rate")
    if candidate_count >= 5 and avg_return_5d is not None and float(avg_return_5d) < 0:
        warnings.append("negative 5-day candidate return")
    if fill_rate is not None and float(fill_rate) < 50:
        warnings.append("low order fill rate")

    previous_status = str(target.get("status") or "draft")
    new_status = previous_status
    if (
        candidate_count >= 10
        and avg_final_score is not None
        and float(avg_final_score) < 1.5
        and fallback_rate >= 0.8
    ):
        new_status = "retired"
    elif candidate_count >= 10 and avg_return_5d is not None and float(avg_return_5d) <= -5:
        new_status = "retired"
    elif warnings and previous_status in {"approved", "paper_passed", "backtested", "verified"}:
        new_status = "review_required"

    changed = new_status != previous_status
    if changed:
        for item in strategies:
            if item.get("id") == strategy_id:
                item["status"] = new_status
                if new_status == "retired":
                    item["selected"] = False
                target = item
                break
        save_ai_strategies(strategies)

    result = {
        "ok": True,
        "strategy_id": strategy_id,
        "days": days,
        "previous_status": previous_status,
        "new_status": new_status,
        "changed": changed,
        "warnings": warnings,
        "fallback_rate": round(fallback_rate, 4),
        "performance": performance,
    }
    record_ai_strategy_event(strategy_id, "performance_review", result, target.get("strategy_version"))
    return result
__all__ = ['KST', 'AI_STRATEGIES_FILE', '_default_strategy_profile', '_parse_strategy_profile', 'strategy_profile_hash', 'normalize_ai_strategy', 'load_ai_strategies', 'save_ai_strategies', 'record_ai_strategy_event', 'halt_ai_strategy', 'get_ai_strategy_events', 'get_ai_strategy_performance', 'review_ai_strategy_performance']
