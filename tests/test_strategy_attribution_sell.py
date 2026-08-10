import tempfile
import unittest
from unittest.mock import patch

from src.dashboard.routes import stock_order


class StrategyAttributionSellTests(unittest.TestCase):
    def test_single_symbol_uses_server_allocation_and_sellable_quantity(self):
        parsed = {
            "holdings": [{
                "symbol": "196170",
                "name": "알테오젠",
                "qty": 29,
                "sellable_qty": 3,
            }]
        }

        def attach(data):
            data["holdings"][0]["strategy_allocations"] = [
                {"strategy_id": "ai_rebalance", "strategy_name": "AI 리밸런싱", "allocated_qty": 4}
            ]
            return data

        with patch.object(stock_order, "_get_api", return_value=object()), patch.object(
            stock_order, "_get_balance_data", return_value={}
        ), patch.object(stock_order, "_parse_balance", return_value=parsed), patch(
            "src.dashboard.routes.account._attach_holding_strategies", side_effect=attach
        ), patch.object(stock_order, "_unsubmitted_dashboard_sell_symbols", return_value=set()):
            orders, skipped = stock_order._strategy_attribution_sell_orders(
                "ai_rebalance", symbol="196170"
            )

        self.assertEqual(skipped, [])
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["qty"], 3)
        self.assertEqual(orders[0]["strategy_id"], "ai_rebalance")
        self.assertEqual(orders[0]["source"], "dashboard_strategy_holding_sell")

    def test_strategy_sell_all_queues_each_attributed_holding(self):
        original_db_path = stock_order.trader.config.trade_db_path
        orders = [
            {
                "symbol": "196170", "name": "알테오젠", "action": "sell", "qty": 4,
                "price": 0, "reason": "strategy sell", "source": "dashboard_strategy_sell_all",
                "strategy_id": "ai_rebalance",
            },
            {
                "symbol": "005930", "name": "삼성전자", "action": "sell", "qty": 2,
                "price": 0, "reason": "strategy sell", "source": "dashboard_strategy_sell_all",
                "strategy_id": "ai_rebalance",
            },
        ]
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                stock_order.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                with patch.object(
                    stock_order, "_strategy_attribution_sell_orders", return_value=(orders, [])
                ), patch.object(stock_order, "_required_env_missing", return_value=[]), patch.object(
                    stock_order, "_auto_approval_enabled", return_value=False
                ), patch.object(stock_order, "_clear_balance_cache"):
                    result = stock_order.sell_all_strategy_attribution({"strategy_id": "ai_rebalance"})

                self.assertEqual(result["created_count"], 2)
                approvals = stock_order.get_approvals()["approvals"]
                self.assertEqual({item["symbol"] for item in approvals}, {"196170", "005930"})
                self.assertTrue(all(item["strategy_id"] == "ai_rebalance" for item in approvals))
        finally:
            stock_order.trader.config.trade_db_path = original_db_path


if __name__ == "__main__":
    unittest.main()
