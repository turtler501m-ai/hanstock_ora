"""Durable hard-stop protection for strategy-owned long positions.

The broker boundary is deliberately a Protocol. This module creates durable
protection intent before broker I/O and treats any uncovered open quantity as
a global blocker for new risk-increasing orders.
"""

from __future__ import annotations

from src.db import ai_execution_repository as execution_repository
from src.db import ai_risk_repository as risk_repository
# Compatibility DI seam for the legacy combined execution/risk repository protocol.
from src.db import ai_autonomy_repository as repository

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Protocol



@dataclass(frozen=True)
class ProtectionAck:
    accepted: bool
    broker_order_id: str
    protected_qty: int
    stop_price: float
    payload: Mapping[str, Any] | None = None
    error: str = ""


@dataclass(frozen=True)
class ProtectionRequest:
    protection_id: int
    position_id: int
    market: str
    account_id: str
    symbol: str
    strategy_id: str
    quantity: int
    stop_price: float
    existing_broker_order_id: str | None = None


@dataclass(frozen=True)
class ProtectionObservation:
    exists: bool
    active: bool
    broker_order_id: str
    protected_qty: int
    stop_price: float
    payload: Mapping[str, Any] | None = None
    error: str = ""


@dataclass(frozen=True)
class ProtectionGateSignal:
    block_new_risk: bool
    reason: str
    uncovered_positions: tuple[Mapping[str, Any], ...] = ()
    alerts: tuple[str, ...] = ()


class ProtectionBroker(Protocol):
    supports_hard_stops: bool

    def submit_hard_stop(self, request: ProtectionRequest) -> ProtectionAck: ...

    def amend_hard_stop(self, request: ProtectionRequest) -> ProtectionAck: ...

    def cancel_hard_stop(self, request: ProtectionRequest) -> ProtectionAck: ...

    def fetch_hard_stop(self, request: ProtectionRequest) -> ProtectionObservation: ...

    def fetch_position_qty(self, *, account_id: str, symbol: str) -> int: ...


class UnavailableProtectionBroker:
    """Explicit fail-closed adapter for a market without hard-stop support."""

    supports_hard_stops = False

    def __init__(self, reason: str = "broker hard-stop protection is unavailable"):
        self.reason = str(reason)

    def _raise(self):
        raise ProtectionError(self.reason)

    def submit_hard_stop(self, request: ProtectionRequest) -> ProtectionAck:
        self._raise()

    def amend_hard_stop(self, request: ProtectionRequest) -> ProtectionAck:
        self._raise()

    def cancel_hard_stop(self, request: ProtectionRequest) -> ProtectionAck:
        self._raise()

    def fetch_hard_stop(self, request: ProtectionRequest) -> ProtectionObservation:
        self._raise()

    def fetch_position_qty(self, *, account_id: str, symbol: str) -> int:
        self._raise()


class PaperProtectionBroker:
    """Deterministic in-memory protection broker for demo/paper assembly."""

    supports_hard_stops = True

    def __init__(self):
        self._orders: dict[int, ProtectionObservation] = {}
        self._requests: dict[int, ProtectionRequest] = {}

    def submit_hard_stop(self, request: ProtectionRequest) -> ProtectionAck:
        return self._upsert(request)

    def amend_hard_stop(self, request: ProtectionRequest) -> ProtectionAck:
        return self._upsert(request)

    def cancel_hard_stop(self, request: ProtectionRequest) -> ProtectionAck:
        self._orders.pop(int(request.position_id), None)
        self._requests.pop(int(request.position_id), None)
        return ProtectionAck(
            True, request.existing_broker_order_id or f"PSTOP-{request.position_id}",
            0, request.stop_price, {"paper": True, "status": "canceled"},
        )

    def fetch_hard_stop(self, request: ProtectionRequest) -> ProtectionObservation:
        return self._orders.get(
            int(request.position_id),
            ProtectionObservation(False, False, "", 0, 0.0, {"paper": True}),
        )

    def fetch_position_qty(self, *, account_id: str, symbol: str) -> int:
        return sum(
            self._orders[position_id].protected_qty
            for position_id, request in self._requests.items()
            if request.account_id == account_id
            and request.symbol == symbol
            and self._orders[position_id].exists
            and self._orders[position_id].active
        )

    def _upsert(self, request: ProtectionRequest) -> ProtectionAck:
        broker_order_id = (
            request.existing_broker_order_id or f"PSTOP-{request.position_id}"
        )
        observation = ProtectionObservation(
            True, True, broker_order_id, int(request.quantity),
            float(request.stop_price), {"paper": True, "status": "active"},
        )
        self._orders[int(request.position_id)] = observation
        self._requests[int(request.position_id)] = request
        return ProtectionAck(
            True, broker_order_id, int(request.quantity), float(request.stop_price),
            observation.payload,
        )


class ProtectionError(RuntimeError):
    pass


class HardStopProtectionService:
    def __init__(
        self,
        *,
        repo: Any = repository,
        alert: Callable[[str], None] | None = None,
    ):
        self.repo = repo
        self.alert = alert

    def protect_entry_fill(
        self,
        *,
        position_id: int,
        filled_qty: int,
        stop_price: float,
        broker: ProtectionBroker,
    ) -> dict[str, Any]:
        """Durably request protection immediately after an entry fill.

        `apply_managed_fill` must already have attributed the fill to the
        strategy position. The requested protection target is the complete
        current open quantity, not merely the latest fill.
        """
        protection = self.request_entry_fill(
            position_id=int(position_id),
            filled_qty=int(filled_qty),
            stop_price=float(stop_price),
        )
        return self.submit_requested(protection, broker)

    def request_entry_fill(
        self,
        *,
        position_id: int,
        filled_qty: int,
        stop_price: float,
    ) -> dict[str, Any]:
        """Persist fill protection without requiring a broker implementation."""
        fill_qty = int(filled_qty)
        if fill_qty <= 0:
            raise ValueError("filled_qty must be positive")
        position = self.repo.get_strategy_position(int(position_id))
        if not position:
            raise ProtectionError("strategy position not found")
        open_qty = int(position.get("remaining_qty") or 0)
        if open_qty <= 0 or fill_qty > open_qty:
            raise ProtectionError("entry fill is inconsistent with strategy position quantity")
        return self.repo.request_position_protection(
            int(position_id),
            required_qty=open_qty,
            stop_price=float(stop_price),
            reason=f"protect entry fill qty={fill_qty}",
        )

    def submit_requested(
        self,
        protection: Mapping[str, Any],
        broker: ProtectionBroker,
        *,
        force_new: bool = False,
    ) -> dict[str, Any]:
        """Submit a previously persisted protection request."""
        request = self._request(protection)
        if force_new:
            request = replace(request, existing_broker_order_id=None)
        try:
            if request.existing_broker_order_id:
                ack = broker.amend_hard_stop(request)
            else:
                ack = broker.submit_hard_stop(request)
        except Exception as exc:
            self.repo.fail_position_protection(
                int(protection["id"]), error=f"broker exception: {exc}"
            )
            self._emit(
                f"하드스톱 제출 예외: position={protection['position_id']}, error={exc}"
            )
            raise ProtectionError("hard-stop broker call failed") from exc
        return self._accept_ack(protection, ack)

    def reconcile_after_sell_fill(
        self,
        *,
        position_id: int,
        broker: ProtectionBroker | None = None,
    ) -> dict[str, Any] | None:
        """Durably shrink protection or request cancellation after a sell fill."""
        position = self.repo.get_strategy_position(int(position_id))
        if not position:
            raise ProtectionError("strategy position not found")
        protection = self.repo.get_position_protection(position_id=int(position_id))
        if not protection:
            return None
        remaining = int(position.get("remaining_qty") or 0)
        if remaining == 0:
            pending = self.repo.request_position_protection_cancel(
                int(protection["id"]), reason="position became flat after sell fill"
            )
            if broker is None:
                return pending
            return self.cancel_after_flat(
                position_id=int(position_id),
                broker=broker,
                reason="position flat after sell fill",
                cancellation_requested=True,
            )
        amended = self.repo.request_position_protection(
            int(position_id),
            required_qty=remaining,
            stop_price=float(protection["current_stop_price"]),
            reason="reduce hard-stop quantity after sell fill",
        )
        if broker is None:
            return amended
        return self.submit_requested(amended, broker)

    def tighten_stop(
        self,
        *,
        position_id: int,
        new_stop_price: float,
        broker: ProtectionBroker,
    ) -> dict[str, Any]:
        """Move a long hard stop upward; lowering it is rejected by the DB."""
        position = self.repo.get_strategy_position(int(position_id))
        if not position or int(position.get("remaining_qty") or 0) <= 0:
            raise ProtectionError("open strategy position not found")
        existing = self.repo.get_position_protection(position_id=int(position_id))
        if not existing or not existing.get("broker_order_id"):
            raise ProtectionError("active broker protection not found")
        protection = self.repo.request_position_protection(
            int(position_id),
            required_qty=int(position["remaining_qty"]),
            stop_price=float(new_stop_price),
            reason="tighten hard stop",
        )
        try:
            ack = broker.amend_hard_stop(self._request(protection))
        except Exception as exc:
            self.repo.fail_position_protection(
                int(protection["id"]), error=f"broker exception: {exc}"
            )
            self._emit(f"하드스톱 변경 예외: position={position_id}, error={exc}")
            raise ProtectionError("hard-stop amendment failed") from exc
        return self._accept_ack(protection, ack)

    def cancel_after_flat(
        self,
        *,
        position_id: int,
        broker: ProtectionBroker,
        reason: str = "position flat",
        cancellation_requested: bool = False,
    ) -> dict[str, Any]:
        """Cancel protection only when strategy-owned open quantity is zero."""
        position = self.repo.get_strategy_position(int(position_id))
        if not position:
            raise ProtectionError("strategy position not found")
        if int(position.get("remaining_qty") or 0) > 0:
            raise ProtectionError("hard stop cannot be canceled while position is open")
        protection = self.repo.get_position_protection(position_id=int(position_id))
        if not protection:
            raise ProtectionError("position protection not found")
        if not cancellation_requested:
            protection = self.repo.request_position_protection_cancel(
                int(protection["id"]), reason=reason
            )
        request = self._request(protection)
        try:
            ack = broker.cancel_hard_stop(request)
        except Exception as exc:
            self._emit(f"하드스톱 취소 예외: position={position_id}, error={exc}")
            raise ProtectionError("hard-stop cancellation failed") from exc
        if not ack.accepted:
            raise ProtectionError(ack.error or "broker rejected hard-stop cancellation")
        return self.repo.cancel_position_protection(
            int(protection["id"]), reason=reason
        )

    def global_gate_signal(self, *, market: str | None = None) -> ProtectionGateSignal:
        """Fail closed when any strategy-owned open quantity is uncovered."""
        try:
            uncovered = tuple(
                self.repo.list_unprotected_strategy_positions(market=market)
            )
        except Exception as exc:
            message = f"보호 상태 조회 실패: {exc}"
            self._emit(message)
            return ProtectionGateSignal(
                block_new_risk=True,
                reason="protection_state_unavailable",
                alerts=(message,),
            )
        if not uncovered:
            return ProtectionGateSignal(False, "all_open_quantities_protected")
        alerts = tuple(
            (
                f"미보호 포지션: position={item.get('position_id')} "
                f"{item.get('market')}:{item.get('symbol')} "
                f"open={item.get('remaining_qty')} protected={item.get('protected_qty')} "
                f"status={item.get('protection_status')}"
            )
            for item in uncovered
        )
        for message in alerts:
            self._emit(message)
        return ProtectionGateSignal(
            block_new_risk=True,
            reason="unprotected_open_quantity",
            uncovered_positions=uncovered,
            alerts=alerts,
        )

    def _accept_ack(
        self,
        protection: Mapping[str, Any],
        ack: ProtectionAck,
    ) -> dict[str, Any]:
        if not ack.accepted:
            error = ack.error or "broker rejected hard-stop protection"
            self.repo.fail_position_protection(
                int(protection["id"]),
                error=error,
                payload=dict(ack.payload or {}),
            )
            self._emit(
                f"하드스톱 보호 실패: position={protection['position_id']}, error={error}"
            )
            raise ProtectionError(error)
        updated = self.repo.activate_position_protection(
            int(protection["id"]),
            broker_order_id=ack.broker_order_id,
            protected_qty=int(ack.protected_qty),
            stop_price=float(ack.stop_price),
            payload=dict(ack.payload or {}),
        )
        if int(updated["protected_qty"]) < int(updated["required_qty"]):
            self._emit(
                f"하드스톱 부분 보호: position={updated['position_id']} "
                f"required={updated['required_qty']} protected={updated['protected_qty']}"
            )
        return updated

    @staticmethod
    def _request(protection: Mapping[str, Any]) -> ProtectionRequest:
        return ProtectionRequest(
            protection_id=int(protection["id"]),
            position_id=int(protection["position_id"]),
            market=str(protection["market"]),
            account_id=str(protection["account_id"]),
            symbol=str(protection["symbol"]),
            strategy_id=str(protection["strategy_id"]),
            quantity=int(protection["required_qty"]),
            stop_price=float(protection["current_stop_price"]),
            existing_broker_order_id=protection.get("broker_order_id"),
        )

    def build_request(self, protection: Mapping[str, Any]) -> ProtectionRequest:
        """Expose the immutable broker request for reconciliation adapters."""
        return self._request(protection)

    def _emit(self, message: str) -> None:
        if self.alert is not None:
            self.alert(message)
