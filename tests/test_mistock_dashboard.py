import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.dashboard import app
from src.dashboard.routes import mistock
from src.config import config as main_config
from src.mistock.config import config as mistock_config
from src.mistock import db as mistock_db
from src.mistock import scheduler as mistock_scheduler
from src.mistock import trader as mistock_trader


class MistockDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = mistock_config.trade_db_path
        self.original_trading_env = mistock_config.trading_env
        self.original_total_capital = mistock_config.total_capital
        self.original_currency = mistock_config.currency
        self.original_split_n = mistock_config.split_n
        self.original_max_positions = mistock_config.max_positions
        self.original_max_single_weight = mistock_config.max_single_weight
        self.original_online_blocked = main_config.online_access_blocked
        object.__setattr__(mistock_config, "trade_db_path", Path(self.tmp.name) / "mistock.sqlite")

    def tearDown(self):
        mistock._mistock_scheduler_run_state.replace({
            "is_running": False, "mode": None, "started_at": None,
            "completed_at": None, "result": None, "error": None, "owner_pid": None,
        })
        object.__setattr__(mistock_config, "trade_db_path", self.original_db_path)
        object.__setattr__(mistock_config, "trading_env", self.original_trading_env)
        object.__setattr__(mistock_config, "total_capital", self.original_total_capital)
        object.__setattr__(mistock_config, "currency", self.original_currency)
        object.__setattr__(mistock_config, "split_n", self.original_split_n)
        object.__setattr__(mistock_config, "max_positions", self.original_max_positions)
        object.__setattr__(mistock_config, "max_single_weight", self.original_max_single_weight)
        main_config.online_access_blocked = self.original_online_blocked
        self.tmp.cleanup()

    def test_mistock_routes_are_registered(self):
        paths = {getattr(route, "path", "") for route in app.routes}

        self.assertIn("/mistock", paths)
        self.assertIn("/api/mistock/balance", paths)
        self.assertIn("/api/mistock/approvals/{approval_id}/approve", paths)
        self.assertIn("/api/mistock/orders/cancel", paths)
        self.assertIn("/api/mistock/orders/revise", paths)

    def test_mistock_uses_separate_runtime_database(self):
        health = mistock.mistock_health()
        watchlist = mistock.mistock_watchlist()

        self.assertTrue(health["ok"])
        self.assertEqual(health["trading_env"], "demo")
        self.assertTrue(str(mistock_config.trade_db_path).endswith("mistock.sqlite"))
        self.assertGreaterEqual(len(watchlist["symbols"]), 1)
        self.assertTrue(mistock_config.trade_db_path.exists())

    def test_paper_approval_executes_against_mistock_holdings(self):
        # 로컬 시뮬 실행 경로(브로커 미경유) 검증 — demo/real이 아닌 환경에서 동작.
        object.__setattr__(mistock_config, "trading_env", "sim")
        mistock_trader.add_watchlist("AAPL", "Apple")
        approval = mistock.mistock_create_approval({
            "symbol": "AAPL",
            "name": "Apple",
            "action": "buy",
            "qty": 2,
            "price": 100,
            "reason": "unit test",
            "source": "test",
        })

        with patch.object(mistock_trader, "quote", return_value={"current": 100.0, "ask1": 100.0, "bid1": 100.0}):
            result = mistock.mistock_approve(approval["id"])
            balance = mistock.mistock_balance()
        trades = mistock.mistock_trades(limit=5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "executed")
        self.assertEqual(balance["cash"], mistock_config.total_capital - 200)
        self.assertEqual(balance["holdings"][0]["symbol"], "AAPL")
        self.assertEqual(len(trades["trades"]), 1)

    def test_mistock_balance_hides_holdings_with_active_sell_approvals(self):
        object.__setattr__(mistock_config, "trading_env", "sim")
        mistock_trader.place_order("AAPL", "buy", 2, 100, reason="seed holding")
        mistock_trader.place_order("MSFT", "buy", 1, 200, reason="seed holding")

        approval = mistock.mistock_create_approval({
            "symbol": "AAPL",
            "name": "Apple",
            "action": "sell",
            "qty": 2,
            "price": 100,
            "reason": "mistock sell current holding",
            "source": "dashboard_holding_sell",
        })

        quotes = {
            "AAPL": {"current": 100.0, "ask1": 100.0, "bid1": 100.0},
            "MSFT": {"current": 200.0, "ask1": 200.0, "bid1": 200.0},
        }
        with patch.object(mistock_trader, "quote", side_effect=lambda symbol: quotes[symbol]):
            balance = mistock.mistock_balance()

        self.assertEqual(approval["status"], "pending")
        self.assertEqual([holding["symbol"] for holding in balance["holdings"]], ["MSFT"])
        self.assertEqual(balance["pending_sell_symbols"], ["AAPL"])

        mistock_db.execute("UPDATE approvals SET status = 'executed' WHERE id = ?", (approval["id"],))
        with patch.object(mistock_trader, "quote", side_effect=lambda symbol: quotes[symbol]):
            balance = mistock.mistock_balance()

        self.assertEqual([holding["symbol"] for holding in balance["holdings"]], ["MSFT"])
        self.assertEqual(balance["pending_sell_symbols"], ["AAPL"])

    def test_demo_balance_does_not_mix_paper_cash_when_kis_cash_is_missing(self):
        class FakeClient:
            def get_overseas_balance(self):
                return {
                    "output1": [
                        {
                            "pdno": "AAPL",
                            "prdt_name": "Apple",
                            "cblc_qty13": "2",
                            "avg_unpr3": "100",
                            "ovrs_now_pric1": "150",
                            "frcr_evlu_amt2": "300",
                            "evlu_pfls_amt2": "100",
                        }
                    ],
                    "output2": {},
                    "output3": {},
                }

        object.__setattr__(mistock_config, "trading_env", "demo")
        with patch.object(mistock_trader, "_get_kis_client", return_value=FakeClient()):
            balance = mistock_trader.get_balance()

        self.assertEqual(balance["cash"], 0.0)
        self.assertEqual(balance["stock_eval"], 300.0)
        self.assertEqual(balance["total_eval"], 300.0)

    def test_demo_balance_derives_cash_from_broker_total_when_cash_is_missing(self):
        class FakeClient:
            def get_overseas_balance(self):
                return {
                    "output1": [
                        {
                            "pdno": "MSFT",
                            "prdt_name": "Microsoft",
                            "cblc_qty13": "1",
                            "avg_unpr3": "200",
                            "ovrs_now_pric1": "250",
                            "frcr_evlu_amt2": "250",
                        }
                    ],
                    "output2": {"tot_asst_amt": "1,000"},
                    "output3": {},
                }

        object.__setattr__(mistock_config, "trading_env", "demo")
        with patch.object(mistock_trader, "_get_kis_client", return_value=FakeClient()):
            balance = mistock_trader.get_balance()

        self.assertEqual(balance["cash"], 750.0)
        self.assertEqual(balance["stock_eval"], 250.0)
        self.assertEqual(balance["total_eval"], 1000.0)
        self.assertEqual(balance["broker_total_eval"], 1000.0)

    def test_demo_balance_uses_config_capital_when_kis_account_is_empty(self):
        class FakeClient:
            def get_overseas_balance(self):
                return {
                    "output1": [],
                    "output2": [],
                    "output3": {
                        "dncl_amt": "0",
                        "tot_dncl_amt": "0",
                        "tot_asst_amt": "0",
                        "frcr_use_psbl_amt": "0.00",
                    },
                    "rt_cd": "0",
                    "msg1": "mock account has no rows",
                }

        object.__setattr__(mistock_config, "trading_env", "demo")
        object.__setattr__(mistock_config, "total_capital", 5000.0)
        object.__setattr__(mistock_config, "currency", "USD")
        with patch.object(mistock_trader, "_get_kis_client", return_value=FakeClient()):
            balance = mistock_trader.get_balance()

        self.assertEqual(balance["cash"], 5000.0)
        self.assertEqual(balance["total_eval"], 5000.0)
        self.assertEqual(balance["balance_source"], "demo_config_fallback")

    def test_demo_order_success_updates_dashboard_before_broker_balance_catches_up(self):
        class FakeClient:
            def get_overseas_balance(self):
                return {
                    "output1": [],
                    "output2": {},
                    "output3": {"tot_asst_amt": "0", "frcr_use_psbl_amt": "0.00"},
                    "rt_cd": "0",
                }

            def place_overseas_order(self, symbol, action, price, qty):
                return {"rt_cd": "0", "msg1": "order accepted"}

        object.__setattr__(mistock_config, "trading_env", "demo")
        original_dry_run = mistock_config.dry_run
        object.__setattr__(mistock_config, "dry_run", False)
        object.__setattr__(mistock_config, "total_capital", 1000.0)
        object.__setattr__(mistock_config, "currency", "USD")

        try:
            with patch.object(mistock_trader, "_get_kis_client", return_value=FakeClient()):
                order = mistock_trader.place_order("AAPL", "buy", 2, 100, reason="unit test")
                balance = mistock.mistock_balance()
        finally:
            object.__setattr__(mistock_config, "dry_run", original_dry_run)

        self.assertTrue(order["ok"])
        self.assertEqual(balance["balance_source"], "demo_local_shadow")
        self.assertEqual(balance["cash"], 800.0)
        self.assertEqual(balance["stock_eval"], 200.0)
        self.assertEqual(balance["total_eval"], 1000.0)
        self.assertEqual(balance["holdings"][0]["symbol"], "AAPL")
        self.assertEqual(balance["holdings"][0]["qty"], 2.0)
        self.assertEqual(balance["holdings"][0]["source"], "local_shadow")

    def test_demo_sell_calls_kis_order_api(self):
        calls = []
        class FakeClient:
            def get_overseas_balance(self):
                return {
                    "output1": [],
                    "output2": {},
                    "output3": {"tot_asst_amt": "0", "frcr_use_psbl_amt": "0.00"},
                    "rt_cd": "0",
                }

            def place_overseas_order(self, symbol, action, price, qty):
                calls.append((symbol, action, price, qty))
                return {"rt_cd": "0", "msg1": "VTS sell order success"}

        object.__setattr__(mistock_config, "trading_env", "demo")
        original_dry_run = mistock_config.dry_run
        object.__setattr__(mistock_config, "dry_run", False)
        object.__setattr__(mistock_config, "total_capital", 1000.0)
        object.__setattr__(mistock_config, "currency", "USD")

        try:
            with patch.object(mistock_trader, "_get_kis_client", return_value=FakeClient()), \
                    patch.object(mistock_trader, "notify_slack_order"):
                mistock_db.execute(
                    "INSERT INTO holdings (symbol, name, qty, avg_price, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("KLAC", "KLAC", 3.0, 100.0, mistock_db.now_text()),
                )
                result = mistock_trader.place_order("KLAC", "sell", 2, 110, reason="unit test")
                balance = mistock.mistock_balance()
        finally:
            object.__setattr__(mistock_config, "dry_run", original_dry_run)

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("KLAC", "sell", 110, 2))

    def test_demo_unsupported_overseas_order_applies_local_fill(self):
        class FakeClient:
            def place_overseas_order(self, symbol, action, price, qty):
                return {"rt_cd": "1", "msg1": "모의투자에서는 해당업무가 제공되지 않습니다."}

        object.__setattr__(mistock_config, "trading_env", "demo")
        original_dry_run = mistock_config.dry_run
        object.__setattr__(mistock_config, "dry_run", False)

        try:
            with patch.object(mistock_trader, "_get_kis_client", return_value=FakeClient()), \
                    patch.object(mistock_trader, "notify_slack_order"):
                mistock_db.execute(
                    "INSERT INTO holdings (symbol, name, qty, avg_price, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("SBUX", "SBUX", 3.0, 100.0, mistock_db.now_text()),
                )
                result = mistock_trader.place_order("SBUX", "sell", 3, 110, reason="unit test")
        finally:
            object.__setattr__(mistock_config, "dry_run", original_dry_run)

        holding = mistock_db.row("SELECT symbol FROM holdings WHERE symbol = ?", ("SBUX",))
        trade = mistock_db.row("SELECT ok, order_status, response_msg FROM trades WHERE symbol = ?", ("SBUX",))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "demo_local_filled")
        self.assertIsNone(holding)
        self.assertEqual(trade["ok"], 1)
        self.assertEqual(trade["order_status"], "demo_local_filled")

    def test_demo_balance_converts_krw_config_capital_to_usd(self):
        class FakeClient:
            def get_overseas_balance(self):
                return {
                    "output1": [],
                    "output2": [],
                    "output3": {"tot_asst_amt": "0", "frcr_use_psbl_amt": "0.00"},
                    "rt_cd": "0",
                    "msg1": "mock account has no rows",
                }

        object.__setattr__(mistock_config, "trading_env", "demo")
        object.__setattr__(mistock_config, "total_capital", 100000000.0)
        object.__setattr__(mistock_config, "currency", "KRW")
        with patch.object(mistock_trader, "_get_kis_client", return_value=FakeClient()), \
             patch("src.mistock.trader.get_usd_krw_rate", return_value=1380.0):
            balance = mistock_trader.get_balance()

        self.assertAlmostEqual(balance["cash"], 72463.7681, places=3)
        self.assertEqual(balance["balance_source"], "demo_config_fallback")

    def test_demo_balance_caps_large_broker_cash_to_configured_capital(self):
        class FakeClient:
            def get_overseas_balance(self):
                return {
                    "output1": [],
                    "output2": {"frcr_dncl_amt": "370809500"},
                    "output3": {},
                }

        object.__setattr__(mistock_config, "trading_env", "demo")
        object.__setattr__(mistock_config, "total_capital", 100000000.0)
        object.__setattr__(mistock_config, "currency", "KRW")
        with patch.object(mistock_trader, "_get_kis_client", return_value=FakeClient()), \
             patch("src.mistock.trader.get_usd_krw_rate", return_value=1380.0):
            balance = mistock_trader.get_balance()

        self.assertAlmostEqual(balance["cash"], 72463.7681, places=3)
        self.assertAlmostEqual(balance["total_eval"], 72463.7681, places=3)
        self.assertEqual(balance["broker_total_eval"], 72463.76811594203)
        self.assertEqual(balance["balance_source"], "kis_config_capped")

    def test_mistock_candidates_include_planned_order_quantity(self):
        scan = {
            "candidates": [
                {
                    "ticker": "AAPL",
                    "symbol": "AAPL",
                    "name": "Apple",
                    "score": 5.0,
                    "price": 100.0,
                    "reasons": ["unit"],
                }
            ],
            "scan_summary": {"scanned": 1, "matched": 1, "scan_error": ""},
            "scanned": 1,
        }

        with patch.object(mistock_trader, "scan_candidates", return_value=scan), \
                patch.object(mistock_trader, "get_balance", return_value={"cash": 1000.0, "balance_source": "test"}):
            result = mistock.mistock_candidates()

        candidate = result["candidates"][0]
        self.assertEqual(candidate["planned_qty"], 8)
        self.assertEqual(candidate["estimated_cost"], 800.0)
        self.assertEqual(result["balance_source"], "test")

    def test_mistock_settings_and_action_endpoints_are_available(self):
        with patch.dict("os.environ", {}, clear=False), \
                patch("src.dashboard.routes.mistock._core._write_env_values") as write_env:
            env_result = mistock.mistock_update_env({"values": {"MISTOCK_TOTAL_CAPITAL": "100000"}})
        strategies = mistock.mistock_ai_strategies()["strategies"]
        strategy_id = strategies[0]["id"]

        self.assertTrue(env_result["ok"])
        self.assertFalse(env_result["requires_restart"])
        self.assertEqual(mistock_config.total_capital, 100000.0)
        write_env.assert_called_once()
        self.assertTrue(mistock.mistock_static_verify(strategy_id)["ok"])
        self.assertTrue(mistock.mistock_api_verify(strategy_id)["ok"])
        with patch(
            "src.strategy.backtest_mistock.run_mistock_backtest",
            return_value={"ok": True, "success": True, "status": "passed"},
        ):
            self.assertTrue(mistock.mistock_backtest(strategy_id)["ok"])
        self.assertTrue(mistock.mistock_paper_start(strategy_id)["ok"])
        self.assertTrue(mistock.mistock_paper_complete(strategy_id, {"days": 20})["ok"])
        self.assertEqual(mistock.mistock_strategy_approve(strategy_id)["status"], "approved")

        watchlist_result = mistock.mistock_watchlist_toggle_auto({"enabled": True, "threshold": 4})
        self.assertTrue(watchlist_result["enabled"])
        self.assertEqual(watchlist_result["threshold"], 4.0)
        self.assertTrue(mistock.mistock_trades_sync()["ok"])
        with patch("src.dashboard.routes.mistock.threading.Thread") as mock_thread:
            mock_thread_instance = mock_thread.return_value
            with patch.object(mistock_trader, "scan_candidates", return_value={"scanned": 0, "candidates": []}):
                response = mistock.mistock_scheduler_run({"mode": "analysis_only"})
                self.assertIn("result", response)
                mock_thread.assert_called_once()
                mock_thread_instance.start.assert_called_once()

    def test_mistock_env_returns_scheduler_and_strategy_values(self):
        with patch(
            "src.dashboard.routes.mistock._mistock_env_values",
            return_value={
                "MISTOCK_TOTAL_CAPITAL": "123456",
                "MISTOCK_DAILY_AUTO_RETRIES": "4",
            },
        ):
            result = mistock.mistock_env()

        fields = {field["key"]: field for field in result["fields"]}
        self.assertFalse(result["requires_restart"])
        self.assertEqual(fields["MISTOCK_TOTAL_CAPITAL"]["value"], "123456")
        self.assertEqual(fields["MISTOCK_SPLIT_N"]["value"], str(mistock_config.split_n))
        self.assertEqual(fields["MISTOCK_DAILY_AUTO_RETRIES"]["value"], "4")
        self.assertIn("MISTOCK_SCHEDULER_RETRY_DELAY_SECONDS", fields)

    def test_mistock_update_env_accepts_strategy_aliases_and_applies_runtime(self):
        with patch.dict("os.environ", {}, clear=False), \
                patch("src.dashboard.routes.mistock._core._write_env_values") as write_env:
            result = mistock.mistock_update_env({
                "values": {
                    "SPLIT_N": "9",
                    "MAX_POSITIONS": "3",
                    "MAX_SINGLE_WEIGHT": "0.4",
                }
            })

        self.assertTrue(result["ok"])
        self.assertEqual(mistock_config.split_n, 9)
        self.assertEqual(mistock_config.max_positions, 3)
        self.assertEqual(mistock_config.max_single_weight, 0.4)
        written = write_env.call_args.args[0]
        self.assertEqual(written["MISTOCK_SPLIT_N"], "9")
        self.assertEqual(written["MISTOCK_MAX_POSITIONS"], "3")
        self.assertEqual(written["MISTOCK_MAX_SINGLE_WEIGHT"], "0.4")

    def test_mistock_env_numeric_values_accept_thousands_separators(self):
        self.assertEqual(mistock._validate_mistock_env_value("MISTOCK_TOTAL_CAPITAL", "100,000,000"), "100000000")
        self.assertEqual(mistock._validate_mistock_env_value("USDKRW_FALLBACK_RATE", "1,516.78"), "1516.78")

    def test_mistock_scheduler_status_includes_config_values(self):
        with patch.dict(
            "os.environ",
            {
                "MISTOCK_CRON_TZ": "Asia/Seoul",
                "MISTOCK_DAILY_AUTO_RETRIES": "5",
                "MISTOCK_SCHEDULER_SLACK": "false",
            },
            clear=False,
        ):
            result = mistock.mistock_scheduler_status()

        self.assertEqual(result["config"]["cron_tz"], "Asia/Seoul")
        self.assertEqual(result["config"]["daily_auto_retries"], "5")
        self.assertEqual(result["config"]["slack_enabled"], "false")
        self.assertIn("order_submission_enabled", result["config"])

    def test_mistock_analysis_only_does_not_queue_sell_approvals(self):
        object.__setattr__(mistock_config, "trading_env", "demo")
        signal = {
            "symbol": "SBUX",
            "action": "sell",
            "signal_qty": 2,
            "signal_price": 100.0,
            "reason": "unit sell signal",
        }

        with patch.object(mistock_trader, "scan_candidates", return_value={"scanned": 1, "candidates": []}), \
                patch.object(
                    mistock_trader,
                    "get_balance",
                    return_value={"cash": 1000.0, "total_eval": 1000.0, "holdings": [], "stock_eval": 0.0},
                ), \
                patch.object(mistock_trader, "signals", return_value=[signal]), \
                patch.object(mistock_scheduler, "is_us_market_open", return_value=False), \
                patch("src.mistock.scheduler.Path.write_text"), \
                patch("src.mistock.scheduler.send_mistock_slack"):
            result = mistock_scheduler.run_mistock_scheduled_cycle(mode="analysis_only")

        pending = mistock_db.rows("SELECT * FROM approvals WHERE status = 'pending'")
        self.assertTrue(result["ok"])
        self.assertEqual(pending, [])

    def test_mistock_scheduler_does_not_duplicate_pending_sell_approval(self):
        object.__setattr__(mistock_config, "trading_env", "demo")
        now = mistock_db.now_text()
        mistock_db.execute(
            """
            INSERT INTO approvals (created_at, updated_at, symbol, name, action, qty, price, reason, source, status, response_msg)
            VALUES (?, ?, 'SBUX', 'SBUX', 'sell', 2, 100, 'existing', 'scheduler', 'pending', '')
            """,
            (now, now),
        )
        signal = {
            "symbol": "SBUX",
            "action": "sell",
            "signal_qty": 2,
            "signal_price": 100.0,
            "reason": "unit sell signal",
        }

        with patch.object(mistock_trader, "scan_candidates", return_value={"scanned": 1, "candidates": []}), \
                patch.object(
                    mistock_trader,
                    "get_balance",
                    return_value={"cash": 1000.0, "total_eval": 1000.0, "holdings": [], "stock_eval": 0.0},
                ), \
                patch.object(mistock_trader, "signals", return_value=[signal]), \
                patch.object(mistock_scheduler, "is_us_market_open", return_value=False), \
                patch("src.mistock.scheduler.Path.write_text"), \
                patch("src.mistock.scheduler.send_mistock_slack"):
            result = mistock_scheduler.run_mistock_scheduled_cycle(mode="execute")

        pending = mistock_db.rows("SELECT * FROM approvals WHERE status = 'pending' AND symbol = 'SBUX'")
        self.assertTrue(result["ok"])
        self.assertEqual(len(pending), 1)

    def test_mistock_scheduler_status_clears_stale_restart_error_after_success(self):
        original_state = dict(mistock._mistock_scheduler_run_state)
        mistock._mistock_scheduler_run_state.replace({
            "is_running": False,
            "mode": "analysis_only",
            "started_at": "2026-06-13T13:52:07+09:00",
            "completed_at": "2026-06-13T14:11:38+09:00",
            "result": None,
            "error": "interrupted by process restart",
            "owner_pid": None,
        })
        runs = [{
            "recorded_at": "2026-06-16T05:00:26+09:00",
            "mode": "execute",
            "result": {
                "status": "success",
                "ok": True,
                "scanned": 100,
                "candidates": 38,
                "sold": [],
                "bought": [],
                "plan": [],
                "errors": [],
            },
        }]

        try:
            with patch("src.dashboard.routes.mistock.load_mistock_daily_runs", return_value=runs):
                result = mistock.mistock_scheduler_status()
        finally:
            mistock._mistock_scheduler_run_state.replace(original_state)

        self.assertIsNone(result["run_state"]["error"])
        self.assertEqual(result["run_state"]["completed_at"], "2026-06-16T05:00:26+09:00")
        self.assertTrue(result["last_result"]["result"]["ok"])

    def test_mistock_scheduler_status_merges_all_daily_run_details(self):
        runs = [
            {
                "recorded_at": "2026-06-18T01:00:00+09:00",
                "mode": "execute",
                "result": {
                    "status": "success",
                    "ok": True,
                    "scanned": 10,
                    "candidates": 1,
                    "sold": [],
                    "bought": [
                        {"symbol": "AAPL", "qty": 1, "price": 100, "result": {"ok": True, "message": "filled"}}
                    ],
                    "pending_approved": [],
                    "plan": [],
                    "errors": [],
                },
            },
            {
                "recorded_at": "2026-06-18T02:00:00+09:00",
                "mode": "execute",
                "result": {
                    "status": "failed",
                    "ok": False,
                    "scanned": 20,
                    "candidates": 2,
                    "sold": [],
                    "bought": [],
                    "pending_approved": [
                        {
                            "id": 7,
                            "symbol": "MSFT",
                            "action": "buy",
                            "qty": 2,
                            "price": 200,
                            "result": {"ok": True, "message": "pending filled"},
                        }
                    ],
                    "plan": [{"symbol": "NVDA", "quantity": 1, "price": 300, "reason": "candidate"}],
                    "errors": [{"symbol": "TSLA", "message": "broker rejected"}],
                },
            },
        ]

        with patch("src.dashboard.routes.mistock.load_mistock_daily_runs", return_value=runs) as load_runs:
            result = mistock.mistock_scheduler_status()

        last = result["last_result"]["result"]
        load_runs.assert_called_once_with(days=30)
        self.assertEqual(result["last_result"]["range_days"], 30)
        self.assertEqual(result["last_result"]["summary_label"], "최근 30일 전체 집계")
        self.assertEqual([row["symbol"] for row in last["results"]], ["AAPL", "NVDA"])
        self.assertEqual([row["symbol"] for row in last["auto_approved"]], ["AAPL", "MSFT"])
        self.assertEqual(last["errors"][0]["symbol"], "TSLA")
        self.assertEqual(last["results"][0]["round"], 1)
        self.assertEqual(last["results"][1]["round"], 2)
        self.assertEqual(last["results"][0]["time"], "06-18 01:00")

    def test_mistock_scheduler_status_keeps_historical_errors_out_of_current_errors(self):
        runs = [
            {
                "recorded_at": "2026-06-18T01:00:00+09:00",
                "mode": "execute",
                "result": {
                    "status": "failed",
                    "ok": False,
                    "scanned": 10,
                    "candidates": 1,
                    "sold": [],
                    "bought": [],
                    "pending_approved": [],
                    "plan": [],
                    "errors": [{"symbol": "TSLA", "message": "old broker rejected"}],
                },
            },
            {
                "recorded_at": "2026-06-18T02:00:00+09:00",
                "mode": "execute",
                "result": {
                    "status": "success",
                    "ok": True,
                    "scanned": 20,
                    "candidates": 2,
                    "sold": [],
                    "bought": [],
                    "pending_approved": [],
                    "plan": [],
                    "errors": [],
                },
            },
        ]

        with patch("src.dashboard.routes.mistock.load_mistock_daily_runs", return_value=runs):
            result = mistock.mistock_scheduler_status()

        last = result["last_result"]["result"]
        self.assertEqual(last["errors"], [])
        self.assertEqual(last["historical_error_count"], 1)
        self.assertEqual(last["historical_errors"][0]["symbol"], "TSLA")

    def test_mistock_easy_preset_uses_nasdaq_profile_and_selects_strategy(self):
        result = mistock.mistock_apply_ai_strategy_preset("aggressive")
        strategies = mistock.mistock_ai_strategies()["strategies"]
        selected = [item for item in strategies if item.get("selected")]

        self.assertTrue(result["ok"])
        self.assertEqual(result["preset"], "aggressive")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["id"], result["strategy"]["id"])
        self.assertEqual(selected[0]["status"], "approved")
        self.assertEqual(selected[0]["profile"]["market"], "NASDAQ")
        self.assertEqual(selected[0]["profile"]["universe"], "NASDAQ100")
        context = mistock.mistock_strategy_context()
        self.assertEqual(context["active_strategy"]["id"], result["strategy"]["id"])
        self.assertIn("backtest", context["active_strategy"]["validation"]["checks"])

    def test_mistock_runtime_order_mode(self):
        # 1. Store initial MISTOCK_DRY_RUN value
        initial_val = mistock_config.dry_run
        target_val = not initial_val

        # 2. Toggle dry_run
        with patch("src.dashboard.routes.mistock._core._write_env_values") as mock_write:
            result = mistock.mistock_runtime_order_mode({"key": "DRY_RUN", "enabled": target_val})
            self.assertTrue(result["ok"])
            self.assertEqual(result["dry_run"], target_val)
            self.assertEqual(mistock_config.dry_run, target_val)
            mock_write.assert_called_once()

            # Toggle back to initial value
            result_true = mistock.mistock_runtime_order_mode({"key": "DRY_RUN", "enabled": initial_val})
            self.assertTrue(result_true["ok"])
            self.assertEqual(result_true["dry_run"], initial_val)
            self.assertEqual(mistock_config.dry_run, initial_val)

    def test_scheduler_marks_broker_order_failure(self):
        order = {
            "symbol": "AAPL",
            "quantity": 1,
            "price": 100.0,
            "reason": "unit test",
        }
        failed_order = {"ok": False, "status": "failed", "msg1": "broker rejected"}

        with patch.object(mistock_trader, "scan_candidates", return_value={"scanned": 1, "candidates": [{"symbol": "AAPL"}]}), \
                patch.object(mistock_trader, "get_balance", return_value={"cash": 1000.0, "total_eval": 1000.0}), \
                patch.object(mistock_trader, "signals", return_value=[]), \
                patch.object(mistock_trader, "build_orders", return_value=[order]), \
                patch.object(mistock_trader, "broker_submission_available", return_value=True), \
                patch.object(mistock_trader, "runtime_flags", return_value={"order_submission_enabled": True}), \
                patch.object(mistock_trader, "place_order", return_value=failed_order), \
                patch.object(mistock_db, "get_setting", return_value="true"), \
                patch.object(mistock_scheduler, "send_mistock_slack"):
            result = mistock_scheduler.run_mistock_scheduled_cycle(mode="execute")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"][0]["symbol"], "AAPL")
        self.assertEqual(result["errors"][0]["message"], "broker rejected")

    def test_scheduler_queues_orders_when_demo_broker_balance_is_fallback(self):
        order = {
            "symbol": "AAPL",
            "quantity": 1,
            "price": 100.0,
            "reason": "unit test",
        }

        with patch.object(mistock_trader, "scan_candidates", return_value={"scanned": 1, "candidates": [{"symbol": "AAPL"}]}), \
                patch.object(mistock_trader, "get_balance", return_value={"cash": 1000.0, "total_eval": 1000.0, "balance_source": "demo_config_fallback"}), \
                patch.object(mistock_trader, "signals", return_value=[]), \
                patch.object(mistock_trader, "build_orders", return_value=[order]), \
                patch.object(mistock_db, "get_setting", return_value="true"), \
                patch.object(mistock_trader, "place_order") as place_order, \
                patch.object(mistock_scheduler, "send_mistock_slack"):
            result = mistock_scheduler.run_mistock_scheduled_cycle(mode="execute")

        self.assertTrue(result["ok"])
        self.assertEqual(result["bought"], [])
        place_order.assert_not_called()
        pending = mistock_db.rows("SELECT symbol, action, qty, price, status FROM approvals WHERE status = 'pending'")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["symbol"], "AAPL")

    def test_scheduler_executes_pending_scheduler_approvals_when_market_open(self):
        now = mistock_db.now_text()
        approval_id = mistock_db.execute(
            """
            INSERT INTO approvals (created_at, updated_at, symbol, name, action, qty, price, reason, source, status, response_msg)
            VALUES (?, ?, 'AAPL', 'Apple', 'buy', 1, 100, 'before market', 'scheduler', 'pending', '')
            """,
            (now, now),
        )

        with patch.object(mistock_trader, "scan_candidates", return_value={"scanned": 1, "candidates": []}), \
                patch.object(mistock_trader, "get_balance", return_value={"cash": 1000.0, "total_eval": 1000.0}), \
                patch.object(mistock_trader, "signals", return_value=[]), \
                patch.object(mistock_trader, "build_orders", return_value=[]), \
                patch.object(mistock_trader, "broker_submission_available", return_value=True), \
                patch.object(mistock_trader, "place_order", return_value={"ok": True, "message": "filled"}) as place_order, \
                patch.object(mistock_db, "get_setting", return_value="true"), \
                patch.object(mistock_scheduler, "is_us_market_open", return_value=True), \
                patch.object(mistock_scheduler, "send_mistock_slack"):
            result = mistock_scheduler.run_mistock_scheduled_cycle(mode="execute")

        row = mistock_db.row("SELECT status, response_msg FROM approvals WHERE id = ?", (approval_id,))
        self.assertTrue(result["ok"])
        self.assertEqual(row["status"], "executed")
        self.assertEqual(result["pending_approved"][0]["symbol"], "AAPL")
        place_order.assert_called_once_with("AAPL", "buy", 1.0, 100.0, reason="before market")

    def test_create_approval_does_not_auto_execute_when_broker_balance_is_fallback(self):
        mistock_db.set_setting("auto_approval", "true")

        with patch.object(mistock_trader, "broker_submission_available", return_value=False), \
                patch.object(mistock_trader, "place_order") as place_order:
            result = mistock.mistock_create_approval({
                "symbol": "AAPL",
                "name": "Apple",
                "action": "buy",
                "qty": 1,
                "price": 100,
                "reason": "fallback balance",
            })

        self.assertTrue(result["ok"])
        self.assertFalse(result["auto_approved"])
        self.assertEqual(result["status"], "pending")
        place_order.assert_not_called()

    def test_create_approval_does_not_auto_execute_before_market_open(self):
        mistock_db.set_setting("auto_approval", "true")

        with patch.object(mistock_trader, "broker_submission_available", return_value=True), \
                patch.object(mistock, "_is_mistock_order_window_open", return_value=False), \
                patch.object(mistock_trader, "place_order") as place_order:
            result = mistock.mistock_create_approval({
                "symbol": "AAPL",
                "name": "Apple",
                "action": "buy",
                "qty": 1,
                "price": 100,
                "reason": "before market",
            })
            with self.assertRaises(mistock.HTTPException) as raised:
                mistock.mistock_approve(result["id"])

        row = mistock_db.row("SELECT status, response_msg FROM approvals WHERE id = ?", (result["id"],))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(result["ok"])
        self.assertFalse(result["auto_approved"])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["response_msg"], "")
        place_order.assert_not_called()

    def test_online_access_block_keeps_mistock_approval_pending(self):
        main_config.online_access_blocked = True
        mistock_db.set_setting("auto_approval", "true")

        with patch.object(mistock_trader, "place_order") as place_order:
            result = mistock.mistock_create_approval({
                "symbol": "AAPL",
                "name": "Apple",
                "action": "buy",
                "qty": 1,
                "price": 100,
                "reason": "blocked",
            })
            with self.assertRaises(mistock.HTTPException) as raised:
                mistock.mistock_approve(result["id"])

        row = mistock_db.row("SELECT status, response_msg FROM approvals WHERE id = ?", (result["id"],))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["response_msg"], "")
        place_order.assert_not_called()

    def test_add_watchlist_bulk_symbols_splits_properly(self):
        mistock_db.execute("DELETE FROM watchlist")
        result = mistock_trader.add_watchlist("GOOG, COST, PEP")
        added = [row["symbol"] for row in mistock_trader.get_watchlist()]
        self.assertEqual(len(added), 13)
        self.assertIn("GOOG", added)
        self.assertIn("COST", added)
        self.assertIn("PEP", added)

    def test_mistock_strategy_selection_supports_multiple(self):
        mistock_db.init_db()
        created = mistock.mistock_create_ai_strategy({"name": "Second", "model": "rule_based"})
        second_id = created["strategy"]["id"]

        mistock.mistock_select_ai_strategy(second_id, {"selected": True})

        selected = {
            row["id"] for row in mistock.mistock_ai_strategies()["strategies"] if row.get("selected")
        }
        self.assertIn("mistock_nasdaq_rule_v1", selected)
        self.assertIn(second_id, selected)

    def test_mistock_scheduler_accepts_multiple_strategy_ids(self):
        mistock_db.init_db()
        mistock._mistock_scheduler_run_state.replace({
            "is_running": False, "mode": None, "started_at": None,
            "completed_at": None, "result": None, "error": None, "owner_pid": None,
        })
        with patch.object(mistock.threading, "Thread") as thread:
            thread.return_value = MagicMock()
            response = mistock.mistock_scheduler_run({
                "mode": "analysis_only", "strategy_ids": ["alpha", "beta", "alpha"],
            })

        self.assertEqual(response["strategy_ids"], ["alpha", "beta"])
        self.assertEqual(thread.call_args.kwargs["args"], ("analysis_only", ["alpha", "beta"]))

    def test_mistock_balance_exposes_strategy_ownership(self):
        mistock_db.init_db()
        mistock_trader.save_trade(
            "AAPL", "Apple", "buy", 2, 100, "strategy test", True, "filled", "ok",
            "mistock_nasdaq_rule_v1",
        )
        with patch.object(mistock_trader, "get_balance", return_value={
            "cash": 1000.0,
            "holdings": [{"symbol": "AAPL", "name": "Apple", "qty": 2, "price": 100}],
        }):
            balance = mistock.mistock_balance()

        holding = balance["holdings"][0]
        self.assertEqual(holding["strategy_ids"], ["mistock_nasdaq_rule_v1"])
        self.assertEqual(holding["strategies"][0]["qty"], 2.0)

    def test_auto_approval_can_be_enabled_while_market_is_closed(self):
        with patch.object(mistock, "_is_mistock_order_window_open", return_value=False):
            result = mistock.mistock_set_auto_approval({"enabled": True})

        self.assertTrue(result["ok"])
        self.assertTrue(result["enabled"])
        self.assertEqual(result["processed_count"], 0)
        self.assertEqual(mistock_db.get_setting("auto_approval"), "true")

    def test_schedule_result_expands_us_stock_identity(self):
        mapped = mistock.map_mistock_to_kis_format({
            "strategy_id": "mistock_nasdaq_rule_v1",
            "status": "success",
            "plan": [{
                "symbol": "SBUX", "quantity": 1, "price": 95.0, "reason": "test",
            }],
        })

        row = mapped["results"][0]
        self.assertEqual(row["name"], "Starbucks")
        self.assertEqual(row["display_name"], "Starbucks (SBUX)")
        self.assertEqual(row["market"], "US")
        self.assertEqual(row["asset_type"], "미국 주식")


if __name__ == "__main__":
    unittest.main()
