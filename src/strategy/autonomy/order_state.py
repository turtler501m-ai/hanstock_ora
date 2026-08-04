"""Compare-and-set order state machine and abstract broker boundary."""
from __future__ import annotations

from src.db import ai_execution_repository as execution_repository
from src.db import ai_risk_repository as risk_repository
# Compatibility DI seam for the legacy combined execution/risk repository protocol.
from src.db import ai_autonomy_repository as repository

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from .protection import HardStopProtectionService, ProtectionBroker


class OrderStatus(str, Enum):
    INTENT_CREATED = "intent_created"
    RISK_APPROVED = "risk_approved"
    APPROVAL_QUEUED = "approval_queued"
    APPROVED = "approved"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELED = "canceled"
    CANCEL_PENDING = "cancel_pending"
    BROKER_UNKNOWN = "broker_unknown"


TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.CANCELED}
)

ALLOWED_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.INTENT_CREATED: frozenset(
        {
            OrderStatus.RISK_APPROVED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.RISK_APPROVED: frozenset(
        {
            OrderStatus.APPROVAL_QUEUED,
            OrderStatus.APPROVED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.APPROVAL_QUEUED: frozenset(
        {
            OrderStatus.APPROVED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.APPROVED: frozenset(
        {
            OrderStatus.SUBMITTING,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.SUBMITTING: frozenset(
        {
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.BROKER_UNKNOWN,
            OrderStatus.REJECTED,
            OrderStatus.CANCEL_PENDING,
        }
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.BROKER_UNKNOWN,
            OrderStatus.REJECTED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.FILLED,
            OrderStatus.BROKER_UNKNOWN,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.BROKER_UNKNOWN: frozenset(
        {
            OrderStatus.BROKER_UNKNOWN,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {
            OrderStatus.CANCELED,
            OrderStatus.BROKER_UNKNOWN,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
}


@dataclass(frozen=True)
class BrokerSubmission:
    accepted: bool
    broker_order_id: str | None
    payload: Mapping[str, Any]
    reason: str = ""
    outcome_unknown: bool = False


@dataclass(frozen=True)
class BrokerCancellation:
    accepted: bool
    payload: Mapping[str, Any]
    reason: str = ""
    outcome_unknown: bool = False


class BrokerGateway(Protocol):
    """Abstract boundary; this package contains no live broker implementation."""

    def submit_order(self, order: Mapping[str, Any]) -> BrokerSubmission: ...

    def cancel_order(self, order: Mapping[str, Any]) -> BrokerCancellation: ...


class OrderStateError(RuntimeError):
    pass


class ConcurrentOrderUpdate(OrderStateError):
    pass


@dataclass(frozen=True)
class _ExecutionAuthorization:
    order_id: int
    approval_id: int
    secret: object


class ManagedOrderService:
    def __init__(
        self,
        repo: Any = repository,
        *,
        protection_service: HardStopProtectionService | None = None,
        protection_broker: ProtectionBroker | None = None,
    ):
        self.repo = repo
        self.__execution_secret = object()
        self.protection = protection_service or HardStopProtectionService(repo=repo)
        self.protection_broker = protection_broker

    def transition(
        self,
        order_id: int,
        *,
        expected: OrderStatus,
        target: OrderStatus,
        reason: str,
        broker_payload: Mapping[str, Any] | None = None,
        broker_order_id: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        if target not in ALLOWED_TRANSITIONS[expected]:
            raise OrderStateError(f"illegal order transition: {expected.value}->{target.value}")
        changed = self.repo.transition_managed_order(
            order_id,
            expected_status=expected.value,
            new_status=target.value,
            reason=reason,
            broker_payload=dict(broker_payload or {}),
            broker_order_id=broker_order_id,
            last_error=last_error,
        )
        if not changed:
            raise ConcurrentOrderUpdate(
                f"order {order_id} is no longer in expected status {expected.value}"
            )
        order = self.require_order(order_id)
        self._finalize_reservation(order, target, reason)
        return order

    def mark_risk_approved(self, order_id: int) -> dict[str, Any]:
        return self.transition(
            order_id,
            expected=OrderStatus.INTENT_CREATED,
            target=OrderStatus.RISK_APPROVED,
            reason="deterministic risk envelope approved",
        )

    def queue_approval(self, order_id: int) -> dict[str, Any]:
        return self.transition(
            order_id,
            expected=OrderStatus.RISK_APPROVED,
            target=OrderStatus.APPROVAL_QUEUED,
            reason="operator approval required",
        )

    def approve(
        self,
        order_id: int,
        *,
        approval_id: int,
        expected: OrderStatus = OrderStatus.APPROVAL_QUEUED,
    ) -> dict[str, Any]:
        order = self.require_order(order_id)
        if int(order.get("approval_id") or 0) != int(approval_id):
            raise OrderStateError("canonical managed order approval does not match")
        return self.transition(
            order_id,
            expected=expected,
            target=OrderStatus.APPROVED,
            reason="order approved",
        )

    def reject(self, order_id: int, *, expected: OrderStatus, reason: str) -> dict[str, Any]:
        return self.transition(
            order_id,
            expected=expected,
            target=OrderStatus.REJECTED,
            reason=reason,
            last_error=reason,
        )

    def expire_if_due(
        self, order_id: int, *, expected: OrderStatus, now: datetime | None = None
    ) -> bool:
        order = self.require_order(order_id)
        expires_at = _parse_time(order.get("expires_at"))
        current = _aware_utc(now or datetime.now(timezone.utc))
        if expires_at is None or current < expires_at:
            return False
        self.transition(
            order_id,
            expected=expected,
            target=OrderStatus.EXPIRED,
            reason="order validity window expired",
        )
        return True

    def submit(
        self,
        order_id: int,
        broker: BrokerGateway,
        *,
        authorization: _ExecutionAuthorization,
    ) -> dict[str, Any]:
        order = self.require_order(order_id)
        if (
            not isinstance(authorization, _ExecutionAuthorization)
            or authorization.secret is not self.__execution_secret
            or authorization.order_id != int(order_id)
            or int(order.get("approval_id") or 0) != authorization.approval_id
        ):
            raise OrderStateError("execution coordinator authorization is required")
        if OrderStatus(order["status"]) is not OrderStatus.APPROVED:
            raise OrderStateError("only approved orders may be submitted")
        if str(order.get("action")) == "buy" and (
            self.protection_broker is None
            or getattr(self.protection_broker, "supports_hard_stops", True) is not True
        ):
            raise OrderStateError(
                "buy submission requires an available hard-stop protection broker"
            )
        if self.expire_if_due(order_id, expected=OrderStatus.APPROVED):
            return self.require_order(order_id)
        order = self.transition(
            order_id,
            expected=OrderStatus.APPROVED,
            target=OrderStatus.SUBMITTING,
            reason="exclusive broker submission claimed",
        )
        try:
            result = broker.submit_order(order)
        except Exception as exc:
            return self.transition(
                order_id,
                expected=OrderStatus.SUBMITTING,
                target=OrderStatus.BROKER_UNKNOWN,
                reason="broker submission outcome unknown",
                broker_payload={"exception_type": type(exc).__name__},
                last_error=str(exc),
            )
        if result.outcome_unknown or (result.accepted and not result.broker_order_id):
            target = OrderStatus.BROKER_UNKNOWN
        elif result.accepted and result.broker_order_id:
            target = OrderStatus.SUBMITTED
        else:
            target = OrderStatus.REJECTED
        return self.transition(
            order_id,
            expected=OrderStatus.SUBMITTING,
            target=target,
            reason=result.reason or target.value,
            broker_payload=result.payload,
            broker_order_id=result.broker_order_id,
            last_error=(
                None
                if target is OrderStatus.SUBMITTED
                else (result.reason or "broker did not provide a conclusive order result")
            ),
        )

    def _authorize_execution(
        self, order_id: int, *, approval_id: int
    ) -> _ExecutionAuthorization:
        """Internal capability issued only after the approval coordinator checks."""
        order = self.require_order(order_id)
        if (
            OrderStatus(order["status"]) is not OrderStatus.APPROVED
            or int(order.get("approval_id") or 0) != int(approval_id)
        ):
            raise OrderStateError("managed order is not canonically approved")
        return _ExecutionAuthorization(
            int(order_id), int(approval_id), self.__execution_secret
        )

    def cancel(
        self, order_id: int, *, expected: OrderStatus, broker: BrokerGateway | None = None
    ) -> dict[str, Any]:
        order = self.require_order(order_id)
        payload: Mapping[str, Any] = {}
        reason = "order canceled before broker submission"
        target = OrderStatus.CANCELED
        if expected in {
            OrderStatus.SUBMITTED,
            OrderStatus.SUBMITTING,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.BROKER_UNKNOWN,
        }:
            if broker is None:
                raise OrderStateError("broker gateway is required to cancel a submitted order")
            order = self.transition(
                order_id,
                expected=expected,
                target=OrderStatus.CANCEL_PENDING,
                reason="exclusive broker cancellation claimed",
            )
            try:
                result = broker.cancel_order(order)
            except Exception as exc:
                return self.transition(
                    order_id,
                    expected=OrderStatus.CANCEL_PENDING,
                    target=OrderStatus.BROKER_UNKNOWN,
                    reason="broker cancellation outcome unknown",
                    broker_payload={"exception_type": type(exc).__name__},
                    last_error=str(exc),
                )
            payload = result.payload
            reason = result.reason or "broker cancellation"
            if result.outcome_unknown:
                target = OrderStatus.BROKER_UNKNOWN
            elif not result.accepted:
                target = (
                    OrderStatus.PARTIALLY_FILLED
                    if int(order.get("filled_qty") or 0) > 0
                    else OrderStatus.SUBMITTED
                )
        return self.transition(
            order_id,
            expected=(
                OrderStatus.CANCEL_PENDING
                if expected
                in {
                    OrderStatus.SUBMITTING,
                    OrderStatus.SUBMITTED,
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.BROKER_UNKNOWN,
                }
                else expected
            ),
            target=target,
            reason=reason,
            broker_payload=payload,
        )

    def apply_fill(
        self,
        order_id: int,
        *,
        fill_qty: int,
        fill_price: float,
        broker_payload: Mapping[str, Any] | None = None,
        fill_key: str | None = None,
    ) -> dict[str, Any]:
        order = self.require_order(order_id)
        status = OrderStatus(order["status"])
        if status not in {
            OrderStatus.SUBMITTING,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.BROKER_UNKNOWN,
            OrderStatus.CANCEL_PENDING,
        }:
            raise OrderStateError(f"fill is not allowed from {status.value}")
        result = self.repo.apply_managed_fill(
            order_id,
            fill_qty=fill_qty,
            fill_price=fill_price,
            broker_payload=dict(broker_payload or {}),
            fill_key=fill_key,
        )
        protection_result: dict[str, Any] | None = None
        protection_error: str | None = None
        try:
            if str(order.get("action")) == "buy":
                if self.protection_broker is None:
                    raise OrderStateError(
                        "buy fill requires a hard-stop protection broker"
                    )
                position = self.repo.get_strategy_position(int(result["position_id"]))
                if not position or not position.get("current_stop_price"):
                    raise OrderStateError("filled buy position has no hard stop price")
                protection_result = self.protection.request_entry_fill(
                    position_id=int(result["position_id"]),
                    filled_qty=int(fill_qty),
                    stop_price=float(position["current_stop_price"]),
                )
                protection_result = self.protection.submit_requested(
                    protection_result, self.protection_broker
                )
                self._require_full_protection(
                    protection_result,
                    expected_qty=int(position["remaining_qty"]),
                    flat=False,
                )
            elif str(order.get("action")) == "sell":
                if self.protection_broker is None:
                    raise OrderStateError(
                        "sell fill protection reconciliation requires a broker"
                    )
                protection_result = self.protection.reconcile_after_sell_fill(
                    position_id=int(result["position_id"]),
                    broker=self.protection_broker,
                )
                position = self.repo.get_strategy_position(int(result["position_id"]))
                remaining_qty = int(position.get("remaining_qty") or 0)
                self._require_full_protection(
                    protection_result,
                    expected_qty=remaining_qty,
                    flat=remaining_qty == 0,
                )
        except Exception as exc:
            # The fill is authoritative and must not be replayed. Missing or
            # failed protection is exposed by protection_gate_signal(), which
            # blocks every later risk-increasing order until repaired.
            protection_error = f"{type(exc).__name__}: {exc}"
            raise OrderStateError(
                f"authoritative fill requires protection recovery: {protection_error}"
            ) from exc
        result["protection"] = protection_result
        result["protection_error"] = protection_error
        if result.get("order_status") == OrderStatus.FILLED.value:
            self._finalize_reservation(
                self.require_order(order_id),
                OrderStatus.FILLED,
                "order fully filled",
            )
        return result

    @staticmethod
    def _require_full_protection(
        protection: Mapping[str, Any] | None,
        *,
        expected_qty: int,
        flat: bool,
    ) -> None:
        if not protection:
            raise OrderStateError("position protection state is missing")
        status = str(protection.get("status") or "")
        protected_qty = int(protection.get("protected_qty") or 0)
        if flat:
            if status != "canceled" or protected_qty != 0:
                raise OrderStateError("flat position hard stop cancellation is unconfirmed")
            return
        if status != "active" or protected_qty != int(expected_qty):
            raise OrderStateError(
                "position is not protected for its complete open quantity"
            )

    def protection_gate_signal(self):
        return self.protection.global_gate_signal()

    def require_order(self, order_id: int) -> dict[str, Any]:
        order = self.repo.get_managed_order(order_id)
        if order is None:
            raise OrderStateError(f"managed order {order_id} not found")
        return order

    def _finalize_reservation(
        self, order: Mapping[str, Any], status: OrderStatus, reason: str
    ) -> None:
        if status not in TERMINAL_STATUSES:
            return
        position_id = order.get("position_id")
        if not position_id:
            return
        reservation = self.repo.get_active_risk_reservation_for_position(position_id)
        if not reservation:
            return
        filled = int(order.get("filled_qty") or 0)
        if status is OrderStatus.EXPIRED:
            final_status = "expired"
        elif status is OrderStatus.FILLED or filled > 0:
            final_status = "consumed"
        else:
            final_status = "released"
        self.repo.release_risk_reservation(
            int(reservation["id"]),
            final_status=final_status,
            reason=reason,
        )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return _aware_utc(value)
    try:
        return _aware_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        raise OrderStateError("invalid order expiry timestamp") from None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise OrderStateError("order timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
