"""Operator-owned lifecycle gate for autonomous strategies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from src.config import config

from .models import TradeIntent


RUNNABLE_STATUSES = frozenset({"approved", "live_limited", "live"})
BLOCKED_STATUSES = frozenset({"review_required", "suspended", "retired"})


@dataclass(frozen=True)
class StrategyHealth:
    observations: int = 0
    errors: int = 0
    state_mismatches: int = 0
    fallback_count: int = 0
    broker_unknown_count: int = 0
    unprotected_count: int = 0
    realized_pnl: float = 0.0
    realized_drawdown: float = 0.0
    halt_required: bool = False


@dataclass(frozen=True)
class LifecyclePolicy:
    max_error_rate: float = 0.20
    min_error_rate_observations: int = 5
    max_state_mismatches: int = 0


@dataclass(frozen=True)
class LifecycleDecision:
    allowed: bool
    status: str
    reasons: tuple[str, ...] = ()
    halt_required: bool = False


class StrategyCatalog(Protocol):
    def get(self, strategy_id: str) -> Mapping[str, Any] | None: ...


class StrategyRepositoryCatalog:
    """Read normalized strategy lifecycle data from the existing repository."""

    def get(self, strategy_id: str) -> Mapping[str, Any] | None:
        from src.db import strategy_repository

        target = str(strategy_id)
        return next(
            (
                strategy
                for strategy in strategy_repository.load_ai_strategies()
                if str(strategy.get("id")) == target
            ),
            None,
        )


class StrategyLifecycleGate:
    def __init__(
        self,
        catalog: StrategyCatalog | None = None,
        policy: LifecyclePolicy | None = None,
    ):
        self.catalog = catalog or StrategyRepositoryCatalog()
        self.policy = policy or LifecyclePolicy()

    def evaluate(
        self,
        intent: TradeIntent,
        health: StrategyHealth | None = None,
    ) -> LifecycleDecision:
        reducing = intent.action.value in {"reduce", "exit", "cancel_pending"}
        try:
            strategy = self.catalog.get(intent.strategy_id)
        except Exception as exc:
            return LifecycleDecision(
                reducing, "unknown",
                (f"strategy catalog unavailable: {type(exc).__name__}",),
                True,
            )
        if not strategy:
            return LifecycleDecision(
                reducing, "missing", ("strategy is not registered",), True
            )

        status = str(strategy.get("status") or "").strip().lower()
        preapproval_runnable = (
            status in {"verified", "backtested", "paper_testing", "paper_passed"}
            and not bool(getattr(config, "autonomy_require_approval", True))
        )
        reasons: list[str] = []
        halt = False
        if status in BLOCKED_STATUSES:
            reasons.append(f"strategy status is {status}")
        elif status not in RUNNABLE_STATUSES and not preapproval_runnable:
            reasons.append(f"strategy status {status or 'missing'} is not runnable")

        stored_version = strategy.get("strategy_version")
        if (
            not isinstance(intent.strategy_version, int)
            or isinstance(intent.strategy_version, bool)
            or intent.strategy_version < 1
            or stored_version != intent.strategy_version
        ):
            reasons.append("strategy_version mismatch")
            halt = True
        stored_hash = str(strategy.get("profile_hash") or "").strip()
        if not stored_hash or stored_hash != str(intent.profile_hash).strip():
            reasons.append("profile_hash mismatch")
            halt = True

        fallback_used = bool(intent.metadata.get("fallback_used", False))
        profile = strategy.get("profile") or {}
        fallback_approved = bool(
            profile.get("allow_fallback_trade", False)
            or (profile.get("risk") or {}).get("allow_fallback_trade", False)
        )
        if fallback_used and not fallback_approved:
            reasons.append("fallback trading is not explicitly approved")

        health = health or StrategyHealth()
        if health.observations < 0 or health.errors < 0 or health.state_mismatches < 0:
            reasons.append("invalid strategy health counters")
            halt = True
        elif health.errors > health.observations:
            reasons.append("strategy errors exceed observations")
            halt = True
        else:
            error_rate = (
                health.errors / health.observations if health.observations else 0.0
            )
            if (
                health.observations >= self.policy.min_error_rate_observations
                and error_rate > self.policy.max_error_rate
            ):
                reasons.append("strategy error rate exceeds lifecycle limit")
                halt = True
            if health.state_mismatches > self.policy.max_state_mismatches:
                reasons.append("strategy state mismatch limit exceeded")
                halt = True
            if health.halt_required:
                reasons.append("strategy health halt required")
                halt = True

        # Lifecycle/health halts stop new risk but must never prevent the
        # owning strategy from reducing or exiting an existing position.
        return LifecycleDecision(reducing or not reasons, status, tuple(reasons), halt)
