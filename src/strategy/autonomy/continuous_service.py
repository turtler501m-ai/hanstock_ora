"""Continuous autonomous strategy cycles with isolated, stoppable scheduling."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from .orchestrator import (
    AutonomousStrategyOrchestrator,
    CycleResult,
    MarketContext,
    PortfolioContext,
    StrategyAdapter,
)
from .protection import ProtectionGateSignal
from .lifecycle import StrategyHealth


class ContextProvider(Protocol):
    def market_context(self, market: str) -> MarketContext: ...

    def portfolio_context(self, market: str) -> PortfolioContext: ...


class RecoveryHooks(Protocol):
    def reconcile_open_orders(self, market: str) -> Any: ...

    def audit_unprotected_positions(self, market: str) -> ProtectionGateSignal: ...


class ApprovalPlanQueue(Protocol):
    def queue_cycle(self, cycle: CycleResult) -> Sequence[Any]: ...


@dataclass(frozen=True)
class MarketCycleResult:
    market: str
    status: str
    cycle_key: str | None = None
    strategy_results: tuple[CycleResult, ...] = ()
    errors: tuple[str, ...] = ()
    protection_block: bool = False
    approval_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class StartupRecoveryResult:
    by_market: Mapping[str, tuple[str, ...]]
    blocked_markets: frozenset[str]


@dataclass
class ContinuousStrategyService:
    markets: Sequence[str]
    adapters: Mapping[str, Sequence[StrategyAdapter]]
    orchestrator: AutonomousStrategyOrchestrator
    contexts: ContextProvider
    recovery: RecoveryHooks
    approval_planner: ApprovalPlanQueue
    health_service: Any | None = None
    interval_seconds: float = 30.0
    stop_event: threading.Event = field(default_factory=threading.Event)
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        normalized = tuple(dict.fromkeys(str(item).upper() for item in self.markets))
        if not normalized:
            raise ValueError("at least one market is required")
        self.markets = normalized
        self._locks = {market: threading.Lock() for market in normalized}
        self._safety_blocked: set[str] = set()
        self._started = False

    def startup_recovery(self) -> StartupRecoveryResult:
        """Reconcile durable orders and audit protection before first cycle."""
        results: dict[str, tuple[str, ...]] = {}
        for market in self.markets:
            messages: list[str] = []
            blocked = False
            try:
                self.recovery.reconcile_open_orders(market)
                messages.append("managed_open_orders_reconciled")
            except Exception as exc:
                blocked = True
                messages.append(f"order_reconciliation_failed:{type(exc).__name__}")
            try:
                signal = self.recovery.audit_unprotected_positions(market)
                if signal.block_new_risk:
                    blocked = True
                    messages.append(f"protection_block:{signal.reason}")
                else:
                    messages.append("protection_audit_clear")
            except Exception as exc:
                blocked = True
                messages.append(f"protection_audit_failed:{type(exc).__name__}")
            if blocked:
                self._safety_blocked.add(market)
            else:
                self._safety_blocked.discard(market)
            results[market] = tuple(messages)
        self._started = True
        return StartupRecoveryResult(results, frozenset(self._safety_blocked))

    def run_market_once(self, market: str) -> MarketCycleResult:
        """Run one market without allowing overlapping re-entry."""
        market = str(market).upper()
        if market not in self._locks:
            raise ValueError(f"unsupported market: {market}")
        lock = self._locks[market]
        if not lock.acquire(blocking=False):
            return MarketCycleResult(market, "reentry_skipped")
        try:
            if self.stop_event.is_set():
                return MarketCycleResult(market, "stopped")
            if not self._started:
                self.startup_recovery()
            now = self.clock()
            cycle_key = f"{market}:{now.isoformat()}"

            try:
                signal = self.recovery.audit_unprotected_positions(market)
            except Exception as exc:
                self._safety_blocked.add(market)
                signal = ProtectionGateSignal(
                    True, "protection_state_unavailable",
                    alerts=(f"protection audit failed: {type(exc).__name__}",),
                )
            if signal.block_new_risk:
                self._safety_blocked.add(market)
            else:
                self._safety_blocked.discard(market)

            try:
                market_context = self.contexts.market_context(market)
                portfolio = self.contexts.portfolio_context(market)
                portfolio = _apply_protection_block(
                    portfolio, market in self._safety_blocked
                )
            except Exception as exc:
                # A safety snapshot cannot be guessed. No orchestrator call is
                # made, so no new risk order can be created in this cycle.
                self._safety_blocked.add(market)
                return MarketCycleResult(
                    market,
                    "safety_snapshot_blocked",
                    cycle_key,
                    errors=(f"snapshot_failed:{type(exc).__name__}",),
                    protection_block=True,
                )

            results: list[CycleResult] = []
            errors: list[str] = []
            approval_ids: list[int] = []
            for adapter in tuple(self.adapters.get(market, ())):
                if self.stop_event.is_set():
                    break
                try:
                    strategy_health = None
                    if self.health_service is not None:
                        try:
                            report = self.health_service.evaluate_and_enforce(
                                adapter.strategy_id
                            )
                            strategy_health = report.lifecycle_health()
                        except Exception as health_exc:
                            # Unknown health must block new risk. Action-aware
                            # lifecycle handling still permits REDUCE/EXIT.
                            strategy_health = StrategyHealth(
                                observations=0,
                                errors=0,
                                state_mismatches=1,
                                halt_required=True,
                            )
                            errors.append(
                                f"{adapter.strategy_id}:health:"
                                f"{type(health_exc).__name__}:{health_exc}"
                            )
                    cycle = self.orchestrator.run_cycle(
                        cycle_key=f"{cycle_key}:{adapter.strategy_id}",
                        adapter=adapter,
                        market=market_context,
                        portfolio=portfolio,
                        strategy_health=strategy_health,
                    )
                    results.append(cycle)
                    plans = tuple(self.approval_planner.queue_cycle(cycle))
                    approval_ids.extend(
                        int(item.approval_id) for item in plans
                    )
                except Exception as exc:
                    # One strategy must not prevent other strategies or
                    # markets from managing their existing positions.
                    errors.append(
                        f"{adapter.strategy_id}:{type(exc).__name__}:{exc}"
                    )
            status = "completed_with_errors" if errors else "completed"
            return MarketCycleResult(
                market,
                status,
                cycle_key,
                tuple(results),
                tuple(errors),
                market in self._safety_blocked,
                tuple(approval_ids),
            )
        finally:
            lock.release()

    def run_iteration(self) -> tuple[MarketCycleResult, ...]:
        results = []
        for market in self.markets:
            if self.stop_event.is_set():
                break
            try:
                results.append(self.run_market_once(market))
            except Exception as exc:
                results.append(
                    MarketCycleResult(
                        market,
                        "market_error",
                        errors=(f"{type(exc).__name__}:{exc}",),
                        protection_block=market in self._safety_blocked,
                    )
                )
        return tuple(results)

    def run_forever(self) -> None:
        """Run until stop() is called; Event.wait makes shutdown interruptible."""
        if not self._started:
            self.startup_recovery()
        while not self.stop_event.is_set():
            self.run_iteration()
            self.stop_event.wait(self.interval_seconds)

    def stop(self) -> None:
        self.stop_event.set()

    def is_new_risk_blocked(self, market: str) -> bool:
        return str(market).upper() in self._safety_blocked


def _apply_protection_block(
    portfolio: PortfolioContext,
    blocked: bool,
) -> PortfolioContext:
    snapshots = {
        symbol: replace(snapshot, protection_global_block=bool(blocked))
        for symbol, snapshot in portfolio.risk_snapshots.items()
    }
    return replace(portfolio, risk_snapshots=snapshots)
