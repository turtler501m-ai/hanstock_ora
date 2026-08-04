"""Strategy health aggregation and atomic automatic lifecycle halts."""

from __future__ import annotations

from src.db.ai_stock_support import connect_ai_stock
from src.db import strategy_repository

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol

from .lifecycle import StrategyHealth


@dataclass(frozen=True)
class StrategyHealthPolicy:
    min_rate_observations: int = 5
    max_error_rate: float = 0.20
    max_fallback_rate: float = 0.50
    max_state_mismatches: int = 0
    max_broker_unknown: int = 0
    max_unprotected_positions: int = 0
    max_realized_drawdown: float = 100_000.0
    max_realized_loss: float = 100_000.0


@dataclass(frozen=True)
class StrategyHealthReport:
    strategy_id: str
    calls: int
    errors: int
    fallback_count: int
    state_mismatches: int
    broker_unknown_count: int
    unprotected_count: int
    filled_order_count: int
    realized_pnl: float
    realized_drawdown: float
    error_rate: float
    fallback_rate: float
    halt_required: bool
    target_status: str | None
    reasons: tuple[str, ...]
    transition: Mapping[str, Any] | None = None

    def lifecycle_health(self) -> StrategyHealth:
        return StrategyHealth(
            observations=self.calls,
            errors=self.errors,
            state_mismatches=self.state_mismatches,
            fallback_count=self.fallback_count,
            broker_unknown_count=self.broker_unknown_count,
            unprotected_count=self.unprotected_count,
            realized_pnl=self.realized_pnl,
            realized_drawdown=self.realized_drawdown,
            halt_required=self.halt_required,
        )


class HealthSource(Protocol):
    def aggregate(self, strategy_id: str) -> Mapping[str, Any]: ...


class HealthLifecycleStore(Protocol):
    def halt(
        self,
        strategy_id: str,
        *,
        target_status: str,
        reason: str,
        payload: dict[str, Any],
    ) -> Mapping[str, Any]: ...


class RepositoryHealthSource:
    """Aggregate only durable autonomy records, never in-memory counters."""

    def aggregate(self, strategy_id: str) -> Mapping[str, Any]:
        with connect_ai_stock() as conn:
            decisions = conn.execute(
                """
                SELECT action, intent_payload, rejection_reason
                FROM ai_strategy_decisions WHERE strategy_id=? ORDER BY id
                """,
                (strategy_id,),
            ).fetchall()
            orders = conn.execute(
                """
                SELECT status, filled_qty FROM ai_managed_orders
                WHERE strategy_id=?
                """,
                (strategy_id,),
            ).fetchall()
            positions = conn.execute(
                """
                SELECT id, realized_pnl, status FROM ai_strategy_positions
                WHERE strategy_id=? ORDER BY COALESCE(closed_at, updated_at), id
                """,
                (strategy_id,),
            ).fetchall()
            protections = conn.execute(
                """
                SELECT p.remaining_qty, g.protected_qty, g.status
                FROM ai_strategy_positions p
                LEFT JOIN ai_position_protections g ON g.position_id=p.id
                WHERE p.strategy_id=? AND p.status IN ('open', 'exit_pending')
                  AND p.remaining_qty > 0
                """,
                (strategy_id,),
            ).fetchall()

        fallback_count = 0
        state_mismatches = 0
        errors = 0
        for decision in decisions:
            payload = _json(decision["intent_payload"])
            metadata = payload.get("metadata") if isinstance(payload, dict) else {}
            if isinstance(metadata, dict) and metadata.get("fallback_used"):
                fallback_count += 1
            rejection = str(decision["rejection_reason"] or "").lower()
            if any(
                marker in rejection
                for marker in ("failed", "error", "unavailable", "exception")
            ):
                errors += 1
            if "mismatch" in rejection:
                state_mismatches += 1

        broker_unknown = sum(
            1 for order in orders if str(order["status"]) == "broker_unknown"
        )
        filled_orders = sum(
            1
            for order in orders
            if str(order["status"]) == "filled" or int(order["filled_qty"] or 0) > 0
        )
        unprotected = sum(
            1
            for item in protections
            if (
                item["status"] not in {"active", "amend_pending"}
                or int(item["protected_qty"] or 0) != int(item["remaining_qty"] or 0)
            )
        )
        realized_values = [float(item["realized_pnl"] or 0) for item in positions]
        realized_pnl = sum(realized_values)
        running = 0.0
        peak = 0.0
        drawdown = 0.0
        for value in realized_values:
            running += value
            peak = max(peak, running)
            drawdown = max(drawdown, peak - running)
        return {
            "calls": len(decisions),
            "errors": errors,
            "fallback_count": fallback_count,
            "state_mismatches": state_mismatches,
            "broker_unknown_count": broker_unknown,
            "unprotected_count": unprotected,
            "filled_order_count": filled_orders,
            "realized_pnl": realized_pnl,
            "realized_drawdown": drawdown,
        }


class RepositoryHealthLifecycleStore:
    def halt(self, strategy_id, *, target_status, reason, payload):
        return strategy_repository.halt_ai_strategy(
            strategy_id,
            target_status=target_status,
            reason=reason,
            payload=payload,
        )


class StrategyHealthService:
    def __init__(
        self,
        *,
        source: HealthSource | None = None,
        lifecycle: HealthLifecycleStore | None = None,
        policy: StrategyHealthPolicy | None = None,
    ):
        self.source = source or RepositoryHealthSource()
        self.lifecycle = lifecycle or RepositoryHealthLifecycleStore()
        self.policy = policy or StrategyHealthPolicy()

    def evaluate_and_enforce(self, strategy_id: str) -> StrategyHealthReport:
        raw = self.source.aggregate(strategy_id)
        calls = _count(raw, "calls")
        errors = _count(raw, "errors")
        fallback = _count(raw, "fallback_count")
        mismatches = _count(raw, "state_mismatches")
        broker_unknown = _count(raw, "broker_unknown_count")
        unprotected = _count(raw, "unprotected_count")
        filled = _count(raw, "filled_order_count")
        realized = float(raw.get("realized_pnl") or 0)
        drawdown = max(0.0, float(raw.get("realized_drawdown") or 0))
        error_rate = errors / calls if calls else 0.0
        fallback_rate = fallback / calls if calls else 0.0
        review: list[str] = []
        suspend: list[str] = []
        p = self.policy

        if mismatches > p.max_state_mismatches:
            suspend.append("state_mismatch_limit")
        if broker_unknown > p.max_broker_unknown:
            suspend.append("broker_unknown_limit")
        if unprotected > p.max_unprotected_positions:
            suspend.append("unprotected_position_limit")
        if calls >= p.min_rate_observations and error_rate > p.max_error_rate:
            review.append("error_rate_limit")
        if calls >= p.min_rate_observations and fallback_rate > p.max_fallback_rate:
            review.append("fallback_rate_limit")
        if drawdown > p.max_realized_drawdown:
            review.append("realized_drawdown_limit")
        if realized < -p.max_realized_loss:
            review.append("realized_loss_limit")

        reasons = tuple(suspend + review)
        target = "suspended" if suspend else "review_required" if review else None
        report = StrategyHealthReport(
            strategy_id=strategy_id,
            calls=calls,
            errors=errors,
            fallback_count=fallback,
            state_mismatches=mismatches,
            broker_unknown_count=broker_unknown,
            unprotected_count=unprotected,
            filled_order_count=filled,
            realized_pnl=realized,
            realized_drawdown=drawdown,
            error_rate=error_rate,
            fallback_rate=fallback_rate,
            halt_required=bool(target),
            target_status=target,
            reasons=reasons,
        )
        if not target:
            return report
        transition = self.lifecycle.halt(
            strategy_id,
            target_status=target,
            reason=",".join(reasons),
            payload={"health": asdict(report)},
        )
        return StrategyHealthReport(**{**asdict(report), "transition": transition})


def _json(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _count(data: Mapping[str, Any], key: str) -> int:
    value = int(data.get(key) or 0)
    if value < 0:
        raise ValueError(f"{key} cannot be negative")
    return value
