"""Fail-closed continuous strategy orchestration, stopping before broker I/O."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from src.db import ai_stock_repository as repository

from .models import OrderType, TradeAction, TradeIntent
from .lifecycle import StrategyHealth, StrategyLifecycleGate
from .risk_envelope import RiskDecision, RiskEnvelope, RiskSnapshot
from .validation import IntentValidationError, validate_trade_intent


@dataclass(frozen=True)
class MarketContext:
    market: str
    regime: str
    data_as_of: datetime
    evaluated_at: datetime
    snapshot_id: str
    features: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PortfolioContext:
    account_id: str
    snapshot_id: str
    risk_snapshots: Mapping[str, RiskSnapshot]
    position_quantities: Mapping[str, int] = field(default_factory=dict)

    def risk_snapshot_for(
        self, symbol: str, position_id: str | int | None = None
    ) -> RiskSnapshot | None:
        snapshot = self.risk_snapshots.get(symbol)
        if snapshot is None or position_id in (None, ""):
            return snapshot
        key = str(position_id)
        if key not in self.position_quantities:
            return None
        return replace(
            snapshot,
            current_position_qty=int(self.position_quantities[key]),
        )


class StrategyAdapter(Protocol):
    strategy_id: str

    def scan(self, market: MarketContext, portfolio: PortfolioContext) -> Sequence[TradeIntent]: ...

    def manage_position(
        self,
        position: Mapping[str, Any],
        market: MarketContext,
        portfolio: PortfolioContext,
    ) -> TradeIntent: ...


class Persistence(Protocol):
    def list_active_positions(self, *, market: str, strategy_id: str) -> list[dict[str, Any]]: ...
    def get_decision(self, key: str) -> dict[str, Any] | None: ...
    def save_decision(self, data: dict[str, Any]) -> int: ...
    def update_decision(self, decision_id: int, **values: Any) -> bool: ...
    def create_position(self, data: dict[str, Any]) -> int: ...
    def get_order(self, key: str) -> dict[str, Any] | None: ...
    def create_order(self, data: dict[str, Any]) -> int: ...
    def reserve_risk(
        self, data: dict[str, Any], *, available_cash: float, risk_budget_limit: float
    ) -> dict[str, Any]: ...
    def release_risk(self, reservation_id: int, *, reason: str) -> dict[str, Any]: ...
    def abandon_position(self, position_id: int, *, reason: str) -> bool: ...


class RepositoryPersistence:
    def list_active_positions(self, *, market: str, strategy_id: str):
        return repository.list_strategy_positions(
            market=market, strategy_id=strategy_id, active_only=True
        )

    def get_decision(self, key: str):
        return repository.get_strategy_decision_by_key(key)

    def save_decision(self, data: dict[str, Any]) -> int:
        return repository.save_strategy_decision(data)

    def update_decision(self, decision_id: int, **values: Any) -> bool:
        return repository.update_strategy_decision_result(decision_id, **values)

    def create_position(self, data: dict[str, Any]) -> int:
        return repository.create_strategy_position(data)

    def get_order(self, key: str):
        return repository.get_managed_order_by_key(key)

    def create_order(self, data: dict[str, Any]) -> int:
        return repository.create_managed_order(data)

    def reserve_risk(self, data, *, available_cash, risk_budget_limit):
        return repository.reserve_risk_budget(
            data,
            available_cash=available_cash,
            risk_budget_limit=risk_budget_limit,
        )

    def release_risk(self, reservation_id: int, *, reason: str):
        return repository.release_risk_reservation(reservation_id, reason=reason)

    def abandon_position(self, position_id: int, *, reason: str) -> bool:
        return repository.abandon_pending_strategy_position(position_id, reason=reason)


@dataclass(frozen=True)
class IntentResult:
    intent_id: str
    decision_id: int | None
    position_id: int | None
    order_id: int | None
    status: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CycleResult:
    cycle_key: str
    scanned_intents: int
    managed_positions: int
    results: tuple[IntentResult, ...]


class AutonomousStrategyOrchestrator:
    def __init__(
        self,
        risk_envelope: RiskEnvelope,
        persistence: Persistence | None = None,
        lifecycle_gate: StrategyLifecycleGate | None = None,
    ):
        self.risk = risk_envelope
        self.db = persistence or RepositoryPersistence()
        self.lifecycle = lifecycle_gate or StrategyLifecycleGate()

    def run_cycle(
        self,
        *,
        cycle_key: str,
        adapter: StrategyAdapter,
        market: MarketContext,
        portfolio: PortfolioContext,
        strategy_health: StrategyHealth | None = None,
    ) -> CycleResult:
        if not str(cycle_key).strip():
            raise ValueError("cycle_key is required")
        positions = self.db.list_active_positions(
            market=market.market, strategy_id=adapter.strategy_id
        )
        results: list[IntentResult] = []
        try:
            scanned = tuple(adapter.scan(market, portfolio))
        except Exception as exc:
            scanned = ()
            results.append(_adapter_error("scan", None, exc))

        intents = list(scanned)
        managed = 0
        for position in positions:
            try:
                intents.append(adapter.manage_position(position, market, portfolio))
                managed += 1
            except Exception as exc:
                results.append(_adapter_error(
                    f"position:{position.get('id')}", _int(position.get("id")), exc
                ))

        for intent in intents:
            results.append(
                self._process(
                    cycle_key,
                    adapter,
                    market,
                    portfolio,
                    intent,
                    strategy_health,
                )
            )
        return CycleResult(cycle_key, len(scanned), managed, tuple(results))

    def _process(
        self, cycle_key, adapter, market, portfolio, intent, strategy_health
    ) -> IntentResult:
        key = _key("decision", cycle_key, adapter.strategy_id, getattr(intent, "intent_id", ""))
        duplicate = self.db.get_decision(key)
        if duplicate:
            return IntentResult(
                str(getattr(intent, "intent_id", "")),
                int(duplicate["id"]),
                _int(duplicate.get("position_id")),
                _int(duplicate.get("order_id")),
                "duplicate",
            )
        try:
            lifecycle = self.lifecycle.evaluate(intent, strategy_health)
        except Exception as exc:
            lifecycle = None
            lifecycle_reasons = (
                f"lifecycle evaluation failed: {type(exc).__name__}",
            )
        else:
            lifecycle_reasons = lifecycle.reasons
        if lifecycle is None or not lifecycle.allowed:
            decision_id = self.db.save_decision(
                _decision(
                    key,
                    intent,
                    market,
                    portfolio,
                    "rejected",
                    "; ".join(lifecycle_reasons),
                )
            )
            return IntentResult(
                str(getattr(intent, "intent_id", "")),
                decision_id,
                _int(getattr(intent, "position_id", None)),
                None,
                "lifecycle_rejected",
                tuple(lifecycle_reasons),
            )
        try:
            validate_trade_intent(intent, now=market.evaluated_at)
            if intent.strategy_id != adapter.strategy_id:
                raise IntentValidationError(("strategy ownership mismatch",))
            if intent.market != market.market:
                raise IntentValidationError(("market context mismatch",))
        except (IntentValidationError, AttributeError, TypeError) as exc:
            reasons = tuple(getattr(exc, "errors", (str(exc),)))
            decision_id = self.db.save_decision(
                _decision(key, intent, market, portfolio, "rejected", "; ".join(reasons))
            )
            return IntentResult(
                str(getattr(intent, "intent_id", "")), decision_id, None, None,
                "validation_rejected", reasons
            )

        decision_id = self.db.save_decision(_decision(key, intent, market, portfolio))
        snapshot = portfolio.risk_snapshot_for(
            intent.symbol,
            intent.position_id
            if intent.action in {TradeAction.ADD, TradeAction.REDUCE, TradeAction.EXIT}
            else None,
        )
        if snapshot is None:
            return self._reject(decision_id, intent, "missing trusted risk snapshot")
        try:
            risk = self.risk.evaluate(intent, snapshot)
        except Exception as exc:
            return self._reject(
                decision_id, intent, f"risk evaluation failed: {type(exc).__name__}"
            )
        if not risk.approved:
            reason = "; ".join(risk.reasons or ("risk denied",))
            return self._reject(decision_id, intent, reason, risk=risk)

        position_id = _int(intent.position_id)
        order_id = None
        reservation_id = None
        created_position = False
        if risk.quantity > 0:
            if intent.action == TradeAction.ENTER_LONG:
                try:
                    position_id = self.db.create_position(
                        _position(intent, portfolio, decision_id, risk)
                    )
                    created_position = True
                except Exception as exc:
                    return self._reject(
                        decision_id, intent,
                        f"position creation failed: {type(exc).__name__}", risk=risk
                    )
            if position_id is None:
                return self._reject(
                    decision_id, intent, "position owner is required", risk=risk
                )
            if intent.action in {TradeAction.ENTER_LONG, TradeAction.ADD}:
                try:
                    reservation = self.db.reserve_risk(
                        {
                            "account_id": portfolio.account_id,
                            "market": intent.market,
                            "strategy_id": intent.strategy_id,
                            "position_id": position_id,
                            "order_id": 0,
                            "cash_amount": risk.estimated_cost,
                            "risk_amount": risk.risk_amount,
                            "symbol": intent.symbol,
                            "sector_key": snapshot.sector_key,
                            "exposure_amount": risk.estimated_cost,
                            "exposure_limits": dict(risk.exposure_reservation_limits),
                            "reason": f"decision:{decision_id}",
                            "expires_at": intent.valid_until.isoformat(),
                        },
                        available_cash=snapshot.available_cash,
                        risk_budget_limit=risk.account_risk_reservation_limit,
                    )
                    reservation_id = int(reservation["id"])
                except Exception as exc:
                    reason = f"risk reservation failed: {type(exc).__name__}"
                    if created_position:
                        self.db.abandon_position(position_id, reason=reason)
                    return self._reject(
                        decision_id, intent, reason,
                        position_id=position_id, risk=risk
                    )
            order_key = _key("order", key, position_id, risk.action)
            existing = self.db.get_order(order_key)
            try:
                order_id = int(existing["id"]) if existing else self.db.create_order(
                    _order(order_key, decision_id, position_id, intent, risk)
                )
            except Exception as exc:
                reason = f"managed order creation failed: {type(exc).__name__}"
                if reservation_id is not None:
                    self.db.release_risk(reservation_id, reason=reason)
                if created_position:
                    self.db.abandon_position(position_id, reason=reason)
                return self._reject(
                    decision_id, intent,
                    reason,
                    position_id=position_id, risk=risk
                )

        self.db.update_decision(
            decision_id,
            risk_decision=asdict(risk),
            final_action=risk.action,
            rejection_reason=None,
            order_id=order_id,
            position_id=position_id,
        )
        status = "managed_order_created" if order_id else "decision_recorded"
        return IntentResult(intent.intent_id, decision_id, position_id, order_id, status)

    def _reject(
        self, decision_id, intent, reason, *, position_id=None, risk=None
    ) -> IntentResult:
        position_id = position_id or _int(getattr(intent, "position_id", None))
        self.db.update_decision(
            decision_id,
            risk_decision=asdict(risk) if risk else {},
            final_action="rejected",
            rejection_reason=reason,
            order_id=None,
            position_id=position_id,
        )
        return IntentResult(
            intent.intent_id, decision_id, position_id, None, "rejected", (reason,)
        )


def _decision(key, intent, market, portfolio, final_action=None, rejection=None):
    action = getattr(intent.action, "value", str(intent.action))
    return {
        "decision_key": key,
        "ts": market.evaluated_at.isoformat(),
        "strategy_id": intent.strategy_id,
        "strategy_version": intent.strategy_version,
        "profile_hash": intent.profile_hash,
        "market": intent.market,
        "symbol": intent.symbol,
        "position_id": _int(getattr(intent, "position_id", None)),
        "market_snapshot_id": market.snapshot_id,
        "portfolio_snapshot_id": portfolio.snapshot_id,
        "data_as_of": intent.data_as_of.isoformat(),
        "action": action,
        "confidence": intent.confidence,
        "thesis": intent.thesis,
        "invalidation_conditions": (
            list(intent.invalidation.conditions) if intent.invalidation else []
        ),
        "intent_payload": intent.to_dict(),
        "final_action": final_action,
        "rejection_reason": rejection,
    }


def _position(intent, portfolio, decision_id, risk):
    assert intent.entry and intent.invalidation
    plan = intent.exit_plan
    return {
        "market": intent.market,
        "account_id": portfolio.account_id,
        "symbol": intent.symbol,
        "strategy_id": intent.strategy_id,
        "strategy_version": intent.strategy_version,
        "profile_hash": intent.profile_hash,
        "status": "pending_entry",
        "entry_thesis": intent.thesis,
        "invalidation_conditions": list(intent.invalidation.conditions),
        "entry_price": risk.approved_price,
        "initial_stop_price": intent.invalidation.hard_stop_price,
        "current_stop_price": intent.invalidation.hard_stop_price,
        "target_plan": [asdict(target) for target in plan.targets] if plan else [],
        "trailing_stop": asdict(plan.trailing_stop) if plan and plan.trailing_stop else {},
        "max_holding_until": (
            plan.max_holding_until.isoformat() if plan and plan.max_holding_until else None
        ),
        "initial_risk_amount": risk.risk_amount,
        "current_risk_amount": risk.risk_amount,
        "last_decision_id": decision_id,
        "last_evaluated_at": intent.created_at.isoformat(),
    }


def _order(key, decision_id, position_id, intent, risk):
    buying = intent.action in {TradeAction.ENTER_LONG, TradeAction.ADD}
    order_type = (
        intent.entry.order.order_type.value if intent.entry else OrderType.MARKET.value
    )
    return {
        "client_order_key": key,
        "decision_id": decision_id,
        "position_id": position_id,
        "market": intent.market,
        "symbol": intent.symbol,
        "strategy_id": intent.strategy_id,
        "action": "buy" if buying else "sell",
        "order_type": order_type,
        "requested_qty": risk.quantity,
        "requested_price": risk.approved_price,
        "status": "intent_created",
        "expires_at": intent.valid_until.isoformat(),
    }


def _adapter_error(intent_id, position_id, exc):
    return IntentResult(
        intent_id, None, position_id, None, "adapter_error", (type(exc).__name__,)
    )


def _key(*parts):
    raw = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _int(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
