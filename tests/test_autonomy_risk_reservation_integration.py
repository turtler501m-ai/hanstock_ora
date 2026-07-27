from datetime import datetime, timezone
import unittest

from src.strategy.autonomy.order_state import ManagedOrderService, OrderStatus


class ReservationRepository:
    def __init__(self, *, order_status, filled_qty=0):
        self.order = {
            "id": 7,
            "status": order_status,
            "position_id": 3,
            "filled_qty": filled_qty,
        }
        self.reservation = {"id": 11, "status": "active", "position_id": 3}

    def get_managed_order(self, order_id):
        return dict(self.order)

    def transition_managed_order(
        self, order_id, *, expected_status, new_status, **_
    ):
        if self.order["status"] != expected_status:
            return False
        self.order["status"] = new_status
        return True

    def get_active_risk_reservation_for_position(self, position_id):
        if self.reservation["status"] == "active" and position_id == 3:
            return dict(self.reservation)
        return None

    def release_risk_reservation(self, reservation_id, *, final_status, reason):
        self.reservation.update(status=final_status, reason=reason)
        return dict(self.reservation)


class RiskReservationOrderIntegrationTests(unittest.TestCase):
    def test_unfilled_cancel_releases_reservation(self):
        repo = ReservationRepository(order_status="approved")
        ManagedOrderService(repo).cancel(7, expected=OrderStatus.APPROVED)
        self.assertEqual(repo.reservation["status"], "released")

    def test_expiry_marks_reservation_expired(self):
        repo = ReservationRepository(order_status="approved")
        repo.order["expires_at"] = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        service = ManagedOrderService(repo)
        service.expire_if_due(
            7,
            expected=OrderStatus.APPROVED,
            now=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(repo.reservation["status"], "expired")

    def test_partially_filled_cancel_consumes_reservation(self):
        repo = ReservationRepository(order_status="partially_filled", filled_qty=2)

        class Broker:
            def cancel_order(self, order):
                from src.strategy.autonomy.order_state import BrokerCancellation
                return BrokerCancellation(True, {})

        ManagedOrderService(repo).cancel(
            7, expected=OrderStatus.PARTIALLY_FILLED, broker=Broker()
        )
        self.assertEqual(repo.reservation["status"], "consumed")


if __name__ == "__main__":
    unittest.main()
