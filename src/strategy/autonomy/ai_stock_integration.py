"""Application wiring between AI-stock schedules and the autonomy platform."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Mapping

from src.approval_service import ApprovalService
from src.config import config
from src.db import ai_stock_repository
from src.db.repository import connect_db
from src.db.strategy_repository import load_ai_strategies
from src.repositories import ApprovalRepository

from .approval_bridge import ManagedApprovalBridge, ManagedApprovalPlanService
from .approval_bridge import ManagedExecutionCoordinator
from .broker_adapters import KRBrokerGateway, ManagedOrderReconciler
from .operational_context import OperationalSnapshotProvider
from .order_state import ManagedOrderService, OrderStatus
from .protection import HardStopProtectionService, PaperProtectionBroker
from .risk_envelope import RiskEnvelope, RiskSnapshot
from .runtime import (
    AutonomyRuntime,
    RuntimeConfigurationError,
    _risk_limits,
    build_runtime_contexts,
)


_DEMO_PROTECTION_BROKER = PaperProtectionBroker()


def _autonomy_execution_enabled() -> bool:
    """Allow demo, or real only after every explicit live opt-in is enabled."""
    environment = str(
        getattr(config, "autonomy_trading_env", "demo")
    ).lower()
    trading_environment = str(getattr(config, "trading_env", "demo")).lower()
    if environment == "demo" and trading_environment == "demo":
        return True
    return (
        environment == "real"
        and trading_environment == "real"
        and bool(getattr(config, "enable_live_trading", False))
        and bool(getattr(config, "autonomy_enable_live_trading", False))
        and bool(getattr(config, "autonomy_live_opt_in", False))
    )


class OperationalApprovalSnapshotProvider:
    """Rebuild trusted risk state immediately before a managed approval."""

    def __init__(self, snapshots: OperationalSnapshotProvider):
        self.snapshots = snapshots

    def snapshot_for_approval(
        self,
        *,
        order: Mapping[str, Any],
        decision: Mapping[str, Any],
        position: Mapping[str, Any],
        exclude_position_reservation_id: int | None,
    ) -> RiskSnapshot:
        market = str(order["market"])
        strategy_id = str(order["strategy_id"])
        current = self.snapshots.snapshot(market, strategy_id)
        _, portfolio = build_runtime_contexts(
            market=market,
            strategy_id=strategy_id,
            account_snapshot=current.account,
            market_snapshot=current.market,
            exclude_reservation_id=exclude_position_reservation_id,
        )
        risk = portfolio.risk_snapshot_for(
            str(order["symbol"]),
            position.get("id") if str(order.get("action")) == "sell" else None,
        )
        if risk is None:
            raise RuntimeConfigurationError(
                "fresh approval risk snapshot is unavailable"
            )
        return risk


def build_managed_approval_bridge(
    *,
    strategy_id: str,
    market: str,
    snapshots: OperationalSnapshotProvider | None = None,
    orders: ManagedOrderService | None = None,
) -> tuple[ManagedApprovalBridge, ManagedOrderService]:
    """Build the canonical approval bridge from current strategy configuration."""
    strategy = next(
        (
            item
            for item in load_ai_strategies()
            if str(item.get("id")) == str(strategy_id)
        ),
        None,
    )
    if not strategy:
        raise RuntimeConfigurationError("registered strategy is required")
    policy = ai_stock_repository.get_policy(strategy_id, market)
    profile = strategy.get("profile")
    risk = profile.get("risk") if isinstance(profile, Mapping) else None
    if not policy or not isinstance(profile, Mapping) or not isinstance(risk, Mapping):
        raise RuntimeConfigurationError(
            "strategy policy and complete risk profile are required"
        )
    limits = _risk_limits(policy, profile, risk)
    order_service = orders or ManagedOrderService(
        protection_broker=(
            _DEMO_PROTECTION_BROKER
            if _autonomy_execution_enabled()
            else None
        )
    )
    snapshot_provider = snapshots or OperationalSnapshotProvider()
    protection = HardStopProtectionService(repo=ai_stock_repository)
    bridge = ManagedApprovalBridge(
        ApprovalService(ApprovalRepository(connect_db)),
        order_service,
        risk_envelope=RiskEnvelope(limits),
        fresh_risk_snapshots=OperationalApprovalSnapshotProvider(snapshot_provider),
        protection=protection,
        repo=ai_stock_repository,
    )
    return bridge, order_service


def approve_managed_ai_stock_order(approval_id: int) -> dict[str, Any]:
    """Approve a managed AI-stock order without bypassing fresh risk checks."""
    approvals = ApprovalService(ApprovalRepository(connect_db))
    approval = approvals.get_pending_approval(int(approval_id))
    if not approval.managed_order_id:
        raise RuntimeConfigurationError("approval is not a managed AI-stock order")
    order = ai_stock_repository.get_managed_order(int(approval.managed_order_id))
    if not order:
        raise RuntimeConfigurationError("managed order is missing")
    bridge, orders = build_managed_approval_bridge(
        strategy_id=str(order["strategy_id"]),
        market=str(order["market"]),
    )
    approved = bridge.approve(int(approval_id))
    response = {
        "id": int(approval_id),
        "managed_order_id": int(order["id"]),
        "status": "approved",
        "order_status": str(approved["status"]),
        "response_msg": "managed AI-stock order approved after fresh risk validation",
    }
    if str(order["market"]) == "KR" and _autonomy_execution_enabled():
        from src.api.kis_api import KIStockAPI

        api = KIStockAPI()

        def submitter(canonical):
            return api.place_order(
                str(canonical["symbol"]),
                str(canonical["action"]),
                int(float(canonical.get("requested_price") or 0)),
                int(canonical["requested_qty"]),
            )

        def canceler(canonical):
            return api.cancel_order(
                str(canonical["broker_order_id"]),
                qty=max(
                    0,
                    int(canonical["requested_qty"])
                    - int(canonical.get("filled_qty") or 0),
                ),
                cancel_all=True,
            )

        def query(canonical):
            created = str(canonical.get("created_at") or "")[:10].replace("-", "")
            return api.get_order_snapshot(
                str(canonical["broker_order_id"]),
                order_date=created or None,
            )

        gateway = KRBrokerGateway(
            submitter=submitter,
            canceler=canceler,
            query=query,
            repo=ai_stock_repository,
        )
        submitted = ManagedExecutionCoordinator(
            bridge.approvals,
            orders,
            repo=ai_stock_repository,
        ).execute(int(approval_id), gateway)
        response.update(
            {
                "status": str(submitted["status"]),
                "order_status": str(submitted["status"]),
                "broker_order_id": submitted.get("broker_order_id"),
                "response_msg": (
                    "managed AI-stock order submitted to KIS "
                    f"{getattr(config, 'trading_env', 'demo')}"
                ),
            }
        )
    return response


def reject_managed_ai_stock_order(
    approval_id: int, *, reason: str = "Rejected by dashboard"
) -> dict[str, Any]:
    """Reject both sides of a canonical managed approval."""
    approvals = ApprovalService(ApprovalRepository(connect_db))
    approval = approvals.get_pending_approval(int(approval_id))
    if not approval.managed_order_id:
        raise RuntimeConfigurationError("approval is not a managed AI-stock order")
    order = ai_stock_repository.get_managed_order(int(approval.managed_order_id))
    if not order:
        raise RuntimeConfigurationError("managed order is missing")
    bridge, _ = build_managed_approval_bridge(
        strategy_id=str(order["strategy_id"]),
        market=str(order["market"]),
    )
    rejected = bridge.reject(int(approval_id), reason=reason)
    return {
        "id": int(approval_id),
        "managed_order_id": int(order["id"]),
        "status": "rejected",
        "order_status": str(rejected["status"]),
    }


def cancel_managed_ai_stock_order(order_id: int) -> dict[str, Any]:
    """Cancel a queued or broker-submitted KR demo managed order."""
    order = ai_stock_repository.get_managed_order(int(order_id))
    if not order:
        raise RuntimeConfigurationError("managed order is missing")
    status = OrderStatus(str(order["status"]))
    orders = ManagedOrderService(
        repo=ai_stock_repository,
        protection_broker=_DEMO_PROTECTION_BROKER,
    )
    broker = None
    if status in {
        OrderStatus.SUBMITTING,
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.BROKER_UNKNOWN,
    }:
        if not (
            str(order.get("market")) == "KR"
            and _autonomy_execution_enabled()
        ):
            raise RuntimeConfigurationError(
                "broker cancellation requires an enabled KR autonomy environment"
            )
        from src.api.kis_api import KIStockAPI

        api = KIStockAPI()

        def unavailable_submit(_canonical):
            raise RuntimeConfigurationError("cancel gateway cannot submit orders")

        def canceler(canonical):
            return api.cancel_order(
                str(canonical["broker_order_id"]),
                qty=max(
                    0,
                    int(canonical["requested_qty"])
                    - int(canonical.get("filled_qty") or 0),
                ),
                cancel_all=True,
            )

        def query(canonical):
            created = str(canonical.get("created_at") or "")[:10].replace("-", "")
            return api.get_order_snapshot(
                str(canonical["broker_order_id"]), order_date=created or None
            )

        broker = KRBrokerGateway(
            submitter=unavailable_submit,
            canceler=canceler,
            query=query,
            repo=ai_stock_repository,
        )
    canceled = orders.cancel(int(order_id), expected=status, broker=broker)
    return {
        "id": int(order_id),
        "status": str(canceled["status"]),
        "broker_order_id": canceled.get("broker_order_id"),
        "response_msg": "managed order cancellation processed",
    }


def run_ai_stock_autonomy_cycle(
    *,
    market: str,
    strategy_id: str,
    scan_id: int,
    run_type: str,
    snapshots: OperationalSnapshotProvider | None = None,
    runtime: AutonomyRuntime | None = None,
) -> dict[str, Any]:
    """Run one AI-stock autonomy cycle and queue every approved managed order."""
    approval_required = bool(
        getattr(config, "autonomy_require_approval", True)
    )
    if (
        not approval_required
        and (str(market).upper() != "KR" or not _autonomy_execution_enabled())
    ):
        raise RuntimeConfigurationError(
            "approval-free autonomy requires an enabled KR autonomy environment"
        )
    reconciliation = reconcile_kis_demo_managed_orders(market=market)
    policy = ai_stock_repository.get_policy(strategy_id, market)
    if not policy or not int(policy.get("enabled", 0)):
        raise RuntimeConfigurationError("enabled automation policy is required")
    snapshot_provider = snapshots or OperationalSnapshotProvider()
    engine = runtime or AutonomyRuntime()
    current = snapshot_provider.snapshot(market, strategy_id)
    cycle_key = (
        f"ai-stock:{run_type}:{market}:{strategy_id}:{scan_id}:"
        f"{datetime.now(timezone.utc).isoformat()}"
    )
    result = engine.run(
        cycle_key=cycle_key,
        strategy_id=strategy_id,
        market=market,
        account_snapshot=current.account,
        market_snapshot=current.market,
    )
    queued = ()
    if (
        int(policy.get("automation_level") or 0) >= 5
        and int(policy.get("auto_approve") or 0)
    ):
        bridge, orders = build_managed_approval_bridge(
            strategy_id=strategy_id,
            market=market,
            snapshots=snapshot_provider,
            orders=engine.order_service,
        )
        queued = ManagedApprovalPlanService(bridge, orders).queue_runtime_result(
            result
        )
    executions: list[dict[str, Any]] = []
    if not approval_required:
        for item in queued:
            executions.append(
                approve_managed_ai_stock_order(int(item.approval_id))
            )
    statuses: dict[str, int] = {}
    for item in result.cycle.results:
        statuses[item.status] = statuses.get(item.status, 0) + 1
    return {
        "enabled": True,
        "cycle_key": result.cycle.cycle_key,
        "scanned_intents": result.cycle.scanned_intents,
        "managed_positions": result.cycle.managed_positions,
        "result_counts": statuses,
        "managed_orders": [dict(item) for item in result.managed_orders],
        "approvals": [asdict(item) for item in queued],
        "executions": executions,
        "reconciliation": reconciliation,
    }


def reconcile_kis_demo_managed_orders(*, market: str = "KR") -> list[dict[str, Any]]:
    """Reconcile durable KR orders against the configured KIS environment."""
    market = str(market).upper()
    if market != "KR" or not _autonomy_execution_enabled():
        return []
    unsettled = ai_stock_repository.list_unsettled_managed_orders(
        market="KR", limit=500
    )
    if not unsettled:
        return []
    from src.api.kis_api import KIStockAPI

    api = KIStockAPI()

    def unavailable_submit(_canonical):
        raise RuntimeConfigurationError("reconciliation cannot submit orders")

    def canceler(canonical):
        return api.cancel_order(
            str(canonical["broker_order_id"]),
            qty=max(
                0,
                int(canonical["requested_qty"])
                - int(canonical.get("filled_qty") or 0),
            ),
            cancel_all=True,
        )

    def query(canonical):
        created = str(canonical.get("created_at") or "")[:10].replace("-", "")
        return api.get_order_snapshot(
            str(canonical["broker_order_id"]), order_date=created or None
        )

    broker = KRBrokerGateway(
        submitter=unavailable_submit,
        canceler=canceler,
        query=query,
        repo=ai_stock_repository,
    )
    service = ManagedOrderService(
        repo=ai_stock_repository,
        protection_broker=_DEMO_PROTECTION_BROKER,
    )
    results = ManagedOrderReconciler(
        service, broker, repo=ai_stock_repository
    ).recover_unsettled()
    if any(item.status in {"error", "inconsistent"} for item in results):
        raise RuntimeConfigurationError(
            "KIS demo managed-order reconciliation is incomplete"
        )
    return [
        {
            "order_id": item.order_id,
            "before": item.before,
            "after": item.after,
            "applied_fill_qty": item.applied_fill_qty,
            "status": item.status,
            "reason": item.reason,
        }
        for item in results
    ]
