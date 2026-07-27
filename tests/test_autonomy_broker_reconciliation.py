import unittest

from src.strategy.autonomy.broker_adapters import (
    KRBrokerGateway,
    ManagedOrderReconciler,
)
from src.strategy.autonomy.order_state import (
    BrokerCancellation,
    BrokerSubmission,
    OrderStateError,
    OrderStatus,
)


def managed(status="submitting", **changes):
    row = {
        "id": 1,
        "client_order_key": "client-1",
        "decision_id": 10,
        "position_id": 20,
        "market": "KR",
        "symbol": "005930",
        "strategy_id": "s1",
        "status": status,
        "action": "buy",
        "requested_qty": 10,
        "requested_price": 1000,
        "filled_qty": 0,
        "average_fill_price": 0,
        "broker_order_id": "B-1",
    }
    row.update(changes)
    return row


class FakeRepository:
    def __init__(self, order):
        self.order = order

    def get_managed_order(self, order_id):
        return dict(self.order) if order_id == self.order["id"] else None

    def list_unsettled_managed_orders(self, **_):
        return [dict(self.order)]


class FakeManagedService:
    def __init__(self, repo):
        self.repo = repo
        self.applied = []

    def require_order(self, order_id):
        return self.repo.get_managed_order(order_id)

    def apply_fill(
        self, order_id, *, fill_qty, fill_price, broker_payload, fill_key=None
    ):
        order = self.repo.order
        old_qty = order["filled_qty"]
        new_qty = old_qty + fill_qty
        order["average_fill_price"] = (
            order["average_fill_price"] * old_qty + fill_price * fill_qty
        ) / new_qty
        order["filled_qty"] = new_qty
        order["status"] = (
            "filled" if new_qty == order["requested_qty"] else "partially_filled"
        )
        self.applied.append((fill_qty, fill_price))
        return {"order_status": order["status"]}

    def transition(self, order_id, *, expected, target, **_):
        if self.repo.order["status"] != expected.value:
            raise AssertionError("CAS mismatch")
        self.repo.order["status"] = target.value
        return dict(self.repo.order)


class BrokerIsolationTests(unittest.TestCase):
    def test_adapter_requires_canonical_submitting_managed_order(self):
        repo = FakeRepository(managed())
        calls = []
        gateway = KRBrokerGateway(
            submitter=lambda order: calls.append(order) or {
                "rt_cd": "0", "output": {"ODNO": "B-2"}
            },
            canceler=lambda order: {"accepted": True},
            query=lambda order: {"status": "submitted"},
            repo=repo,
        )
        result = gateway.submit_order(dict(repo.order))
        self.assertIsInstance(result, BrokerSubmission)
        self.assertEqual(result.broker_order_id, "B-2")
        self.assertEqual(len(calls), 1)

        forged = dict(repo.order, decision_id=999)
        with self.assertRaises(OrderStateError):
            gateway.submit_order(forged)

    def test_adapter_rejects_approved_order_not_claimed_by_service(self):
        repo = FakeRepository(managed(status="approved"))
        gateway = KRBrokerGateway(
            submitter=lambda order: {"rt_cd": "0"},
            canceler=lambda order: {"accepted": True},
            query=lambda order: {},
            repo=repo,
        )
        with self.assertRaises(OrderStateError):
            gateway.submit_order(dict(repo.order))


class ReconciliationTests(unittest.TestCase):
    def test_only_cumulative_fill_delta_is_applied_across_restarts(self):
        repo = FakeRepository(managed(status="submitted"))
        service = FakeManagedService(repo)
        gateway = KRBrokerGateway(
            submitter=lambda order: {},
            canceler=lambda order: {},
            query=lambda order: {
                "status": "partially_filled",
                "cumulative_filled_qty": 4,
                "average_fill_price": 1005,
            },
            repo=repo,
        )
        reconciler = ManagedOrderReconciler(service, gateway, repo)
        first = reconciler.reconcile(1)
        second = reconciler.reconcile(1)
        self.assertEqual(first.applied_fill_qty, 4)
        self.assertEqual(second.applied_fill_qty, 0)
        self.assertEqual(service.applied, [(4, 1005)])

    def test_incremental_price_is_derived_from_cumulative_average(self):
        repo = FakeRepository(
            managed(
                status="partially_filled",
                filled_qty=4,
                average_fill_price=1000,
            )
        )
        service = FakeManagedService(repo)
        gateway = KRBrokerGateway(
            submitter=lambda order: {},
            canceler=lambda order: {},
            query=lambda order: {
                "status": "partially_filled",
                "cumulative_filled_qty": 6,
                "average_fill_price": 1010,
            },
            repo=repo,
        )
        ManagedOrderReconciler(service, gateway, repo).reconcile(1)
        self.assertEqual(service.applied, [(2, 1030)])

    def test_restart_recovery_enumerates_unsettled_orders(self):
        repo = FakeRepository(managed(status="broker_unknown"))
        service = FakeManagedService(repo)
        gateway = KRBrokerGateway(
            submitter=lambda order: {},
            canceler=lambda order: {},
            query=lambda order: {"status": "submitted", "filled_qty": 0},
            repo=repo,
        )
        results = ManagedOrderReconciler(service, gateway, repo).recover_unsettled()
        self.assertEqual(results[0].after, "submitted")
        self.assertEqual(results[0].status, "reconciled")


if __name__ == "__main__":
    unittest.main()
