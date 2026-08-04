import unittest
from unittest.mock import Mock

from src.dashboard.routes import stock_order


class StockOrderRouteStateTests(unittest.TestCase):
    def test_legacy_dependency_refresh_preserves_running_trade_sync_thread(self):
        original_thread = stock_order._trade_sync_thread
        original_lock = stock_order._trade_sync_lock
        running_thread = Mock()
        running_thread.is_alive.return_value = True
        try:
            stock_order._trade_sync_thread = running_thread
            stock_order._refresh_legacy_dependencies()

            self.assertIs(stock_order._trade_sync_thread, running_thread)
            self.assertIs(stock_order._trade_sync_lock, original_lock)
            self.assertTrue(stock_order._trade_sync_thread.is_alive())
        finally:
            stock_order._trade_sync_thread = original_thread


if __name__ == "__main__":
    unittest.main()
