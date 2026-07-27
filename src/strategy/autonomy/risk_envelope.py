"""Deterministic, fail-closed risk gate for autonomous trade intents.

This module deliberately has no dependency on an AI model or a concrete
TradeIntent class.  Callers may pass a mapping or an object with matching
attributes.  The returned quantity is authoritative: downstream execution
must never use the quantity proposed by the strategy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


INCREASE_ACTIONS = frozenset({"enter_long", "buy", "add"})
REDUCE_ACTIONS = frozenset({"reduce", "exit", "sell"})
SUPPORTED_ACTIONS = INCREASE_ACTIONS | REDUCE_ACTIONS | frozenset(
    {"hold", "watch", "cancel_pending"}
)


@dataclass(frozen=True)
class RiskLimits:
    """Operator-owned limits that a strategy cannot override."""

    max_risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_position_pct: float
    max_market_exposure_pct: float
    max_sector_exposure_pct: float
    max_liquidity_participation_pct: float
    max_strategy_exposure_pct: float
    max_total_open_risk_pct: float
    max_data_age_seconds: int
    allowed_regimes: frozenset[str]
    max_daily_orders: int = 3
    min_cash_reserve_pct: float = 0.0


@dataclass(frozen=True)
class RiskSnapshot:
    """Trusted account and market state used by the deterministic gate."""

    total_equity: float
    available_cash: float
    daily_pnl: float
    position_value: float
    market_exposure_value: float
    sector_exposure_value: float
    strategy_exposure_value: float
    reserved_symbol_exposure_value: float
    reserved_market_exposure_value: float
    reserved_sector_exposure_value: float
    reserved_strategy_exposure_value: float
    sector_key: str
    average_daily_trading_value: float
    open_position_risk_amount_excluding_reservations: float
    current_position_qty: int
    market_regime: str
    data_as_of: datetime
    evaluated_at: datetime
    kill_switch_active: bool
    account_snapshot_available: bool = True
    current_price: float | None = None
    protection_global_block: bool = False
    daily_new_risk_orders: int = 0


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    action: str
    quantity: int
    approved_price: float | None
    risk_budget: float
    risk_amount: float
    account_risk_reservation_limit: float
    exposure_reservation_limits: Mapping[str, float]
    estimated_cost: float
    binding_cap: str | None
    caps: Mapping[str, int] = field(default_factory=dict)
    checks: Mapping[str, bool] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()


class RiskEnvelope:
    """Evaluate intents using immutable limits and trusted state.

    Risk-increasing actions require every safety input.  Risk-reducing actions
    are allowed during a kill switch or loss halt, but cannot exceed the
    currently held quantity.  Unknown actions and malformed values are denied.
    """

    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def evaluate(self, intent: Any, snapshot: RiskSnapshot) -> RiskDecision:
        raw_action = _get(intent, "action", "")
        action = str(getattr(raw_action, "value", raw_action) or "").strip().lower()
        if action not in SUPPORTED_ACTIONS:
            return self._deny(action, "unsupported_action")
        if action in {"hold", "watch", "cancel_pending"}:
            return RiskDecision(
                approved=True,
                action=action,
                quantity=0,
                approved_price=None,
                risk_budget=0.0,
                risk_amount=0.0,
                account_risk_reservation_limit=0.0,
                exposure_reservation_limits={},
                estimated_cost=0.0,
                binding_cap=None,
            )
        if action in REDUCE_ACTIONS:
            return self._evaluate_reduction(intent, snapshot, action)
        return self._evaluate_increase(intent, snapshot, action)

    def _evaluate_reduction(
        self, intent: Any, snapshot: RiskSnapshot, action: str
    ) -> RiskDecision:
        held = _nonnegative_int(snapshot.current_position_qty)
        reduce_pct = _positive_float(
            _get(intent, "reduce_pct", 100.0 if action == "exit" else None)
        )
        price = _positive_float(_price(intent)) or _positive_float(snapshot.current_price)
        reasons: list[str] = []
        if held <= 0:
            reasons.append("no_position_to_reduce")
        if reduce_pct is None or reduce_pct > 100:
            reasons.append("invalid_reduce_pct")
        if action == "exit" and reduce_pct != 100:
            reasons.append("exit_requires_100_pct")
        if price is None:
            reasons.append("invalid_price")
        if reasons:
            return self._deny(action, *reasons)
        assert reduce_pct is not None
        quantity = held if action == "exit" else math.floor(held * reduce_pct / 100.0)
        if quantity <= 0:
            return self._deny(action, "reduction_below_one_share")
        return RiskDecision(
            approved=True,
            action=action,
            quantity=quantity,
            approved_price=price,
            risk_budget=0.0,
            risk_amount=0.0,
            account_risk_reservation_limit=0.0,
            exposure_reservation_limits={},
            estimated_cost=round(quantity * price, 2),
            binding_cap="current_position",
            caps={"current_position": held},
            checks={"position_available": True, "price_valid": True},
        )

    def _evaluate_increase(
        self, intent: Any, snapshot: RiskSnapshot, action: str
    ) -> RiskDecision:
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        def check(name: str, condition: bool) -> None:
            checks[name] = bool(condition)
            if not condition:
                reasons.append(name)

        entry = _positive_float(_price(intent))
        stop = _positive_float(_stop_price(intent))
        requested_raw = _get(intent, "quantity", None)
        requested = (
            _positive_int(requested_raw) if requested_raw is not None else None
        )
        total = _positive_float(snapshot.total_equity)
        cash = _nonnegative_float(snapshot.available_cash)
        position_value = _nonnegative_float(snapshot.position_value)
        market_value = _nonnegative_float(snapshot.market_exposure_value)
        sector_value = _nonnegative_float(snapshot.sector_exposure_value)
        strategy_value = _nonnegative_float(snapshot.strategy_exposure_value)
        reserved_symbol = _nonnegative_float(snapshot.reserved_symbol_exposure_value)
        reserved_market = _nonnegative_float(snapshot.reserved_market_exposure_value)
        reserved_sector = _nonnegative_float(snapshot.reserved_sector_exposure_value)
        reserved_strategy = _nonnegative_float(snapshot.reserved_strategy_exposure_value)
        adv = _positive_float(snapshot.average_daily_trading_value)
        open_risk = _nonnegative_float(
            snapshot.open_position_risk_amount_excluding_reservations
        )

        check("account_snapshot_available", bool(snapshot.account_snapshot_available))
        check("kill_switch_off", not bool(snapshot.kill_switch_active))
        check("all_open_positions_protected", not bool(snapshot.protection_global_block))
        check("entry_price_valid", entry is not None)
        check("stop_price_required", stop is not None)
        check("long_stop_below_entry", entry is not None and stop is not None and stop < entry)
        check("total_equity_valid", total is not None)
        check("cash_valid", cash is not None)
        check("position_exposure_valid", position_value is not None)
        check("market_exposure_valid", market_value is not None)
        check("sector_exposure_valid", sector_value is not None)
        check("strategy_exposure_valid", strategy_value is not None)
        check("reserved_symbol_exposure_valid", reserved_symbol is not None)
        check("reserved_market_exposure_valid", reserved_market is not None)
        check("reserved_sector_exposure_valid", reserved_sector is not None)
        check("reserved_strategy_exposure_valid", reserved_strategy is not None)
        check("sector_key_valid", bool(str(snapshot.sector_key).strip()))
        check("liquidity_data_valid", adv is not None)
        check("open_position_risk_valid", open_risk is not None)
        check(
            "requested_quantity_valid",
            requested_raw is None or requested is not None,
        )
        check("limits_valid", self._limits_valid())
        check("allowed_market_regime", snapshot.market_regime in self.limits.allowed_regimes)
        check("fresh_data", self._fresh(snapshot))
        check("daily_loss_limit", self._within_daily_loss(snapshot))
        daily_orders = _valid_nonnegative_int(snapshot.daily_new_risk_orders)
        check("daily_order_count_valid", daily_orders is not None)
        check(
            "daily_order_limit",
            daily_orders is not None
            and daily_orders < self.limits.max_daily_orders,
        )
        if reasons:
            return self._deny(action, *reasons, checks=checks)

        assert entry is not None and stop is not None and total is not None
        assert cash is not None and position_value is not None
        assert market_value is not None and sector_value is not None and adv is not None
        assert strategy_value is not None
        assert reserved_symbol is not None and reserved_market is not None
        assert reserved_sector is not None and reserved_strategy is not None
        assert open_risk is not None
        risk_budget = total * self.limits.max_risk_per_trade_pct / 100.0
        account_reservation_limit = max(
            0.0,
            total * self.limits.max_total_open_risk_pct / 100.0 - open_risk,
        )
        cash_reserve = total * self.limits.min_cash_reserve_pct / 100.0
        spendable_cash = max(0.0, cash - cash_reserve - reserved_market)
        rooms = {
            "risk": risk_budget / (entry - stop),
            "account_risk": account_reservation_limit / (entry - stop),
            "cash": spendable_cash / entry,
            "position": max(
                0.0,
                total * self.limits.max_position_pct / 100.0
                - position_value
                - reserved_symbol,
            )
            / entry,
            "market": max(
                0.0,
                total * self.limits.max_market_exposure_pct / 100.0
                - market_value
                - reserved_market,
            )
            / entry,
            "sector": max(
                0.0,
                total * self.limits.max_sector_exposure_pct / 100.0
                - sector_value
                - reserved_sector,
            )
            / entry,
            "strategy": max(
                0.0,
                total * self.limits.max_strategy_exposure_pct / 100.0
                - strategy_value
                - reserved_strategy,
            )
            / entry,
            "liquidity": (
                adv * self.limits.max_liquidity_participation_pct / 100.0
            )
            / entry,
        }
        if requested is not None:
            rooms["requested"] = float(requested)
        caps = {name: max(0, math.floor(value)) for name, value in rooms.items()}
        quantity = min(caps.values())
        binding = min(caps, key=caps.get)
        if quantity <= 0:
            checks["quantity_positive"] = False
            return self._deny(action, "quantity_positive", checks=checks, caps=caps)
        checks["quantity_positive"] = True
        return RiskDecision(
            approved=True,
            action=action,
            quantity=quantity,
            approved_price=entry,
            risk_budget=round(risk_budget, 2),
            risk_amount=round((entry - stop) * quantity, 2),
            account_risk_reservation_limit=round(account_reservation_limit, 2),
            exposure_reservation_limits={
                "position": round(
                    max(0.0, total * self.limits.max_position_pct / 100.0 - position_value), 2
                ),
                "market": round(
                    max(0.0, total * self.limits.max_market_exposure_pct / 100.0 - market_value), 2
                ),
                "sector": round(
                    max(0.0, total * self.limits.max_sector_exposure_pct / 100.0 - sector_value), 2
                ),
                "strategy": round(
                    max(0.0, total * self.limits.max_strategy_exposure_pct / 100.0 - strategy_value), 2
                ),
            },
            estimated_cost=round(entry * quantity, 2),
            binding_cap=binding,
            caps=caps,
            checks=checks,
        )

    def _limits_valid(self) -> bool:
        limits = self.limits
        percentages = (
            limits.max_risk_per_trade_pct,
            limits.max_daily_loss_pct,
            limits.max_position_pct,
            limits.max_market_exposure_pct,
            limits.max_sector_exposure_pct,
            limits.max_liquidity_participation_pct,
            limits.max_strategy_exposure_pct,
            limits.max_total_open_risk_pct,
        )
        normalized = tuple(_positive_float(value) for value in percentages)
        reserve = _nonnegative_float(limits.min_cash_reserve_pct)
        max_age = _positive_int(limits.max_data_age_seconds)
        max_daily_orders = _positive_int(limits.max_daily_orders)
        return (
            all(value is not None and value <= 100 for value in normalized)
            and normalized[7] >= normalized[0]
            and reserve is not None
            and reserve < 100
            and max_age is not None
            and max_daily_orders is not None
            and bool(limits.allowed_regimes)
        )

    def _fresh(self, snapshot: RiskSnapshot) -> bool:
        if not isinstance(snapshot.data_as_of, datetime) or not isinstance(
            snapshot.evaluated_at, datetime
        ):
            return False
        try:
            data_at = _as_utc(snapshot.data_as_of)
            evaluated_at = _as_utc(snapshot.evaluated_at)
        except (TypeError, ValueError):
            return False
        age = (evaluated_at - data_at).total_seconds()
        max_age = _positive_int(self.limits.max_data_age_seconds)
        return max_age is not None and 0 <= age <= max_age

    def _within_daily_loss(self, snapshot: RiskSnapshot) -> bool:
        total = _positive_float(snapshot.total_equity)
        pnl = _finite_float(snapshot.daily_pnl)
        if total is None or pnl is None:
            return False
        loss_pct = max(0.0, -pnl) / total * 100.0
        return loss_pct < self.limits.max_daily_loss_pct

    @staticmethod
    def _deny(
        action: str,
        *reasons: str,
        checks: Mapping[str, bool] | None = None,
        caps: Mapping[str, int] | None = None,
    ) -> RiskDecision:
        return RiskDecision(
            approved=False,
            action=action,
            quantity=0,
            approved_price=None,
            risk_budget=0.0,
            risk_amount=0.0,
            account_risk_reservation_limit=0.0,
            exposure_reservation_limits={},
            estimated_cost=0.0,
            binding_cap=None,
            caps=caps or {},
            checks=checks or {},
            reasons=tuple(dict.fromkeys(reasons)),
        )


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _price(intent: Any) -> Any:
    direct = _get(intent, "entry_price", None)
    if direct is not None:
        return direct
    entry = _get(intent, "entry", None)
    return _get(entry, "price", _get(entry, "price_max", None))


def _stop_price(intent: Any) -> Any:
    direct = _get(intent, "stop_price", None)
    if direct is not None:
        return direct
    invalidation = _get(intent, "invalidation", None)
    return _get(invalidation, "hard_stop_price", None)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_float(value: Any) -> float | None:
    number = _finite_float(value)
    return number if number is not None and number > 0 else None


def _nonnegative_float(value: Any) -> float | None:
    number = _finite_float(value)
    return number if number is not None and number >= 0 else None


def _positive_int(value: Any) -> int | None:
    number = _finite_float(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _nonnegative_int(value: Any) -> int:
    number = _finite_float(value)
    if number is None or number < 0 or not number.is_integer():
        return 0
    return int(number)


def _valid_nonnegative_int(value: Any) -> int | None:
    number = _finite_float(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("risk timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
