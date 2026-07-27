"""Canonical approval and execution pipeline for autonomous managed orders."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from src.approval_service import (
    ApprovalCreateRequest,
    ApprovalService,
    ApprovalStatusError,
)
from src.db import ai_stock_repository as repository

from .lifecycle import StrategyLifecycleGate
from .ai_planner import trade_intent_from_payload
from .models import TradeAction, TradeIntent
from .order_state import BrokerGateway, ManagedOrderService, OrderStateError, OrderStatus
from .protection import HardStopProtectionService
from .risk_envelope import RiskEnvelope, RiskSnapshot


class ApprovalBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalPlanResult:
    order_id: int
    approval_id: int


class ManagedApprovalPlanService:
    """Advance risk-approved plans to the operator approval queue only.

    This service deliberately has no approve or execute capability.  It is the
    explicit boundary shared by one-shot runtime and continuous orchestration.
    """

    def __init__(
        self,
        bridge: "ManagedApprovalBridge",
        orders: ManagedOrderService,
    ):
        self.bridge = bridge
        self.orders = orders

    def queue_cycle(self, cycle: Any) -> tuple[ApprovalPlanResult, ...]:
        planned: list[ApprovalPlanResult] = []
        for result in tuple(getattr(cycle, "results", ())):
            if (
                getattr(result, "status", None)
                not in {"managed_order_created", "duplicate"}
                or getattr(result, "order_id", None) is None
            ):
                continue
            order_id = int(result.order_id)
            planned.append(self._queue_order(order_id))
        return tuple(planned)

    def queue_runtime_result(self, runtime_result: Any) -> tuple[ApprovalPlanResult, ...]:
        return tuple(
            self._queue_order(int(supplied["id"]))
            for supplied in tuple(getattr(runtime_result, "managed_orders", ()))
        )

    def _queue_order(self, order_id: int) -> ApprovalPlanResult:
        canonical = self.orders.require_order(order_id)
        status = OrderStatus(canonical["status"])
        if status is OrderStatus.INTENT_CREATED:
            canonical = self.orders.mark_risk_approved(order_id)
            status = OrderStatus(canonical["status"])
        if status is OrderStatus.RISK_APPROVED:
            approval_id = int(self.bridge.queue(order_id))
        elif status is OrderStatus.APPROVAL_QUEUED:
            approval_id = int(canonical.get("approval_id") or 0)
            if approval_id <= 0:
                raise ApprovalBridgeError(
                    "approval-queued order has no canonical approval"
                )
        else:
            raise ApprovalBridgeError(
                f"order is not approval-plan eligible: {status.value}"
            )
        return ApprovalPlanResult(order_id, approval_id)


class FreshRiskSnapshotProvider(Protocol):
    def snapshot_for_approval(
        self,
        *,
        order: Mapping[str, Any],
        decision: Mapping[str, Any],
        position: Mapping[str, Any],
        exclude_position_reservation_id: int | None,
    ) -> RiskSnapshot: ...


class ManagedApprovalBridge:
    def __init__(
        self,
        approvals: ApprovalService,
        orders: ManagedOrderService,
        *,
        risk_envelope: RiskEnvelope,
        fresh_risk_snapshots: FreshRiskSnapshotProvider,
        intent_loader: Callable[[Mapping[str, Any]], TradeIntent] = trade_intent_from_payload,
        lifecycle: StrategyLifecycleGate | None = None,
        protection: HardStopProtectionService | None = None,
        repo: Any = repository,
    ):
        self.approvals = approvals
        self.orders = orders
        self.risk_envelope = risk_envelope
        self.fresh_risk_snapshots = fresh_risk_snapshots
        self.intent_loader = intent_loader
        self.lifecycle = lifecycle or StrategyLifecycleGate()
        self.protection = protection or HardStopProtectionService(repo=repo)
        self.repo = repo

    def queue(self, order_id: int) -> int:
        order = self._canonical_order(order_id, OrderStatus.RISK_APPROVED)
        decision, position = self._linked_records(order)
        approval_id = self.approvals.create_approval(
            ApprovalCreateRequest(
                symbol=str(order["symbol"]),
                name=str(position.get("name") or order["symbol"]),
                action=str(order["action"]),
                qty=int(order["requested_qty"]),
                price=float(order.get("requested_price") or 0),
                reason="autonomous managed order",
                source="autonomous_strategy",
                strategy_id=str(order["strategy_id"]),
                strategy_version=int(decision["strategy_version"]),
                profile_hash=str(decision.get("profile_hash") or ""),
                managed_order_id=int(order["id"]),
                decision_id=int(decision["id"]),
                position_id=int(position["id"]),
                client_order_key=str(order["client_order_key"]),
            )
        )
        if not self.repo.bind_managed_order_approval(
            order_id, approval_id=approval_id
        ):
            self.approvals.transition_pending(
                approval_id,
                status="rejected",
                response_msg="managed order approval bind failed",
            )
            self.orders.reject(
                order_id,
                expected=OrderStatus.RISK_APPROVED,
                reason="approval bind failed",
            )
            raise ApprovalBridgeError("managed order approval bind failed")
        try:
            self.orders.queue_approval(order_id)
        except Exception:
            self.approvals.transition_pending(
                approval_id,
                status="rejected",
                response_msg="managed order queue transition failed",
            )
            self.orders.reject(
                order_id,
                expected=OrderStatus.RISK_APPROVED,
                reason="approval queue transition failed",
            )
            raise
        return approval_id

    def approve(self, approval_id: int) -> dict[str, Any]:
        approval = self.approvals.get_pending_approval(approval_id)
        order_id = self._approval_order_id(approval)
        try:
            order = self._canonical_order(order_id, OrderStatus.APPROVAL_QUEUED)
            self._assert_approval_links(approval, order)
            self._revalidate(order, approval)
            approved_order = self.orders.approve(
                order_id, approval_id=approval_id
            )
            self.approvals.transition_pending(
                approval_id,
                status="approved",
                response_msg="canonical autonomous order approved",
            )
            return approved_order
        except Exception as exc:
            current = self.repo.get_managed_order(order_id)
            if current and current.get("status") in {
                OrderStatus.APPROVAL_QUEUED.value,
                OrderStatus.APPROVED.value,
            }:
                self.orders.reject(
                    order_id,
                    expected=OrderStatus(current["status"]),
                    reason=f"approval revalidation failed: {type(exc).__name__}",
                )
            try:
                self.approvals.transition_pending(
                    approval_id,
                    status="rejected",
                    response_msg=f"approval revalidation failed: {type(exc).__name__}",
                )
            except ApprovalStatusError:
                pass
            raise ApprovalBridgeError("managed approval failed closed") from exc

    def reject(self, approval_id: int, *, reason: str) -> dict[str, Any]:
        approval = self.approvals.get_pending_approval(approval_id)
        order_id = self._approval_order_id(approval)
        order = self._canonical_order(order_id, OrderStatus.APPROVAL_QUEUED)
        self._assert_approval_links(approval, order)
        rejected = self.orders.reject(
            order_id, expected=OrderStatus.APPROVAL_QUEUED, reason=reason
        )
        self.approvals.transition_pending(
            approval_id, status="rejected", response_msg=reason
        )
        return rejected

    def revalidate_for_execution(
        self, order: Mapping[str, Any], approval: Any
    ) -> None:
        """Fail closed if trusted account/market state changed after approval."""
        order_id = int(order["id"])
        current = self._canonical_order(order_id, OrderStatus.APPROVED)
        self._assert_approval_links(approval, current)
        try:
            self._revalidate(current, approval)
        except Exception as exc:
            latest = self.repo.get_managed_order(order_id)
            if latest and latest.get("status") == OrderStatus.APPROVED.value:
                self.orders.reject(
                    order_id,
                    expected=OrderStatus.APPROVED,
                    reason=f"pre-submit revalidation failed: {type(exc).__name__}",
                )
            raise ApprovalBridgeError(
                "managed execution revalidation failed closed"
            ) from exc

    def expire(self, approval_id: int, *, now: datetime | None = None) -> bool:
        approval = self.approvals.get_pending_approval(approval_id)
        order_id = self._approval_order_id(approval)
        order = self._canonical_order(order_id, OrderStatus.APPROVAL_QUEUED)
        self._assert_approval_links(approval, order)
        if not self.orders.expire_if_due(
            order_id, expected=OrderStatus.APPROVAL_QUEUED, now=now
        ):
            return False
        self.approvals.transition_pending(
            approval_id, status="expired", response_msg="managed order expired"
        )
        return True

    def _revalidate(self, order: dict[str, Any], approval: Any) -> None:
        decision, position = self._linked_records(order)
        intent_payload = decision.get("intent_payload") or {}
        lifecycle = self.lifecycle.evaluate(
            SimpleNamespace(
                strategy_id=str(order["strategy_id"]),
                strategy_version=int(decision["strategy_version"]),
                profile_hash=str(decision.get("profile_hash") or ""),
                metadata=intent_payload.get("metadata") or {},
                action=(
                    TradeAction.ENTER_LONG
                    if str(order["action"]) == "buy"
                    else TradeAction.EXIT
                ),
            )
        )
        if not lifecycle.allowed:
            raise ApprovalBridgeError("; ".join(lifecycle.reasons))
        if not (decision.get("risk_decision") or {}).get("approved"):
            raise ApprovalBridgeError("decision has no approved deterministic risk result")
        risk = decision["risk_decision"]
        if int(risk.get("quantity") or 0) != int(order["requested_qty"]):
            raise ApprovalBridgeError("managed order quantity differs from risk approval")
        if float(risk.get("approved_price") or 0) != float(
            order.get("requested_price") or 0
        ):
            raise ApprovalBridgeError("managed order price differs from risk approval")
        if (
            str(approval.profile_hash or "") != str(decision.get("profile_hash") or "")
            or approval.strategy_version != int(decision["strategy_version"])
        ):
            raise ApprovalBridgeError("approval strategy version or profile hash mismatch")
        if str(position.get("profile_hash") or "") != str(decision.get("profile_hash") or ""):
            raise ApprovalBridgeError("position profile hash mismatch")
        if int(position["strategy_version"]) != int(decision["strategy_version"]):
            raise ApprovalBridgeError("position strategy version mismatch")
        if str(order["action"]) == "buy":
            reservation = self.repo.get_active_risk_reservation_for_position(
                int(position["id"])
            )
            if not reservation:
                raise ApprovalBridgeError("active risk reservation is missing")
            if (
                int(reservation.get("position_id") or 0) != int(position["id"])
                or str(reservation.get("strategy_id") or "") != str(order["strategy_id"])
                or float(reservation.get("cash_amount") or 0)
                != float(risk.get("estimated_cost") or 0)
                or float(reservation.get("risk_amount") or 0)
                != float(risk.get("risk_amount") or 0)
            ):
                raise ApprovalBridgeError("risk reservation does not match risk approval")
            reservation_id = int(reservation["id"])
        else:
            reservation_id = None

        try:
            intent = self.intent_loader(intent_payload)
            snapshot = self.fresh_risk_snapshots.snapshot_for_approval(
                order=order,
                decision=decision,
                position=position,
                exclude_position_reservation_id=reservation_id,
            )
            latest = self.risk_envelope.evaluate(intent, snapshot)
        except Exception as exc:
            raise ApprovalBridgeError(
                f"latest risk evaluation unavailable: {type(exc).__name__}"
            ) from exc
        if not latest.approved:
            raise ApprovalBridgeError(
                "latest risk denied: " + "; ".join(latest.reasons or ("unknown",))
            )
        requested_qty = int(order["requested_qty"])
        if latest.quantity < requested_qty:
            raise ApprovalBridgeError(
                f"latest risk quantity reduced: {latest.quantity}<{requested_qty}"
            )
        if str(order["action"]) == "sell":
            if latest.action not in {"sell", "reduce", "exit"}:
                raise ApprovalBridgeError("sell is no longer risk-reducing")
            if requested_qty > int(snapshot.current_position_qty):
                raise ApprovalBridgeError("sell exceeds latest strategy-owned quantity")
        elif latest.action not in {"buy", "enter_long", "add"}:
            raise ApprovalBridgeError("buy is no longer risk-increasing")

        if str(order["action"]) == "buy":
            gate = self.protection.global_gate_signal()
            if gate.block_new_risk:
                raise ApprovalBridgeError(f"new risk blocked: {gate.reason}")

    def _linked_records(self, order):
        decision = self.repo.get_strategy_decision(int(order["decision_id"]))
        position = self.repo.get_strategy_position(int(order["position_id"]))
        if not decision or not position:
            raise ApprovalBridgeError("managed order ownership records are missing")
        for record in (decision, position):
            if (
                str(record.get("strategy_id")) != str(order["strategy_id"])
                or str(record.get("symbol")) != str(order["symbol"])
                or str(record.get("market")) != str(order["market"])
            ):
                raise ApprovalBridgeError("managed order ownership mismatch")
        return decision, position

    def _canonical_order(self, order_id, expected):
        order = self.orders.require_order(order_id)
        if OrderStatus(order["status"]) is not expected:
            raise ApprovalBridgeError(
                f"managed order status must be {expected.value}"
            )
        return order

    @staticmethod
    def _approval_order_id(approval) -> int:
        if not approval.managed_order_id:
            raise ApprovalBridgeError("approval is not linked to a managed order")
        return int(approval.managed_order_id)

    @staticmethod
    def _assert_approval_links(approval, order) -> None:
        pairs = (
            (approval.id, order.get("approval_id")),
            (approval.decision_id, order.get("decision_id")),
            (approval.position_id, order.get("position_id")),
            (approval.client_order_key, order.get("client_order_key")),
            (approval.strategy_id, order.get("strategy_id")),
        )
        if any(str(left) != str(right) for left, right in pairs):
            raise ApprovalBridgeError("approval does not match canonical managed order")


class ManagedExecutionCoordinator:
    def __init__(
        self,
        approvals: ApprovalService,
        orders: ManagedOrderService,
        *,
        pre_submit_validator: Callable[[Mapping[str, Any], Any], None] | None = None,
        repo: Any = repository,
    ):
        self.approvals = approvals
        self.orders = orders
        self.pre_submit_validator = pre_submit_validator
        self.repo = repo

    def execute(
        self, approval_id: int, broker: BrokerGateway
    ) -> dict[str, Any]:
        approval = self.approvals.get_approval(approval_id)
        if approval.status != "approved" or not approval.managed_order_id:
            raise ApprovalBridgeError("execution requires an approved managed approval")
        order = self.orders.require_order(int(approval.managed_order_id))
        ManagedApprovalBridge._assert_approval_links(approval, order)
        if OrderStatus(order["status"]) is not OrderStatus.APPROVED:
            raise ApprovalBridgeError("managed order is not approved for execution")
        if self.pre_submit_validator is not None:
            self.pre_submit_validator(order, approval)
        if str(order.get("action")) == "buy":
            protection_broker = getattr(self.orders, "protection_broker", None)
            if (
                protection_broker is None
                or getattr(protection_broker, "supports_hard_stops", True) is not True
            ):
                raise ApprovalBridgeError(
                    "buy execution requires an available protection broker"
                )
        authorization = self.orders._authorize_execution(
            int(order["id"]), approval_id=int(approval.id)
        )
        return self.orders.submit(
            int(order["id"]), broker, authorization=authorization
        )
