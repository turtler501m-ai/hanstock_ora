from types import SimpleNamespace
import unittest

from src.strategy.autonomy.broker_adapters import ReconciliationResult
from src.strategy.autonomy.protection import (
    ProtectionGateSignal,
    ProtectionObservation,
)
from src.strategy.autonomy.recovery import AutonomousRecoveryService


class Reconciler:
    def __init__(self, status="reconciled"):
        self.status = status

    def recover_unsettled(self):
        return (
            ReconciliationResult(1, "submitted", "submitted", 0, self.status),
        )


class Repo:
    def __init__(self):
        self.positions = [{
            "id": 7,
            "market": "KR",
            "account_id": "A1",
            "symbol": "005930",
            "strategy_id": "alpha",
            "side": "long",
            "status": "open",
            "remaining_qty": 10,
            "current_stop_price": 900,
        }]

    def list_strategy_positions(self, **kwargs):
        return list(self.positions)

    def activate_position_protection(self, protection_id, **kwargs):
        return {
            "id": protection_id,
            "position_id": 7,
            "status": "active",
            "required_qty": 10,
            "protected_qty": kwargs["protected_qty"],
            "current_stop_price": kwargs["stop_price"],
            "broker_order_id": kwargs["broker_order_id"],
        }


class Protection:
    def __init__(self, *, existing=False):
        self.existing = existing
        self.requests = []
        self.submissions = []

    def request_entry_fill(self, **kwargs):
        self.requests.append(kwargs)
        return {
            "id": 3,
            "position_id": kwargs["position_id"],
            "market": "KR",
            "account_id": "A1",
            "symbol": "005930",
            "strategy_id": "alpha",
            "required_qty": 10,
            "protected_qty": 0,
            "current_stop_price": kwargs["stop_price"],
            "status": "failed" if self.existing else "pending",
            "broker_order_id": "OLD" if self.existing else None,
        }

    def build_request(self, protection):
        return SimpleNamespace(position_id=protection["position_id"])

    def submit_requested(self, protection, broker, *, force_new=False):
        self.submissions.append(force_new)
        return {
            **protection,
            "status": "active",
            "protected_qty": protection["required_qty"],
            "broker_order_id": "NEW",
        }

    def global_gate_signal(self, *, market=None):
        return ProtectionGateSignal(False, "clear")


class Broker:
    def __init__(self, observed_exists=False):
        self.observed_exists = observed_exists

    def fetch_hard_stop(self, request):
        return ProtectionObservation(
            exists=self.observed_exists,
            active=self.observed_exists,
            broker_order_id="OLD",
            protected_qty=10,
            stop_price=900,
        )

    def fetch_position_qty(self, *, account_id, symbol):
        return 10


class MismatchedPositionBroker(Broker):
    def fetch_position_qty(self, *, account_id, symbol):
        return 9


class RecoveryServiceTest(unittest.TestCase):
    def test_startup_repairs_missing_protection_request(self):
        protection = Protection(existing=False)
        service = AutonomousRecoveryService(
            order_reconcilers={"KR": Reconciler()},
            protection_brokers={"KR": Broker()},
            protection=protection,
            repo=Repo(),
        )
        signal = service.audit_unprotected_positions("KR")
        self.assertFalse(signal.block_new_risk)
        self.assertEqual(protection.requests[0]["filled_qty"], 10)
        self.assertEqual(protection.submissions, [False])

    def test_missing_broker_stop_forces_fresh_submission(self):
        protection = Protection(existing=True)
        service = AutonomousRecoveryService(
            order_reconcilers={"KR": Reconciler()},
            protection_brokers={"KR": Broker(observed_exists=False)},
            protection=protection,
            repo=Repo(),
        )
        service.audit_unprotected_positions("KR")
        self.assertEqual(protection.submissions, [True])

    def test_incomplete_managed_order_reconciliation_fails_closed(self):
        service = AutonomousRecoveryService(
            order_reconcilers={"KR": Reconciler(status="inconsistent")},
            protection_brokers={"KR": Broker()},
            protection=Protection(),
            repo=Repo(),
        )
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            service.reconcile_open_orders("KR")

    def test_missing_market_adapters_fail_closed(self):
        service = AutonomousRecoveryService(
            order_reconcilers={},
            protection_brokers={},
            protection=Protection(),
            repo=Repo(),
        )
        with self.assertRaisesRegex(RuntimeError, "reconciler unavailable"):
            service.reconcile_open_orders("KR")
        with self.assertRaisesRegex(RuntimeError, "protection broker unavailable"):
            service.audit_unprotected_positions("KR")

    def test_broker_and_virtual_position_mismatch_blocks_new_risk(self):
        service = AutonomousRecoveryService(
            order_reconcilers={"KR": Reconciler()},
            protection_brokers={"KR": MismatchedPositionBroker()},
            protection=Protection(),
            repo=Repo(),
        )
        signal = service.audit_unprotected_positions("KR")
        self.assertTrue(signal.block_new_risk)
        self.assertEqual(signal.reason, "protection_recovery_incomplete")


if __name__ == "__main__":
    unittest.main()
