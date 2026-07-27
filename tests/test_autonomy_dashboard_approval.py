import unittest
from unittest.mock import patch

from src.dashboard import core
from src.dashboard.routes import stock


class AutonomyDashboardApprovalTests(unittest.TestCase):
    @patch(
        "src.strategy.autonomy.ai_stock_integration.approve_managed_ai_stock_order",
        return_value={
            "id": 7,
            "managed_order_id": 11,
            "status": "approved",
            "order_status": "approved",
        },
    )
    @patch("src.online_access.is_online_access_blocked", return_value=False)
    @patch.object(
        core,
        "_load_pending_approval",
        return_value={"id": 7, "managed_order_id": 11, "status": "pending"},
    )
    @patch.object(core, "_claim_pending_approval")
    @patch.object(core, "_get_api")
    def test_managed_approval_never_uses_legacy_broker_path(
        self, get_api, claim, pending, online, approve
    ):
        result = core._approve_pending_approval(7)

        self.assertEqual(result["status"], "approved")
        approve.assert_called_once_with(7)
        claim.assert_not_called()
        get_api.assert_not_called()

    @patch(
        "src.strategy.autonomy.ai_stock_integration.reject_managed_ai_stock_order",
        return_value={
            "id": 7,
            "managed_order_id": 11,
            "status": "rejected",
            "order_status": "rejected",
        },
    )
    @patch.object(
        stock,
        "_load_pending_approval",
        return_value={"id": 7, "managed_order_id": 11, "status": "pending"},
    )
    def test_managed_rejection_updates_canonical_order(
        self, pending, reject
    ):
        result = stock.reject_order(7)

        self.assertEqual(result["status"], "rejected")
        reject.assert_called_once_with(7)


if __name__ == "__main__":
    unittest.main()
