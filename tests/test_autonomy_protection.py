import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.db import ai_stock_repository as repo
from src.strategy.autonomy.protection import (
    HardStopProtectionService,
    PaperProtectionBroker,
    ProtectionAck,
    ProtectionError,
    ProtectionRequest,
)


class FakeProtectionBroker:
    def __init__(self, *, protected_qty=None, accepted=True):
        self.protected_qty = protected_qty
        self.accepted = accepted
        self.submitted = []
        self.amended = []
        self.canceled = []

    def _ack(self, request):
        return ProtectionAck(
            accepted=self.accepted,
            broker_order_id="STOP-1",
            protected_qty=(
                request.quantity if self.protected_qty is None else self.protected_qty
            ),
            stop_price=request.stop_price,
            error="" if self.accepted else "rejected",
        )

    def submit_hard_stop(self, request):
        self.submitted.append(request)
        return self._ack(request)

    def amend_hard_stop(self, request):
        self.amended.append(request)
        return self._ack(request)

    def cancel_hard_stop(self, request):
        self.canceled.append(request)
        return self._ack(request)


class PaperProtectionBrokerTest(unittest.TestCase):
    def test_submit_amend_and_cancel_are_confirmed(self):
        broker = PaperProtectionBroker()
        request = ProtectionRequest(
            protection_id=1,
            position_id=2,
            market="KR",
            account_id="A1",
            symbol="005930",
            strategy_id="s1",
            quantity=3,
            stop_price=900.0,
        )
        submitted = broker.submit_hard_stop(request)
        self.assertTrue(submitted.accepted)
        self.assertEqual(submitted.protected_qty, 3)
        self.assertTrue(broker.fetch_hard_stop(request).active)
        self.assertEqual(
            broker.fetch_position_qty(account_id="A1", symbol="005930"), 3
        )
        canceled = broker.cancel_hard_stop(request)
        self.assertTrue(canceled.accepted)
        self.assertEqual(canceled.protected_qty, 0)
        self.assertFalse(broker.fetch_hard_stop(request).exists)


class HardStopProtectionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "protection.sqlite"

        def connect():
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            return closing(conn)

        self.connect = connect
        self.connect_patch = patch.object(repo, "_connect", side_effect=connect)
        self.connect_patch.start()
        with connect() as conn:
            repo.init_ai_stock_tables(conn)
            conn.commit()
        self.position_id = repo.create_strategy_position({
            "market": "KR",
            "account_id": "A1",
            "symbol": "005930",
            "strategy_id": "swing-v1",
            "status": "open",
            "filled_qty": 10,
            "remaining_qty": 10,
            "average_price": 70_000,
            "initial_stop_price": 66_000,
            "current_stop_price": 66_000,
        })
        self.alerts = []
        self.service = HardStopProtectionService(repo=repo, alert=self.alerts.append)

    def tearDown(self):
        self.connect_patch.stop()
        self.temp_dir.cleanup()

    def test_entry_fill_creates_active_protection_for_complete_open_quantity(self):
        broker = FakeProtectionBroker()
        result = self.service.protect_entry_fill(
            position_id=self.position_id,
            filled_qty=10,
            stop_price=66_000,
            broker=broker,
        )
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["required_qty"], 10)
        self.assertEqual(result["protected_qty"], 10)
        self.assertEqual(len(broker.submitted), 1)
        self.assertFalse(self.service.global_gate_signal().block_new_risk)

    def test_partial_broker_protection_blocks_all_new_risk_and_alerts(self):
        broker = FakeProtectionBroker(protected_qty=6)
        result = self.service.protect_entry_fill(
            position_id=self.position_id,
            filled_qty=10,
            stop_price=66_000,
            broker=broker,
        )
        self.assertEqual(result["status"], "partial")
        signal = self.service.global_gate_signal()
        self.assertTrue(signal.block_new_risk)
        self.assertEqual(signal.reason, "unprotected_open_quantity")
        self.assertEqual(signal.uncovered_positions[0]["protected_qty"], 6)
        self.assertTrue(self.alerts)

    def test_missing_protection_blocks_new_risk(self):
        signal = self.service.global_gate_signal()
        self.assertTrue(signal.block_new_risk)
        self.assertEqual(signal.uncovered_positions[0]["protection_status"], "missing")

    def test_stop_cannot_move_down_for_long_position(self):
        broker = FakeProtectionBroker()
        self.service.protect_entry_fill(
            position_id=self.position_id,
            filled_qty=10,
            stop_price=66_000,
            broker=broker,
        )
        with self.assertRaisesRegex(ValueError, "loss-expanding"):
            self.service.tighten_stop(
                position_id=self.position_id,
                new_stop_price=65_000,
                broker=broker,
            )
        self.assertEqual(len(broker.amended), 0)

    def test_protection_cannot_be_canceled_while_position_is_open(self):
        broker = FakeProtectionBroker()
        self.service.protect_entry_fill(
            position_id=self.position_id,
            filled_qty=10,
            stop_price=66_000,
            broker=broker,
        )
        with self.assertRaisesRegex(ProtectionError, "cannot be canceled"):
            self.service.cancel_after_flat(
                position_id=self.position_id,
                broker=broker,
            )
        self.assertEqual(len(broker.canceled), 0)

    def test_broker_rejection_is_persisted_and_blocks_new_risk(self):
        broker = FakeProtectionBroker(accepted=False)
        with self.assertRaisesRegex(ProtectionError, "rejected"):
            self.service.protect_entry_fill(
                position_id=self.position_id,
                filled_qty=10,
                stop_price=66_000,
                broker=broker,
            )
        stored = repo.get_position_protection(position_id=self.position_id)
        self.assertEqual(stored["status"], "failed")
        self.assertTrue(self.service.global_gate_signal().block_new_risk)
        events = repo.list_position_protection_events(stored["id"])
        self.assertEqual(events[-1]["event_type"], "broker_protection_failed")

    def test_sell_fill_quantity_reduction_is_durable_and_blocks_until_amended(self):
        broker = FakeProtectionBroker()
        self.service.protect_entry_fill(
            position_id=self.position_id,
            filled_qty=10,
            stop_price=66_000,
            broker=broker,
        )
        with self.connect() as conn:
            conn.execute(
                "UPDATE ai_strategy_positions SET remaining_qty=6 WHERE id=?",
                (self.position_id,),
            )
            conn.commit()
        pending = self.service.reconcile_after_sell_fill(position_id=self.position_id)
        self.assertEqual(pending["required_qty"], 6)
        self.assertEqual(pending["protected_qty"], 10)
        self.assertEqual(pending["status"], "amend_pending")
        self.assertTrue(self.service.global_gate_signal().block_new_risk)

    def test_flat_sell_fill_creates_durable_cancel_request_without_broker(self):
        broker = FakeProtectionBroker()
        self.service.protect_entry_fill(
            position_id=self.position_id,
            filled_qty=10,
            stop_price=66_000,
            broker=broker,
        )
        with self.connect() as conn:
            conn.execute(
                "UPDATE ai_strategy_positions SET remaining_qty=0, status='closed' WHERE id=?",
                (self.position_id,),
            )
            conn.commit()
        pending = self.service.reconcile_after_sell_fill(position_id=self.position_id)
        self.assertEqual(pending["status"], "cancel_pending")


if __name__ == "__main__":
    unittest.main()
