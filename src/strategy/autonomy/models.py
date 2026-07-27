"""Immutable data contracts produced by autonomous strategies.

The models deliberately do not contain an executable quantity.  A strategy
may describe prices and its thesis, but only the deterministic risk layer may
turn an accepted intent into a sized order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class TradeAction(str, Enum):
    WATCH = "watch"
    ENTER_LONG = "enter_long"
    ADD = "add"
    HOLD = "hold"
    REDUCE = "reduce"
    EXIT = "exit"
    CANCEL_PENDING = "cancel_pending"
    SUSPEND_STRATEGY = "suspend_strategy"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    DAY = "day"
    IOC = "ioc"
    FOK = "fok"
    GTC = "gtc"


@dataclass(frozen=True)
class OrderPlan:
    order_type: OrderType
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: float | None = None
    stop_price: float | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class EntryPlan:
    order: OrderPlan
    price_min: float
    price_max: float


@dataclass(frozen=True)
class InvalidationPlan:
    hard_stop_price: float
    conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExitTarget:
    price: float
    reduce_pct: float


@dataclass(frozen=True)
class TrailingStopPlan:
    activate_after_r: float
    atr_multiple: float


@dataclass(frozen=True)
class ExitPlan:
    targets: tuple[ExitTarget, ...] = ()
    trailing_stop: TrailingStopPlan | None = None
    max_holding_until: datetime | None = None


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    strategy_id: str
    strategy_version: int
    profile_hash: str
    symbol: str
    market: str
    action: TradeAction
    confidence: float
    thesis: str
    created_at: datetime
    data_as_of: datetime
    valid_until: datetime
    entry: EntryPlan | None = None
    invalidation: InvalidationPlan | None = None
    exit_plan: ExitPlan | None = None
    position_id: str | None = None
    reduce_pct: float | None = None
    reasons: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        payload = asdict(self)
        payload["action"] = self.action.value
        if self.entry:
            payload["entry"]["order"]["order_type"] = self.entry.order.order_type.value
            payload["entry"]["order"]["time_in_force"] = self.entry.order.time_in_force.value
        return _json_compatible(payload)


def _json_compatible(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_compatible(item) for item in value]
    return value
