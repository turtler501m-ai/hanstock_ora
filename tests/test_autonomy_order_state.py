from datetime import datetime, timedelta, timezone
import unittest

from src.strategy.autonomy.order_state import (
    ALLOWED_TRANSITIONS,
    BrokerCancellation,
    BrokerSubmission,
    ConcurrentOrderUpdate,
    ManagedOrderService,
    OrderStateError,
    OrderStatus,
    TERMINAL_STATUSES,
)
from src.strategy.autonomy.protection import UnavailableProtectionBroker


class FakeRepository:
    def __init__(self, status="intent_created", expires_at=None):
        self.order = {
            "id": 1,
            "status": status,
            "expires_at": expires_at,
            "broker_order_id": None,
            "approval_id": 41,
        }
        self.fills = []
        self.reservation = None

    def get_managed_order(self, order_id):
        return dict(self.order) if order_id == 1 else None

    def transition_managed_order(
        self, order_id, *, expected_status, new_status, broker_order_id=None, **kwargs
    ):
        if order_id != 1 or self.order["status"] != expected_status:
            return False
        self.order["status"] = new_status
        if broker_order_id:
            self.order["broker_order_id"] = broker_order_id
        return True

    def apply_managed_fill(self, order_id, **fill):
        self.fills.append(fill)
        self.order["status"] = "partially_filled"
        return {"order_id": order_id, "order_status": "partially_filled"}

    def get_active_risk_reservation_for_position(self, position_id):
        return self.reservation

    def release_risk_reservation(self, reservation_id, **values):
        self.reservation["status"] = values["final_status"]
        return dict(self.reservation)


class AcceptingBroker:
    def __init__(self):
        self.submit_calls = 0

    def submit_order(self, order):
        self.submit_calls += 1
        return BrokerSubmission(True, "broker-1", {"accepted": True})

    def cancel_order(self, order):
        return BrokerCancellation(True, {"canceled": True})


class UnknownBroker:
    def submit_order(self, order):
        raise TimeoutError("timeout after request transmission")

    def cancel_order(self, order):
        return BrokerCancellation(False, {}, outcome_unknown=True)


class ManagedOrderStateTests(unittest.TestCase):
    def test_terminal_states_have_no_outgoing_transitions(self):
        for status in TERMINAL_STATUSES:
            self.assertEqual(ALLOWED_TRANSITIONS[status], frozenset())

    def test_happy_path_uses_compare_and_set(self):
        repo = FakeRepository()
        service = ManagedOrderService(repo)
        service.mark_risk_approved(1)
        service.queue_approval(1)
        service.approve(1, approval_id=41)
        auth = service._authorize_execution(1, approval_id=41)
        order = service.submit(1, AcceptingBroker(), authorization=auth)
        self.assertEqual(order["status"], "submitted")
        self.assertEqual(order["broker_order_id"], "broker-1")

    def test_illegal_transition_is_rejected_before_repository_write(self):
        service = ManagedOrderService(FakeRepository())
        with self.assertRaises(OrderStateError):
            service.transition(
                1,
                expected=OrderStatus.INTENT_CREATED,
                target=OrderStatus.SUBMITTED,
                reason="skip safety gates",
            )

    def test_compare_and_set_conflict_is_explicit(self):
        service = ManagedOrderService(FakeRepository(status="approved"))
        with self.assertRaises(ConcurrentOrderUpdate):
            service.mark_risk_approved(1)

    def test_expired_approved_order_is_not_sent_to_broker(self):
        expires = datetime.now(timezone.utc) - timedelta(seconds=1)
        repo = FakeRepository(status="approved", expires_at=expires.isoformat())
        service = ManagedOrderService(repo)
        auth = service._authorize_execution(1, approval_id=41)
        order = service.submit(1, AcceptingBroker(), authorization=auth)
        self.assertEqual(order["status"], "expired")

    def test_broker_timeout_becomes_unknown_not_rejected(self):
        repo = FakeRepository(status="approved")
        service = ManagedOrderService(repo)
        auth = service._authorize_execution(1, approval_id=41)
        order = service.submit(1, UnknownBroker(), authorization=auth)
        self.assertEqual(order["status"], "broker_unknown")

    def test_second_submitter_cannot_reach_broker(self):
        repo = FakeRepository(status="approved")
        service = ManagedOrderService(repo)
        broker = AcceptingBroker()
        auth = service._authorize_execution(1, approval_id=41)
        service.submit(1, broker, authorization=auth)
        with self.assertRaises(OrderStateError):
            service.submit(1, broker, authorization=auth)
        self.assertEqual(broker.submit_calls, 1)

    def test_submit_rejects_direct_call_without_coordinator_capability(self):
        service = ManagedOrderService(FakeRepository(status="approved"))
        with self.assertRaises(OrderStateError):
            service.submit(1, AcceptingBroker(), authorization=object())

    def test_buy_submit_requires_available_protection_broker(self):
        repo = FakeRepository(status="approved")
        repo.order["action"] = "buy"
        service = ManagedOrderService(repo)
        auth = service._authorize_execution(1, approval_id=41)
        with self.assertRaisesRegex(OrderStateError, "hard-stop protection broker"):
            service.submit(1, AcceptingBroker(), authorization=auth)

    def test_buy_submit_rejects_explicit_unavailable_adapter(self):
        repo = FakeRepository(status="approved")
        repo.order["action"] = "buy"
        service = ManagedOrderService(
            repo, protection_broker=UnavailableProtectionBroker("unsupported")
        )
        auth = service._authorize_execution(1, approval_id=41)
        with self.assertRaisesRegex(OrderStateError, "hard-stop protection broker"):
            service.submit(1, AcceptingBroker(), authorization=auth)

    def test_fill_is_only_accepted_after_submission(self):
        service = ManagedOrderService(FakeRepository(status="approved"))
        with self.assertRaises(OrderStateError):
            service.apply_fill(1, fill_qty=1, fill_price=100)


if __name__ == "__main__":
    unittest.main()
