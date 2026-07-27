"""Fail-closed validation for strategy-authored trade intents."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable

from .models import (
    EntryPlan,
    ExitPlan,
    OrderPlan,
    OrderType,
    TradeAction,
    TradeIntent,
)


class IntentValidationError(ValueError):
    """Raised when an intent is unsafe or internally inconsistent."""

    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


_ENTRY_ACTIONS = {TradeAction.ENTER_LONG, TradeAction.ADD}
_POSITION_ACTIONS = {
    TradeAction.ADD,
    TradeAction.HOLD,
    TradeAction.REDUCE,
    TradeAction.EXIT,
}
_NO_ENTRY_ACTIONS = {
    TradeAction.WATCH,
    TradeAction.HOLD,
    TradeAction.REDUCE,
    TradeAction.EXIT,
    TradeAction.CANCEL_PENDING,
    TradeAction.SUSPEND_STRATEGY,
}


def validate_trade_intent(intent: TradeIntent, *, now: datetime | None = None) -> TradeIntent:
    """Validate an intent and return it unchanged.

    Validation is intentionally explicit instead of being performed only in
    dataclass constructors, so persisted historical intents can be loaded and
    audited even after validation policy evolves.
    """
    errors: list[str] = []

    if not isinstance(intent.action, TradeAction):
        errors.append("action must be a TradeAction")

    for name in (
        "intent_id",
        "strategy_id",
        "profile_hash",
        "symbol",
        "market",
        "thesis",
    ):
        if not str(getattr(intent, name, "")).strip():
            errors.append(f"{name} is required")
    if (
        not isinstance(intent.strategy_version, int)
        or isinstance(intent.strategy_version, bool)
        or intent.strategy_version < 1
    ):
        errors.append("strategy_version must be a positive integer")

    if not _finite(intent.confidence) or not 0.0 <= intent.confidence <= 1.0:
        errors.append("confidence must be finite and between 0 and 1")

    for name in ("created_at", "data_as_of", "valid_until"):
        _require_aware_datetime(getattr(intent, name, None), name, errors)

    if _comparable_datetimes(intent.data_as_of, intent.created_at):
        if intent.data_as_of > intent.created_at:
            errors.append("data_as_of cannot be after created_at")
    if _comparable_datetimes(intent.created_at, intent.valid_until):
        if intent.valid_until <= intent.created_at:
            errors.append("valid_until must be after created_at")
    if now is not None:
        _require_aware_datetime(now, "now", errors)
        if _comparable_datetimes(now, intent.valid_until) and intent.valid_until <= now:
            errors.append("intent has expired")

    if intent.action in _POSITION_ACTIONS and not _text(intent.position_id):
        if intent.action != TradeAction.ENTER_LONG:
            errors.append(f"position_id is required for {intent.action.value}")
    if intent.action == TradeAction.ENTER_LONG and intent.position_id is not None:
        errors.append("enter_long cannot target an existing position")

    if intent.action in _ENTRY_ACTIONS:
        if intent.entry is None:
            errors.append(f"entry plan is required for {intent.action.value}")
        else:
            _validate_entry(intent.entry, errors)
            if (
                intent.entry.order.expires_at is not None
                and _comparable_datetimes(intent.entry.order.expires_at, intent.valid_until)
                and intent.entry.order.expires_at > intent.valid_until
            ):
                errors.append("entry order cannot expire after intent.valid_until")
        if intent.invalidation is None:
            errors.append(f"invalidation plan is required for {intent.action.value}")
        else:
            _validate_invalidation(intent, errors)
        if intent.exit_plan is None:
            errors.append(f"exit plan is required for {intent.action.value}")
    elif intent.action in _NO_ENTRY_ACTIONS and intent.entry is not None:
        errors.append(f"entry plan is not allowed for {intent.action.value}")

    if intent.action == TradeAction.REDUCE and intent.exit_plan is None:
        errors.append("exit plan is required for reduce")
    if intent.action == TradeAction.REDUCE:
        if not _finite(intent.reduce_pct) or not 0 < intent.reduce_pct <= 100:
            errors.append("reduce_pct must be in (0, 100] for reduce")
    elif intent.action == TradeAction.EXIT:
        if intent.reduce_pct != 100:
            errors.append("reduce_pct must be 100 for exit")
    elif intent.reduce_pct is not None:
        errors.append(f"reduce_pct is not allowed for {intent.action.value}")
    if intent.exit_plan is not None:
        _validate_exit_plan(intent.exit_plan, errors)
        if (
            intent.exit_plan.max_holding_until is not None
            and _comparable_datetimes(intent.created_at, intent.exit_plan.max_holding_until)
            and intent.exit_plan.max_holding_until <= intent.created_at
        ):
            errors.append("exit_plan.max_holding_until must be after created_at")

    if intent.action == TradeAction.SUSPEND_STRATEGY and intent.symbol != "*":
        errors.append("suspend_strategy must use symbol '*'")

    forbidden_keys = {"qty", "quantity", "shares", "units", "final_quantity"}
    present = forbidden_keys.intersection({str(key).lower() for key in intent.metadata})
    present.update(forbidden_keys.intersection({str(key).lower() for key in intent.evidence}))
    if present:
        errors.append(
            "strategy intent cannot prescribe executable quantity: "
            + ", ".join(sorted(present))
        )

    if errors:
        raise IntentValidationError(errors)
    return intent


def _validate_entry(entry: EntryPlan, errors: list[str]) -> None:
    if not _positive(entry.price_min):
        errors.append("entry.price_min must be positive and finite")
    if not _positive(entry.price_max):
        errors.append("entry.price_max must be positive and finite")
    if _finite(entry.price_min) and _finite(entry.price_max):
        if entry.price_min > entry.price_max:
            errors.append("entry.price_min cannot exceed entry.price_max")
    _validate_order(entry.order, errors)
    if entry.order.limit_price is not None and _finite(entry.order.limit_price):
        if not entry.price_min <= entry.order.limit_price <= entry.price_max:
            errors.append("limit_price must be inside the entry price range")


def _validate_order(order: OrderPlan, errors: list[str]) -> None:
    if not isinstance(order.order_type, OrderType):
        errors.append("order_type must be an OrderType")
        return
    if order.order_type == OrderType.MARKET:
        if order.limit_price is not None or order.stop_price is not None:
            errors.append("market order cannot contain limit_price or stop_price")
    elif order.order_type == OrderType.LIMIT:
        if not _positive(order.limit_price):
            errors.append("limit order requires a positive finite limit_price")
        if order.stop_price is not None:
            errors.append("limit order cannot contain stop_price")
    elif order.order_type == OrderType.STOP:
        if not _positive(order.stop_price):
            errors.append("stop order requires a positive finite stop_price")
        if order.limit_price is not None:
            errors.append("stop order cannot contain limit_price")
    elif order.order_type == OrderType.STOP_LIMIT:
        if not _positive(order.stop_price) or not _positive(order.limit_price):
            errors.append("stop_limit order requires positive stop_price and limit_price")
    else:
        errors.append("unsupported order_type")
    if order.expires_at is not None:
        _require_aware_datetime(order.expires_at, "entry.order.expires_at", errors)


def _validate_invalidation(intent: TradeIntent, errors: list[str]) -> None:
    assert intent.invalidation is not None
    stop = intent.invalidation.hard_stop_price
    if not _positive(stop):
        errors.append("invalidation.hard_stop_price must be positive and finite")
    if intent.entry and _finite(stop) and _finite(intent.entry.price_min):
        if stop >= intent.entry.price_min:
            errors.append("long hard stop must be below entry.price_min")
    if any(not _text(condition) for condition in intent.invalidation.conditions):
        errors.append("invalidation conditions cannot be blank")


def _validate_exit_plan(plan: ExitPlan, errors: list[str]) -> None:
    total_reduce = 0.0
    previous_price = 0.0
    for index, target in enumerate(plan.targets):
        if not _positive(target.price):
            errors.append(f"exit_plan.targets[{index}].price must be positive and finite")
        if not _finite(target.reduce_pct) or not 0 < target.reduce_pct <= 100:
            errors.append(f"exit_plan.targets[{index}].reduce_pct must be in (0, 100]")
        else:
            total_reduce += target.reduce_pct
        if _finite(target.price) and target.price <= previous_price:
            errors.append("exit target prices must be strictly increasing")
        previous_price = target.price
    if total_reduce > 100.0 + 1e-9:
        errors.append("exit target reduce_pct total cannot exceed 100")
    if plan.trailing_stop is not None:
        if not _positive(plan.trailing_stop.activate_after_r):
            errors.append("trailing_stop.activate_after_r must be positive and finite")
        if not _positive(plan.trailing_stop.atr_multiple):
            errors.append("trailing_stop.atr_multiple must be positive and finite")
    if plan.max_holding_until is not None:
        _require_aware_datetime(
            plan.max_holding_until,
            "exit_plan.max_holding_until",
            errors,
        )
    if not plan.targets and plan.trailing_stop is None and plan.max_holding_until is None:
        errors.append("exit plan must define a target, trailing stop, or time exit")


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value: object) -> bool:
    return _finite(value) and value > 0


def _require_aware_datetime(value: object, name: str, errors: list[str]) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        errors.append(f"{name} must be a timezone-aware datetime")


def _comparable_datetimes(left: object, right: object) -> bool:
    return (
        isinstance(left, datetime)
        and isinstance(right, datetime)
        and left.tzinfo is not None
        and right.tzinfo is not None
        and left.utcoffset() is not None
        and right.utcoffset() is not None
    )
