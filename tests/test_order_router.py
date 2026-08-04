import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.config import config
from src.strategy import router


class OrderRouterTests(unittest.TestCase):
    def test_explicit_execution_context_is_independent_from_global_config(self):
        context = SimpleNamespace(
            dry_run=True,
            trading_env="demo",
            enable_live_trading=False,
            require_approval=True,
            online_access_blocked=False,
        )
        with patch.object(router.config, "dry_run", False):
            order_router = router.OrderRouter(Mock(), execution_context=context)

        self.assertTrue(order_router.dry_run)
        self.assertEqual(order_router.env, "demo")
        self.assertTrue(order_router.require_approval)

    def _set_config(self, **values):
        original = {key: getattr(config, key) for key in values}
        for key, value in values.items():
            setattr(config, key, value)

        def restore():
            for key, value in original.items():
                setattr(config, key, value)

        self.addCleanup(restore)

    def test_direct_demo_submission_records_broker_tracking_fields(self):
        original = {
            "dry_run": config.dry_run,
            "trading_env": config.trading_env,
            "enable_live_trading": config.enable_live_trading,
            "require_approval": config.require_approval,
        }

        class FakeApi:
            def get_balance(self):
                return {"output1": [{"pdno": "005930", "hldg_qty": "3"}]}

            def place_order(self, symbol, action, price, qty):
                return {"rt_cd": "0", "msg1": "accepted", "output": {"ODNO": "D98765"}}

        saved = []
        try:
            config.dry_run = False
            config.trading_env = "demo"
            config.enable_live_trading = False
            config.require_approval = False

            order_router = router.OrderRouter(FakeApi())
            with patch.object(router, "save_decision_log"), patch.object(
                router, "save_trade", side_effect=lambda *args, **kwargs: saved.append((args, kwargs))
            ):
                result = order_router.route("005930", "Samsung", "sell", 1, 70000, "test", {})

            self.assertTrue(result["ok"])
            self.assertEqual(len(saved), 1)
            kwargs = saved[0][1]
            self.assertEqual(kwargs["broker_result"]["output"]["ODNO"], "D98765")
            self.assertEqual(kwargs["order_status"], "submitted")
            self.assertEqual(kwargs["response_msg"], "accepted")
            self.assertEqual(kwargs["filled_qty"], 0)
            self.assertEqual(kwargs["filled_price"], 0)
            self.assertEqual(kwargs["pre_order_qty"], 3)
        finally:
            config.dry_run = original["dry_run"]
            config.trading_env = original["trading_env"]
            config.enable_live_trading = original["enable_live_trading"]
            config.require_approval = original["require_approval"]

    def test_rate_limit_response_retries_before_recording_success(self):
        self._set_config(
            dry_run=False,
            trading_env="demo",
            enable_live_trading=False,
            require_approval=False,
        )

        class FakeApi:
            def __init__(self):
                self.calls = 0

            def get_balance(self):
                return {"output1": []}

            def place_order(self, symbol, action, price, qty):
                self.calls += 1
                if self.calls == 1:
                    return {"rt_cd": "1", "msg1": "초당 거래건수를 초과하였습니다."}
                return {"rt_cd": "0", "msg1": "accepted"}

        api = FakeApi()
        saved = []
        order_router = router.OrderRouter(api)
        with patch.object(router, "save_decision_log"), patch.object(
            router, "save_trade", side_effect=lambda *args, **kwargs: saved.append((args, kwargs))
        ), patch.object(
            router.time, "sleep"
        ) as sleep_mock:
            result = order_router.route("005930", "Samsung", "buy", 1, 70000, "test", {})

        self.assertTrue(result["ok"])
        self.assertEqual(api.calls, 2)
        self.assertEqual(saved[0][1]["order_status"], "submitted")
        self.assertEqual(saved[0][1]["response_msg"], "accepted")
        sleep_mock.assert_called_once_with(router._RATE_LIMIT_BACKOFF_SECONDS)

    def test_buy_order_skips_pre_order_balance_lookup(self):
        self._set_config(
            dry_run=False,
            trading_env="demo",
            enable_live_trading=False,
            require_approval=False,
        )
        api = Mock()
        api.place_order.return_value = {"rt_cd": "0", "msg1": "accepted"}
        order_router = router.OrderRouter(api)

        with patch.object(router, "save_decision_log"), patch.object(router, "save_trade") as save_trade:
            result = order_router.route("005930", "Samsung", "buy", 1, 70000, "test", {})

        self.assertTrue(result["ok"])
        api.get_balance.assert_not_called()
        self.assertEqual(save_trade.call_args.kwargs["pre_order_qty"], 0)

    def test_require_approval_returns_approval_id(self):
        original = {
            "dry_run": config.dry_run,
            "trading_env": config.trading_env,
            "enable_live_trading": config.enable_live_trading,
            "require_approval": config.require_approval,
            "trade_db_path": config.trade_db_path,
        }

        class FakeApi:
            def get_balance(self):
                return {"output1": []}

        import sqlite3
        import tempfile
        temp_db = tempfile.NamedTemporaryFile(delete=False)
        temp_db.close()
        
        try:
            config.dry_run = False
            config.trading_env = "demo"
            config.enable_live_trading = False
            config.require_approval = True
            config.trade_db_path = temp_db.name

            # Initialize approvals table in temp db
            conn = sqlite3.connect(temp_db.name)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    price INTEGER NOT NULL,
                    reason TEXT,
                    source TEXT,
                    status TEXT NOT NULL,
                    response_msg TEXT
                )
            """)
            conn.close()

            order_router = router.OrderRouter(FakeApi())
            with patch.object(router, "save_decision_log"):
                result = order_router.route("005930", "Samsung", "buy", 1, 70000, "test_reason", {}, strategy_id="plunge_bounce_strategy")

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "pending")
            self.assertIn("approval_id", result)
            self.assertIsNotNone(result["approval_id"])
            
            # Check db content
            conn = sqlite3.connect(temp_db.name)
            cursor = conn.execute("SELECT * FROM approvals WHERE id = ?", (result["approval_id"],))
            row = cursor.fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[3], "005930")
            self.assertEqual(row[4], "Samsung")
            self.assertEqual(row[5], "buy")
            self.assertEqual(row[6], 1)
            self.assertEqual(row[7], 70000)
            self.assertEqual(row[8], "test_reason")
            self.assertEqual(row[10], "pending")
        finally:
            import os
            try:
                os.unlink(temp_db.name)
            except Exception:
                pass
            config.dry_run = original["dry_run"]
            config.trading_env = original["trading_env"]
            config.enable_live_trading = original["enable_live_trading"]
            config.require_approval = original["require_approval"]
            config.trade_db_path = original["trade_db_path"]

    def test_real_environment_without_live_switch_is_rejected(self):
        self._set_config(
            dry_run=False,
            trading_env="real",
            enable_live_trading=False,
            require_approval=False,
        )
        api = Mock()
        order_router = router.OrderRouter(api)

        with patch.object(router, "save_decision_log"), patch.object(router, "save_trade") as save_trade:
            result = order_router.route("005930", "Samsung", "buy", 1, 70000, "test", {})

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rejected")
        api.place_order.assert_not_called()
        save_trade.assert_not_called()

    def test_approval_queue_failure_is_reported(self):
        self._set_config(
            dry_run=False,
            trading_env="demo",
            enable_live_trading=False,
            require_approval=True,
        )
        approval_service = Mock()
        approval_service.queue_approval.side_effect = RuntimeError("database unavailable")
        order_router = router.OrderRouter(Mock(), approval_service=approval_service)

        with patch.object(router, "save_decision_log"):
            result = order_router.route("005930", "Samsung", "buy", 1, 70000, "test", {})

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("approval_id", result)

    def test_online_access_block_rejects_without_order_or_approval(self):
        self._set_config(
            dry_run=True,
            trading_env="demo",
            enable_live_trading=False,
            require_approval=True,
            online_access_blocked=True,
        )
        api = Mock()
        approval_service = Mock()
        order_router = router.OrderRouter(api, approval_service=approval_service)

        with patch.object(router, "save_decision_log"), patch.object(router, "save_trade") as save_trade:
            result = order_router.route("005930", "Samsung", "buy", 1, 70000, "test", {})

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rejected")
        api.place_order.assert_not_called()
        approval_service.queue_approval.assert_not_called()
        save_trade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
