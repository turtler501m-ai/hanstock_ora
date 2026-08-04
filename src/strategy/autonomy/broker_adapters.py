"""Injected KR/US broker adapters and restart-safe order reconciliation."""
from __future__ import annotations

from src.db import ai_execution_repository as execution_repository
# Compatibility DI seam for the legacy combined execution/risk repository protocol.
from src.db import ai_autonomy_repository as repository

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol


from .order_state import (
    BrokerCancellation,
    BrokerSubmission,
    ManagedOrderService,
    OrderStateError,
    OrderStatus,
)


class BrokerObservedStatus(str, Enum):
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    status: BrokerObservedStatus
    cumulative_filled_qty: int
    average_fill_price: float
    payload: Mapping[str, Any]
    reason: str = ""


class ReconcilableBroker(Protocol):
    market: str

    def fetch_order(self, order: Mapping[str, Any]) -> BrokerOrderSnapshot: ...


RawOperation = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class _InjectedBrokerGateway:
    """Base adapter that accepts only canonical managed-order rows."""

    market = ""

    def __init__(
        self,
        *,
        submitter: RawOperation,
        canceler: RawOperation,
        query: RawOperation,
        repo: Any = repository,
    ):
        self._submitter = submitter
        self._canceler = canceler
        self._query = query
        self._repo = repo

    def submit_order(self, order: Mapping[str, Any]) -> BrokerSubmission:
        canonical = self._require_managed(order, OrderStatus.SUBMITTING)
        raw = dict(self._submitter(canonical))
        accepted = _accepted(raw)
        order_id = _broker_order_id(raw)
        unknown = _unknown(raw) or (accepted and not order_id)
        return BrokerSubmission(
            accepted=accepted,
            broker_order_id=order_id,
            payload=raw,
            reason=_message(raw),
            outcome_unknown=unknown,
        )

    def cancel_order(self, order: Mapping[str, Any]) -> BrokerCancellation:
        canonical = self._require_managed(order, OrderStatus.CANCEL_PENDING)
        if not canonical.get("broker_order_id"):
            return BrokerCancellation(
                False, {}, "managed order has no broker order id", outcome_unknown=True
            )
        raw = dict(self._canceler(canonical))
        return BrokerCancellation(
            accepted=_accepted(raw),
            payload=raw,
            reason=_message(raw),
            outcome_unknown=_unknown(raw),
        )

    def fetch_order(self, order: Mapping[str, Any]) -> BrokerOrderSnapshot:
        canonical = self._require_managed(
            order,
            {
                OrderStatus.SUBMITTING,
                OrderStatus.SUBMITTED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.CANCEL_PENDING,
                OrderStatus.BROKER_UNKNOWN,
            },
        )
        raw = dict(self._query(canonical))
        return _snapshot(raw)

    def _require_managed(
        self,
        supplied: Mapping[str, Any],
        expected: OrderStatus | set[OrderStatus],
    ) -> dict[str, Any]:
        order_id = _positive_int(supplied.get("id"))
        if order_id is None:
            raise OrderStateError("broker adapter requires a persisted managed order id")
        canonical = self._repo.get_managed_order(order_id)
        if canonical is None:
            raise OrderStateError("managed order does not exist")
        expected_set = {expected} if isinstance(expected, OrderStatus) else expected
        status = OrderStatus(canonical["status"])
        if status not in expected_set:
            raise OrderStateError(f"managed order is not broker-call eligible: {status.value}")
        if canonical.get("market") != self.market:
            raise OrderStateError("managed order market does not match broker adapter")
        for field in ("client_order_key", "decision_id", "position_id", "requested_qty"):
            if canonical.get(field) in (None, ""):
                raise OrderStateError(f"managed order lacks {field}")
            if str(supplied.get(field)) != str(canonical.get(field)):
                raise OrderStateError(f"managed order {field} does not match persistence")
        return canonical


class KRBrokerGateway(_InjectedBrokerGateway):
    """Korean-stock adapter; inject existing KIS API call wrappers."""

    market = "KR"


class USBrokerGateway(_InjectedBrokerGateway):
    """US-stock adapter; inject existing overseas API call wrappers."""

    market = "US"


@dataclass(frozen=True)
class ReconciliationResult:
    order_id: int
    before: str
    after: str
    applied_fill_qty: int
    status: str
    reason: str = ""


class ManagedOrderReconciler:
    def __init__(
        self,
        service: ManagedOrderService,
        broker: ReconcilableBroker,
        repo: Any = repository,
    ):
        self.service = service
        self.broker = broker
        self.repo = repo

    def reconcile(self, order_id: int) -> ReconciliationResult:
        order = self.service.require_order(order_id)
        before = OrderStatus(order["status"])
        if order.get("market") != self.broker.market:
            raise OrderStateError("broker reconciler market mismatch")
        observed = self.broker.fetch_order(order)
        local_filled = int(order.get("filled_qty") or 0)
        cumulative = observed.cumulative_filled_qty
        if cumulative < local_filled:
            return self._mark_unknown(
                order_id, before, "broker cumulative fill regressed"
            )
        if cumulative > int(order["requested_qty"]):
            return self._mark_unknown(
                order_id, before, "broker cumulative fill exceeds requested quantity"
            )
        delta = cumulative - local_filled
        if delta:
            if observed.average_fill_price <= 0:
                return self._mark_unknown(
                    order_id, before, "positive fill delta lacks average fill price"
                )
            previous_average = float(order.get("average_fill_price") or 0)
            incremental_price = (
                (observed.average_fill_price * cumulative)
                - (previous_average * local_filled)
            ) / delta
            if incremental_price <= 0:
                return self._mark_unknown(
                    order_id, before, "derived incremental fill price is invalid"
                )
            self.service.apply_fill(
                order_id,
                fill_qty=delta,
                fill_price=incremental_price,
                broker_payload=observed.payload,
                fill_key=f"broker-cumulative:{cumulative}",
            )
            current = OrderStatus(self.service.require_order(order_id)["status"])
        else:
            current = before

        target = _target_status(observed.status, cumulative, int(order["requested_qty"]))
        if target is not None and current is not target:
            if target not in self._allowed_targets(current):
                return self._mark_unknown(
                    order_id, current, f"inconsistent broker status {observed.status.value}"
                )
            self.service.transition(
                order_id,
                expected=current,
                target=target,
                reason=observed.reason or "broker reconciliation",
                broker_payload=observed.payload,
            )
            current = target
        return ReconciliationResult(
            order_id, before.value, current.value, delta, "reconciled", observed.reason
        )

    def recover_unsettled(self, *, limit: int = 500) -> tuple[ReconciliationResult, ...]:
        results = []
        orders = self.repo.list_unsettled_managed_orders(
            market=self.broker.market, limit=limit
        )
        for order in orders:
            try:
                results.append(self.reconcile(int(order["id"])))
            except Exception as exc:
                results.append(
                    ReconciliationResult(
                        int(order["id"]),
                        str(order["status"]),
                        str(order["status"]),
                        0,
                        "error",
                        type(exc).__name__,
                    )
                )
        return tuple(results)

    @staticmethod
    def _allowed_targets(status: OrderStatus) -> frozenset[OrderStatus]:
        from .order_state import ALLOWED_TRANSITIONS

        return ALLOWED_TRANSITIONS[status]

    def _mark_unknown(
        self, order_id: int, current: OrderStatus, reason: str
    ) -> ReconciliationResult:
        if current is not OrderStatus.BROKER_UNKNOWN:
            self.service.transition(
                order_id,
                expected=current,
                target=OrderStatus.BROKER_UNKNOWN,
                reason=reason,
                last_error=reason,
            )
        return ReconciliationResult(
            order_id,
            current.value,
            OrderStatus.BROKER_UNKNOWN.value,
            0,
            "inconsistent",
            reason,
        )


def _snapshot(raw: Mapping[str, Any]) -> BrokerOrderSnapshot:
    status_text = str(raw.get("status") or raw.get("order_status") or "").lower()
    aliases = {
        "open": BrokerObservedStatus.SUBMITTED,
        "pending": BrokerObservedStatus.SUBMITTED,
        "submitted": BrokerObservedStatus.SUBMITTED,
        "partial": BrokerObservedStatus.PARTIALLY_FILLED,
        "partially_filled": BrokerObservedStatus.PARTIALLY_FILLED,
        "filled": BrokerObservedStatus.FILLED,
        "done": BrokerObservedStatus.FILLED,
        "canceled": BrokerObservedStatus.CANCELED,
        "cancelled": BrokerObservedStatus.CANCELED,
        "rejected": BrokerObservedStatus.REJECTED,
        "failed": BrokerObservedStatus.REJECTED,
    }
    status = aliases.get(status_text, BrokerObservedStatus.UNKNOWN)
    qty = _parse_nonnegative_int(
        raw.get("cumulative_filled_qty", raw.get("filled_qty", raw.get("tot_ccld_qty", 0)))
    )
    price = _parse_nonnegative_float(
        raw.get("average_fill_price", raw.get("fill_price", raw.get("avg_prvs", 0)))
    )
    if qty is None or price is None:
        status = BrokerObservedStatus.UNKNOWN
        qty = 0
        price = 0.0
    return BrokerOrderSnapshot(status, qty, price, dict(raw), _message(raw))


def _target_status(
    observed: BrokerObservedStatus, cumulative: int, requested: int
) -> OrderStatus | None:
    if cumulative == requested and requested > 0:
        return OrderStatus.FILLED
    if cumulative > 0:
        if observed in {BrokerObservedStatus.CANCELED, BrokerObservedStatus.REJECTED}:
            return OrderStatus.CANCELED
        return OrderStatus.PARTIALLY_FILLED
    return {
        BrokerObservedStatus.SUBMITTED: OrderStatus.SUBMITTED,
        BrokerObservedStatus.CANCELED: OrderStatus.CANCELED,
        BrokerObservedStatus.REJECTED: OrderStatus.REJECTED,
        BrokerObservedStatus.UNKNOWN: OrderStatus.BROKER_UNKNOWN,
    }.get(observed)


def _accepted(raw: Mapping[str, Any]) -> bool:
    if "accepted" in raw:
        return _truthy(raw["accepted"])
    if "ok" in raw:
        return _truthy(raw["ok"])
    return str(raw.get("rt_cd", "")) == "0"


def _unknown(raw: Mapping[str, Any]) -> bool:
    return _truthy(raw.get("outcome_unknown")) or str(raw.get("status", "")).lower() in {
        "unknown", "timeout",
    }


def _broker_order_id(raw: Mapping[str, Any]) -> str | None:
    output = raw.get("output") if isinstance(raw.get("output"), Mapping) else {}
    value = (
        raw.get("broker_order_id")
        or raw.get("order_id")
        or raw.get("ODNO")
        or output.get("ODNO")
        or output.get("odno")
    )
    return str(value).strip() if value not in (None, "") else None


def _message(raw: Mapping[str, Any]) -> str:
    return str(raw.get("reason") or raw.get("message") or raw.get("msg1") or "")


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _parse_nonnegative_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _parse_nonnegative_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result < 0 or result != result or result in {float("inf"), float("-inf")}:
        return None
    return result
