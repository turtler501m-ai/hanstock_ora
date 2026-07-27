from types import SimpleNamespace
import unittest

from src.strategy.autonomy.approval_bridge import (
    ApprovalPlanResult,
    ApprovalBridgeError,
    ManagedApprovalBridge,
    ManagedApprovalPlanService,
    ManagedExecutionCoordinator,
)
from src.strategy.autonomy.orchestrator import CycleResult, IntentResult
from src.strategy.autonomy.order_state import (
    BrokerSubmission,
    OrderStatus,
)
from src.strategy.autonomy.protection import ProtectionGateSignal


class FakeApprovals:
    def __init__(self):
        self.rows = {}
        self.next_id = 1

    def create_approval(self, request):
        row = SimpleNamespace(
            id=self.next_id, status="pending", response_msg="", **request.__dict__
        )
        self.rows[row.id] = row
        self.next_id += 1
        return row.id

    def get_approval(self, approval_id):
        return self.rows[approval_id]

    def get_pending_approval(self, approval_id):
        row = self.rows[approval_id]
        if row.status != "pending":
            raise ValueError("not pending")
        return row

    def transition_pending(self, approval_id, *, status, response_msg):
        row = self.get_pending_approval(approval_id)
        row.status = status
        row.response_msg = response_msg
        return row


class FakeRepo:
    def __init__(self):
        self.order = {
            "id": 5,
            "client_order_key": "client-5",
            "decision_id": 6,
            "position_id": 7,
            "market": "KR",
            "symbol": "005930",
            "strategy_id": "s1",
            "action": "buy",
            "requested_qty": 3,
            "requested_price": 1000,
            "status": "risk_approved",
            "approval_id": None,
        }
        self.decision = {
            "id": 6,
            "market": "KR",
            "symbol": "005930",
            "strategy_id": "s1",
            "strategy_version": 2,
            "profile_hash": "hash-2",
            "risk_decision": {
                "approved": True,
                "quantity": 3,
                "approved_price": 1000,
                "estimated_cost": 3000,
                "risk_amount": 300,
            },
            "intent_payload": {"metadata": {}},
        }
        self.position = {
            "id": 7,
            "market": "KR",
            "symbol": "005930",
            "strategy_id": "s1",
            "strategy_version": 2,
            "profile_hash": "hash-2",
            "name": "Samsung",
        }
        self.reservation = {
            "id": 8,
            "status": "active",
            "position_id": 7,
            "strategy_id": "s1",
            "cash_amount": 3000,
            "risk_amount": 300,
        }

    def get_managed_order(self, order_id):
        return dict(self.order)

    def bind_managed_order_approval(self, order_id, *, approval_id):
        if self.order["status"] != "risk_approved":
            return False
        self.order["approval_id"] = approval_id
        return True

    def get_strategy_decision(self, decision_id):
        return dict(self.decision)

    def get_strategy_position(self, position_id):
        return dict(self.position)

    def get_active_risk_reservation_for_position(self, position_id):
        return dict(self.reservation) if self.reservation else None


class FakeOrders:
    def __init__(self, repo):
        self.repo = repo
        self.submissions = 0
        self.protection_broker = SimpleNamespace(supports_hard_stops=True)

    def require_order(self, order_id):
        return self.repo.get_managed_order(order_id)

    def mark_risk_approved(self, order_id):
        if self.repo.order["status"] != "intent_created":
            raise ValueError("not intent-created")
        self.repo.order["status"] = "risk_approved"
        return dict(self.repo.order)

    def queue_approval(self, order_id):
        self.repo.order["status"] = "approval_queued"
        return dict(self.repo.order)

    def approve(self, order_id, *, approval_id):
        if self.repo.order["approval_id"] != approval_id:
            raise ValueError("approval mismatch")
        self.repo.order["status"] = "approved"
        return dict(self.repo.order)

    def reject(self, order_id, *, expected, reason):
        self.repo.order["status"] = "rejected"
        self.repo.reservation = None
        return dict(self.repo.order)

    def expire_if_due(self, *args, **kwargs):
        self.repo.order["status"] = "expired"
        self.repo.reservation = None
        return True

    def submit(self, order_id, broker, *, authorization):
        if self.repo.order["status"] != "approved":
            raise ValueError("not approved")
        self.submissions += 1
        return {"status": "submitted"}

    def _authorize_execution(self, order_id, *, approval_id):
        return (order_id, approval_id)


class Lifecycle:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.last_intent = None

    def evaluate(self, intent):
        self.last_intent = intent
        return SimpleNamespace(
            allowed=self.allowed,
            reasons=() if self.allowed else ("suspended",),
        )


class Protection:
    def __init__(self, blocked=False):
        self.blocked = blocked

    def global_gate_signal(self):
        return ProtectionGateSignal(self.blocked, "blocked" if self.blocked else "clear")


class QueueBridge:
    def __init__(self, repo):
        self.repo = repo
        self.queued = []

    def queue(self, order_id):
        self.queued.append(order_id)
        self.repo.order["status"] = "approval_queued"
        self.repo.order["approval_id"] = 91
        return 91


class FreshSnapshots:
    def __init__(self, *, owned_qty=10, fail=False):
        self.owned_qty = owned_qty
        self.fail = fail
        self.excluded = []

    def snapshot_for_approval(self, *, exclude_position_reservation_id, **_):
        if self.fail:
            raise RuntimeError("fresh snapshot unavailable")
        self.excluded.append(exclude_position_reservation_id)
        return SimpleNamespace(current_position_qty=self.owned_qty)


class LatestRisk:
    def __init__(self, *, quantity=3, action="enter_long", approved=True):
        self.quantity = quantity
        self.action = action
        self.approved = approved

    def evaluate(self, intent, snapshot):
        return SimpleNamespace(
            approved=self.approved,
            quantity=self.quantity,
            action=self.action,
            reasons=() if self.approved else ("fresh_data",),
        )


class ApprovalBridgeTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()
        self.orders = FakeOrders(self.repo)
        self.approvals = FakeApprovals()

    def bridge(
        self, *, blocked=False, lifecycle=True, latest=None, snapshots=None
    ):
        return ManagedApprovalBridge(
            self.approvals,
            self.orders,
            risk_envelope=latest or LatestRisk(),
            fresh_risk_snapshots=snapshots or FreshSnapshots(),
            intent_loader=lambda payload: SimpleNamespace(),
            lifecycle=Lifecycle(lifecycle),
            protection=Protection(blocked),
            repo=self.repo,
        )

    def test_approval_plan_advances_cycle_to_queue_without_approving(self):
        self.repo.order["status"] = "intent_created"
        queue_bridge = QueueBridge(self.repo)
        planner = ManagedApprovalPlanService(queue_bridge, self.orders)
        cycle = CycleResult(
            "cycle-1",
            1,
            0,
            (IntentResult("intent-1", 6, 7, 5, "managed_order_created"),),
        )

        result = planner.queue_cycle(cycle)

        self.assertEqual(result, (ApprovalPlanResult(5, 91),))
        self.assertEqual(self.repo.order["status"], "approval_queued")
        self.assertEqual(queue_bridge.queued, [5])

    def test_runtime_approval_plan_is_idempotent_after_queue(self):
        self.repo.order["status"] = "approval_queued"
        self.repo.order["approval_id"] = 91
        queue_bridge = QueueBridge(self.repo)
        planner = ManagedApprovalPlanService(queue_bridge, self.orders)
        runtime_result = SimpleNamespace(
            managed_orders=({"id": 5, "status": "risk_approved"},)
        )

        result = planner.queue_runtime_result(runtime_result)

        self.assertEqual(result, (ApprovalPlanResult(5, 91),))
        self.assertEqual(queue_bridge.queued, [])

    def test_queue_binds_full_canonical_identity(self):
        approval_id = self.bridge().queue(5)
        approval = self.approvals.get_approval(approval_id)
        self.assertEqual(approval.managed_order_id, 5)
        self.assertEqual(approval.decision_id, 6)
        self.assertEqual(approval.position_id, 7)
        self.assertEqual(approval.client_order_key, "client-5")
        self.assertEqual(self.repo.order["status"], "approval_queued")

    def test_approval_revalidates_and_enables_coordinator_only(self):
        approval_id = self.bridge().queue(5)
        snapshots = FreshSnapshots()
        self.bridge(snapshots=snapshots).approve(approval_id)
        self.assertEqual(snapshots.excluded, [8])
        coordinator = ManagedExecutionCoordinator(
            self.approvals, self.orders, repo=self.repo
        )
        result = coordinator.execute(approval_id, object())
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(self.orders.submissions, 1)

    def test_protection_gate_rejects_new_risk_and_releases_reservation(self):
        approval_id = self.bridge(blocked=True).queue(5)
        with self.assertRaises(ApprovalBridgeError):
            self.bridge(blocked=True).approve(approval_id)
        self.assertEqual(self.repo.order["status"], "rejected")
        self.assertEqual(self.approvals.get_approval(approval_id).status, "rejected")
        self.assertIsNone(self.repo.reservation)

    def test_execution_rejects_pending_approval(self):
        approval_id = self.bridge().queue(5)
        coordinator = ManagedExecutionCoordinator(
            self.approvals, self.orders, repo=self.repo
        )
        with self.assertRaises(ApprovalBridgeError):
            coordinator.execute(approval_id, object())

    def test_execution_rejects_buy_when_protection_broker_is_unavailable(self):
        approval_id = self.bridge().queue(5)
        self.bridge(snapshots=FreshSnapshots()).approve(approval_id)
        self.orders.protection_broker = SimpleNamespace(supports_hard_stops=False)
        coordinator = ManagedExecutionCoordinator(
            self.approvals, self.orders, repo=self.repo
        )
        with self.assertRaisesRegex(ApprovalBridgeError, "protection broker"):
            coordinator.execute(approval_id, object())
        self.assertEqual(self.orders.submissions, 0)

    def test_execution_rechecks_fresh_snapshot_and_fails_closed(self):
        approval_id = self.bridge().queue(5)
        self.bridge().approve(approval_id)
        execution_bridge = self.bridge(snapshots=FreshSnapshots(fail=True))
        coordinator = ManagedExecutionCoordinator(
            self.approvals,
            self.orders,
            pre_submit_validator=execution_bridge.revalidate_for_execution,
            repo=self.repo,
        )

        with self.assertRaisesRegex(ApprovalBridgeError, "failed closed"):
            coordinator.execute(approval_id, object())

        self.assertEqual(self.repo.order["status"], "rejected")
        self.assertEqual(self.orders.submissions, 0)

    def test_execution_revalidation_preserves_risk_reducing_sell(self):
        self.repo.order.update(action="sell", status="risk_approved", requested_qty=3)
        self.repo.decision["risk_decision"].update(quantity=3)
        approval_id = self.bridge(
            latest=LatestRisk(quantity=3, action="exit"),
            snapshots=FreshSnapshots(owned_qty=3),
        ).queue(5)
        bridge = self.bridge(
            latest=LatestRisk(quantity=3, action="exit"),
            snapshots=FreshSnapshots(owned_qty=3),
        )
        bridge.approve(approval_id)
        coordinator = ManagedExecutionCoordinator(
            self.approvals,
            self.orders,
            pre_submit_validator=bridge.revalidate_for_execution,
            repo=self.repo,
        )

        result = coordinator.execute(approval_id, object())

        self.assertEqual(result["status"], "submitted")
        self.assertEqual(self.orders.submissions, 1)

    def test_latest_smaller_quantity_rejects_without_mutating_order(self):
        approval_id = self.bridge().queue(5)
        with self.assertRaises(ApprovalBridgeError):
            self.bridge(latest=LatestRisk(quantity=2)).approve(approval_id)
        self.assertEqual(self.repo.order["requested_qty"], 3)
        self.assertEqual(self.repo.order["status"], "rejected")

    def test_fresh_snapshot_failure_is_fail_closed(self):
        approval_id = self.bridge().queue(5)
        with self.assertRaises(ApprovalBridgeError):
            self.bridge(snapshots=FreshSnapshots(fail=True)).approve(approval_id)
        self.assertEqual(self.repo.order["status"], "rejected")

    def test_sell_only_checks_latest_strategy_owned_quantity(self):
        self.repo.order.update(action="sell", status="risk_approved", requested_qty=3)
        self.repo.decision["risk_decision"].update(quantity=3)
        approval_id = self.bridge().queue(5)
        self.bridge(
            latest=LatestRisk(quantity=4, action="exit"),
            snapshots=FreshSnapshots(owned_qty=4),
        ).approve(approval_id)
        self.assertEqual(self.repo.order["status"], "approved")

    def test_approval_lifecycle_receives_action_for_risk_reduction(self):
        self.repo.order["action"] = "sell"
        lifecycle = Lifecycle()
        bridge = ManagedApprovalBridge(
            self.approvals,
            self.orders,
            risk_envelope=LatestRisk(quantity=3, action="exit"),
            fresh_risk_snapshots=FreshSnapshots(owned_qty=3),
            intent_loader=lambda payload: SimpleNamespace(),
            lifecycle=lifecycle,
            protection=Protection(False),
            repo=self.repo,
        )
        approval_id = bridge.queue(5)
        bridge.approve(approval_id)
        self.assertEqual(lifecycle.last_intent.action.value, "exit")


if __name__ == "__main__":
    unittest.main()
