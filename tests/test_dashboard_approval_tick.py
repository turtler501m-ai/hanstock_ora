import unittest
from unittest.mock import Mock, call, patch

from src.dashboard.services import approval_service


class DashboardApprovalTickSizeTest(unittest.TestCase):
    def test_tick_size_error_detection_uses_broker_message(self):
        self.assertTrue(approval_service._is_tick_size_error({"msg1": "호가단위 오류"}))
        self.assertFalse(approval_service._is_tick_size_error({"msg1": "초당 거래건수 초과"}))

    def test_approval_retries_tick_size_error_with_adjusted_price(self):
        api = Mock()
        api.place_order.side_effect = [
            {"rt_cd": "1", "msg1": "호가단위 오류"},
            {"rt_cd": "0", "msg1": "주문 접수", "output": {"ODNO": "123"}},
        ]
        item = {
            "id": 1, "symbol": "204320", "name": "HL만도", "action": "sell",
            "qty": 20, "price": 51_250, "reason": "test", "strategy_id": "ai_rebalance",
        }
        approval_service._refresh_dependencies()
        with patch.object(approval_service, "_get_api", return_value=api), patch.object(
            approval_service, "_claim_pending_approval", return_value=item
        ), patch.object(
            approval_service, "_current_holding_qty_from_balance", return_value=20
        ), patch.object(approval_service.trader, "save_trade") as save_trade, patch.object(
            approval_service.trader, "connect_db"
        ) as connect_db, patch.object(approval_service, "_slack_order"):
            result = approval_service._approve_pending_approval_serialized(1, "test", approval=item)

        self.assertEqual(result["status"], "executed")
        self.assertEqual(api.place_order.call_args_list, [
            call("204320", "sell", 51_250, 20),
            call("204320", "sell", 51_200, 20),
        ])
        self.assertEqual(save_trade.call_args.args[4], 51_200)
        connect_db.return_value.__enter__.return_value.execute.assert_called()


if __name__ == "__main__":
    unittest.main()
