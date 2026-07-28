import tempfile
import unittest
import asyncio
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import src.dashboard as dashboard
import src.dashboard.core as dashboard_core
from src.dashboard import _parse_balance, _portfolio_totals


class MemoryCachePath:
    def __init__(self):
        self.content = None
        self.parent = self

    def mkdir(self, parents=False, exist_ok=False):
        return None

    def exists(self):
        return self.content is not None

    def write_text(self, text, encoding=None):
        self.content = text

    def read_text(self, encoding=None):
        return self.content


class MemoryTextPath:
    def __init__(self, content=""):
        self.content = content

    def exists(self):
        return True

    def read_text(self, encoding=None):
        return self.content

    def write_text(self, text, encoding=None):
        self.content = text


class DashboardCoreTests(unittest.TestCase):
    def test_json_safe_value_replaces_non_finite_floats(self):
        payload = {
            "ok": True,
            "nan": math.nan,
            "inf": math.inf,
            "nested": [{"value": -math.inf}, {"value": 1.5}],
        }

        safe = dashboard_core._json_safe_value(payload)

        self.assertIsNone(safe["nan"])
        self.assertIsNone(safe["inf"])
        self.assertIsNone(safe["nested"][0]["value"])
        self.assertEqual(safe["nested"][1]["value"], 1.5)

    def test_broker_history_import_is_treated_as_real_fill(self):
        self.assertFalse(dashboard._trade_is_sync_adjustment({"reason": "broker history import"}))
        self.assertTrue(dashboard._trade_is_sync_adjustment({"reason": "balance sync adjustment"}))

    def test_parse_balance_uses_holding_eval_amount(self):
        parsed = _parse_balance({
            "output1": [{
                "pdno": "005930",
                "prdt_name": "Samsung",
                "hldg_qty": "10",
                "prpr": "0",
                "evlu_amt": "700000",
                "evlu_pfls_amt": "-10000",
                "evlu_pfls_rt": "-1.41",
            }],
            "output2": [{
                "dnca_tot_amt": "1000000",
                "prvs_rcdl_excc_amt": "200000",
                "scts_evlu_amt": "700000",
                "tot_evlu_amt": "900000",
                "evlu_pfls_smtl_amt": "-10000",
            }],
        })

        self.assertEqual(parsed["holdings"][0]["value"], 700000)
        self.assertEqual(parsed["holdings"][0]["price"], 70000)
        self.assertEqual(parsed["stock_eval"], 700000)
        self.assertEqual(parsed["cash"], 200000)
        self.assertEqual(parsed["total_eval"], 900000)
        self.assertLessEqual(parsed["cash_ratio"], 1.0)

    def test_portfolio_totals_clamps_cash_ratio(self):
        totals = _portfolio_totals(
            cash=10_000_000,
            summary_total=9_937_130,
            holdings=[{"value": 2_000_000}],
        )

        self.assertEqual(totals["stock_eval"], 2_000_000)
        self.assertEqual(totals["total_eval"], 9_937_130)
        self.assertGreater(totals["cash_ratio"], 0)
        self.assertLessEqual(totals["cash_ratio"], 1.0)

    def test_auto_approve_ignores_already_claimed_approval(self):
        exc = dashboard.HTTPException(status_code=409, detail="approval is already executing")

        with patch.object(dashboard_core, "_pending_approval_ids", return_value=[123]), patch.object(
            dashboard_core, "_approve_pending_approval", side_effect=exc
        ), patch.object(dashboard_core.logger, "warning") as warning, patch.object(
            dashboard_core.logger, "debug"
        ) as debug:
            result = dashboard_core._auto_approve_pending_approvals()

        self.assertEqual(result, [])
        warning.assert_not_called()
        debug.assert_called_once()

    def test_balance_cache_is_scoped_to_account(self):
        original_cache = dashboard.BALANCE_CACHE
        original_account = dashboard.trader.config.kistock_account
        try:
            dashboard.BALANCE_CACHE = MemoryCachePath()
            dashboard.trader.config.kistock_account = "1111111101"
            dashboard._save_balance_cache({"output1": [], "output2": []})

            dashboard.trader.config.kistock_account = "2222222201"
            self.assertIsNone(dashboard._load_balance_cache())

            dashboard.trader.config.kistock_account = "1111111101"
            self.assertIsNotNone(dashboard._load_balance_cache())
        finally:
            dashboard.BALANCE_CACHE = original_cache
            dashboard.trader.config.kistock_account = original_account

    def test_balance_data_falls_back_to_cache_on_kis_server_error(self):
        cached = {
            "output1": [],
            "output2": [{"dnca_tot_amt": "1000", "tot_evlu_amt": "1000"}],
            "_cache": {"cached_at": "2026-06-30T08:00:00+09:00", "stale": True},
        }

        class _FailingAPI:
            def get_balance(self):
                raise RuntimeError("Balance HTTP 500: Internal Server Error")

        with patch.object(dashboard_core, "_load_balance_cache", return_value=cached), \
                patch.object(dashboard_core, "_balance_cache_age_seconds", return_value=999999):
            result = dashboard_core._get_balance_data(_FailingAPI())

        self.assertEqual(result, cached)

    def test_kis_http_error_message_does_not_expose_account_url(self):
        from src.api.kis_api import KIStockAPI

        class _Response:
            status_code = 500
            text = "Internal Server Error"

            def json(self):
                return {}

        api = object.__new__(KIStockAPI)
        with self.assertRaises(RuntimeError) as raised:
            api._response_json(_Response(), "Balance")

        message = str(raised.exception)
        self.assertIn("Balance HTTP 500", message)
        self.assertNotIn("CANO=", message)
        self.assertNotIn("inquire-balance?", message)

    def test_env_writer_preserves_comments_and_updates_allowed_keys(self):
        path = MemoryTextPath("# KIS\nTRADING_ENV=demo\nDRY_RUN=true\nMAX_POSITIONS=10 # 최대보유주식종목\n")
        dashboard._write_env_values({"TRADING_ENV": "real", "MAX_POSITIONS": "5"}, path)

        self.assertIn("# KIS", path.content)
        self.assertIn("TRADING_ENV=real", path.content)
        self.assertIn("DRY_RUN=true", path.content)
        self.assertIn("MAX_POSITIONS=5 # 최대보유주식종목", path.content)

    def test_env_reader_strips_inline_comments_from_numbers(self):
        path = MemoryTextPath("MAX_POSITIONS=10 # 최대보유주식종목\nACTIVE_MODEL_VERSION=v1\n")

        values = dashboard._read_env_values(path)

        self.assertEqual(values["MAX_POSITIONS"], "10")
        self.assertEqual(dashboard._validate_env_value("MAX_POSITIONS", "10 # 최대보유주식종목"), "10")

    def test_env_numeric_values_accept_thousands_separators(self):
        self.assertEqual(dashboard._validate_env_value("TOTAL_CAPITAL", "100,000,000"), "100000000")
        self.assertEqual(dashboard._validate_env_value("USDKRW_FALLBACK_RATE", "1,516.78"), "1516.78")

    def test_kis_ops_routes_are_registered(self):
        paths = {getattr(route, "path", "") for route in dashboard.app.routes}

        self.assertIn("/api/kis/condition-search/list", paths)
        self.assertIn("/api/kis/condition-search/result", paths)
        self.assertIn("/api/kis/websocket/status", paths)
        self.assertIn("/api/kis/websocket/start", paths)
        self.assertIn("/api/kis/websocket/stop", paths)
        self.assertIn("/api/kis/websocket/subscribe", paths)
        self.assertIn("/api/kis/orders/cancel", paths)
        self.assertIn("/api/kis/orders/revise", paths)
        self.assertIn("/api/kis/rehearsal", paths)

    def test_kis_env_settings_apply_without_restart(self):
        original_env_path = dashboard.ENV_PATH
        original_values = {
            "kistock_hts_id": getattr(dashboard.trader.config, "kistock_hts_id", ""),
            "kis_websocket_enabled": getattr(dashboard.trader.config, "kis_websocket_enabled", False),
            "kis_condition_search_enabled": getattr(dashboard.trader.config, "kis_condition_search_enabled", False),
            "kis_condition_user_id": getattr(dashboard.trader.config, "kis_condition_user_id", ""),
            "kis_condition_seq": getattr(dashboard.trader.config, "kis_condition_seq", ""),
            "kis_condition_name": getattr(dashboard.trader.config, "kis_condition_name", ""),
        }
        try:
            dashboard.ENV_PATH = MemoryTextPath("")
            result = dashboard.update_env_settings({
                "values": {
                    "KISTOCK_HTS_ID": "hts-user",
                    "KIS_WEBSOCKET_ENABLED": "true",
                    "KIS_CONDITION_SEARCH_ENABLED": "true",
                    "KIS_CONDITION_USER_ID": "condition-user",
                    "KIS_CONDITION_SEQ": "001",
                    "KIS_CONDITION_NAME": "breakout",
                    "MISTOCK_EXCHANGE_MAP": "BRK.B=NYSE",
                }
            })

            self.assertTrue(result["ok"])
            self.assertEqual(dashboard.trader.config.kistock_hts_id, "hts-user")
            self.assertTrue(dashboard.trader.config.kis_websocket_enabled)
            self.assertTrue(dashboard.trader.config.kis_condition_search_enabled)
            self.assertEqual(dashboard.trader.config.kis_condition_user_id, "condition-user")
            self.assertEqual(dashboard.trader.config.kis_condition_seq, "001")
            self.assertEqual(dashboard.trader.config.kis_condition_name, "breakout")
        finally:
            dashboard.ENV_PATH = original_env_path
            for key, value in original_values.items():
                setattr(dashboard.trader.config, key, value)

    def test_online_access_setting_applies_without_restart(self):
        original_env_path = dashboard.ENV_PATH
        original_config = dashboard.trader.config.online_access_blocked
        original_runtime = dashboard.trader.ONLINE_ACCESS_BLOCKED
        original_submission = dashboard.trader.ORDER_SUBMISSION_ENABLED
        try:
            dashboard.ENV_PATH = MemoryTextPath("ONLINE_ACCESS_BLOCKED=false\n")
            dashboard.trader.config.online_access_blocked = False
            dashboard.trader.ONLINE_ACCESS_BLOCKED = False
            dashboard.trader.ORDER_SUBMISSION_ENABLED = True

            with patch("src.dashboard.routes.settings._stop_kis_websocket") as stop_websocket:
                result = dashboard.update_env_settings({
                    "values": {"ONLINE_ACCESS_BLOCKED": "true"}
                })

            self.assertTrue(result["ok"])
            self.assertTrue(dashboard.trader.config.online_access_blocked)
            self.assertTrue(dashboard.trader.ONLINE_ACCESS_BLOCKED)
            self.assertFalse(dashboard.trader.ORDER_SUBMISSION_ENABLED)
            self.assertIn("ONLINE_ACCESS_BLOCKED=true", dashboard.ENV_PATH.content)
            stop_websocket.assert_called_once()
        finally:
            dashboard.ENV_PATH = original_env_path
            dashboard.trader.config.online_access_blocked = original_config
            dashboard.trader.ONLINE_ACCESS_BLOCKED = original_runtime
            dashboard.trader.ORDER_SUBMISSION_ENABLED = original_submission

    def test_runtime_order_mode_updates_apply_without_restart(self):
        original_env_path = dashboard.ENV_PATH
        original_trading_env = dashboard.trader.TRADING_ENV
        original_dry_run = dashboard.trader.DRY_RUN
        original_enable_live = dashboard.trader.ENABLE_LIVE_TRADING
        original_order_submission = dashboard.trader.ORDER_SUBMISSION_ENABLED
        original_real_orders = dashboard.trader.REAL_ORDERS_ENABLED
        original_config_values = {
            "trading_env": dashboard.trader.config.trading_env,
            "dry_run": dashboard.trader.config.dry_run,
            "enable_live_trading": dashboard.trader.config.enable_live_trading,
        }
        try:
            path = MemoryTextPath("TRADING_ENV=demo\nDRY_RUN=true\nENABLE_LIVE_TRADING=false\n")
            dashboard.ENV_PATH = path
            dashboard.trader.TRADING_ENV = "demo"
            dashboard.trader.DRY_RUN = True
            dashboard.trader.ENABLE_LIVE_TRADING = False
            dashboard.trader.REAL_ORDERS_ENABLED = False
            dashboard.trader.ORDER_SUBMISSION_ENABLED = False
            dashboard.trader.config.trading_env = "demo"
            dashboard.trader.config.dry_run = True
            dashboard.trader.config.enable_live_trading = False

            result = dashboard.set_runtime_order_mode({"key": "DRY_RUN", "enabled": False})
            self.assertFalse(result["dry_run"])
            self.assertTrue(result["order_submission_enabled"])
            self.assertIn("DRY_RUN=false", path.content)

            with self.assertRaises(dashboard.HTTPException):
                dashboard.set_runtime_order_mode({"key": "REAL_ORDERS_ENABLED", "enabled": True})
        finally:
            dashboard.ENV_PATH = original_env_path
            dashboard.trader.TRADING_ENV = original_trading_env
            dashboard.trader.DRY_RUN = original_dry_run
            dashboard.trader.ENABLE_LIVE_TRADING = original_enable_live
            dashboard.trader.ORDER_SUBMISSION_ENABLED = original_order_submission
            dashboard.trader.REAL_ORDERS_ENABLED = original_real_orders
            dashboard.trader.config.trading_env = original_config_values["trading_env"]
            dashboard.trader.config.dry_run = original_config_values["dry_run"]
            dashboard.trader.config.enable_live_trading = original_config_values["enable_live_trading"]

    def test_secret_env_values_are_masked_for_response(self):
        self.assertEqual(dashboard._mask_env_value("1234567801"), "12******01")

    def test_env_settings_return_unmasked_values(self):
        original_env_path = dashboard.ENV_PATH
        try:
            dashboard.ENV_PATH = MemoryTextPath(
                "KISTOCK_APP_KEY=app-key-secret\n"
                "KISTOCK_ACCOUNT=1234567801\n"
                "TRADING_ENV=demo\n"
            )
            with patch("src.utils.exchange_rate.get_usd_krw_rate", return_value=1380.0):
                data = dashboard.get_env_settings()
            fields = {field["key"]: field for field in data["fields"]}

            self.assertEqual(fields["KISTOCK_APP_KEY"]["value"], "app-key-secret")
            self.assertEqual(fields["KISTOCK_APP_KEY"]["masked"], "")
            self.assertEqual(fields["KISTOCK_APP_KEY"]["type"], "text")
            self.assertFalse(fields["KISTOCK_APP_KEY"]["secret"])
            self.assertTrue(fields["KISTOCK_APP_KEY"]["has_value"])
            self.assertEqual(fields["KISTOCK_ACCOUNT"]["value"], "1234567801")
            self.assertEqual(fields["KISTOCK_ACCOUNT"]["masked"], "")
            self.assertEqual(fields["KISTOCK_ACCOUNT"]["type"], "text")
            self.assertFalse(fields["KISTOCK_ACCOUNT"]["secret"])
        finally:
            dashboard.ENV_PATH = original_env_path

    def test_env_settings_labels_are_korean_for_ai_fields(self):
        original_env_path = dashboard.ENV_PATH
        try:
            dashboard.ENV_PATH = MemoryTextPath("AI_STRATEGY_ENABLED=false\nOPENAI_MODEL=gpt-5-mini\n")
            data = dashboard.get_env_settings()
            labels = {field["key"]: field["label"] for field in data["fields"]}

            self.assertEqual(labels["AI_STRATEGY_ENABLED"], "AI 전략 사용")
            self.assertEqual(labels["OPENAI_MODEL"], "OpenAI 모델")
            self.assertEqual(labels["TRADING_ENV"], "거래 환경")
        finally:
            dashboard.ENV_PATH = original_env_path

    def test_env_settings_use_runtime_defaults_for_missing_ai_values(self):
        original_env_path = dashboard.ENV_PATH
        original_ai_enabled = dashboard.trader.config.ai_strategy_enabled
        original_backtest = dashboard.trader.config.ai_require_backtest_pass
        original_model = dashboard.trader.config.openai_model
        try:
            dashboard.ENV_PATH = MemoryTextPath("TRADING_ENV=demo\n")
            dashboard.trader.config.ai_strategy_enabled = False
            dashboard.trader.config.ai_require_backtest_pass = True
            dashboard.trader.config.openai_model = "gpt-5-mini"

            data = dashboard.get_env_settings()
            values = {field["key"]: field["value"] for field in data["fields"]}

            self.assertEqual(values["AI_STRATEGY_ENABLED"], "false")
            self.assertEqual(values["AI_REQUIRE_BACKTEST_PASS"], "true")
            self.assertEqual(values["OPENAI_MODEL"], "gpt-5-mini")
        finally:
            dashboard.ENV_PATH = original_env_path
            dashboard.trader.config.ai_strategy_enabled = original_ai_enabled
            dashboard.trader.config.ai_require_backtest_pass = original_backtest
            dashboard.trader.config.openai_model = original_model

    def test_config_response_returns_account(self):
        original_account = dashboard.trader.config.kistock_account
        try:
            dashboard.trader.config.kistock_account = "1234567801"
            config = dashboard.get_config()
            self.assertEqual(config["kistock_account"], "1234567801")
            self.assertEqual(config["ai_analysis"]["account"], "1234567801")
            self.assertEqual(config["ai_analysis"]["account_priority"], "current_kis_account")
            self.assertEqual(config["ai_analysis"]["provider"], "openai_responses")
        finally:
            dashboard.trader.config.kistock_account = original_account

    def test_demo_trading_readiness_requires_demo_submission_without_live_switch(self):
        original_values = {
            "trading_env": dashboard.trader.TRADING_ENV,
            "dry_run": dashboard.trader.DRY_RUN,
            "enable_live_trading": dashboard.trader.ENABLE_LIVE_TRADING,
            "order_submission_enabled": dashboard.trader.ORDER_SUBMISSION_ENABLED,
            "real_orders_enabled": dashboard.trader.REAL_ORDERS_ENABLED,
            "account": dashboard.trader.config.kistock_account,
        }
        original_required_env_missing = dashboard._required_env_missing
        try:
            dashboard.trader.TRADING_ENV = "demo"
            dashboard.trader.DRY_RUN = False
            dashboard.trader.ENABLE_LIVE_TRADING = False
            dashboard.trader.ORDER_SUBMISSION_ENABLED = True
            dashboard.trader.REAL_ORDERS_ENABLED = False
            dashboard.trader.config.kistock_account = "1234567801"
            dashboard._required_env_missing = lambda: []

            readiness = dashboard.get_demo_trading_readiness()

            self.assertTrue(readiness["ready"])
            self.assertTrue(all(check["ok"] for check in readiness["checks"] if check["critical"]))
            self.assertFalse(readiness["real_orders_enabled"])
        finally:
            dashboard.trader.TRADING_ENV = original_values["trading_env"]
            dashboard.trader.DRY_RUN = original_values["dry_run"]
            dashboard.trader.ENABLE_LIVE_TRADING = original_values["enable_live_trading"]
            dashboard.trader.ORDER_SUBMISSION_ENABLED = original_values["order_submission_enabled"]
            dashboard.trader.REAL_ORDERS_ENABLED = original_values["real_orders_enabled"]
            dashboard.trader.config.kistock_account = original_values["account"]
            dashboard._required_env_missing = original_required_env_missing

    def test_env_update_applies_strategy_settings_without_restart(self):
        original_env_path = dashboard.ENV_PATH
        original_total_capital = dashboard.trader.TOTAL_CAPITAL
        original_max_single_weight = dashboard.trader.MAX_SINGLE_WEIGHT
        original_config_total_capital = dashboard.trader.config.total_capital
        original_account_initial_capital = dashboard.trader.config.account_initial_capital
        original_config_max_single_weight = dashboard.trader.config.max_single_weight
        try:
            path = MemoryTextPath("TOTAL_CAPITAL=10000000\nACCOUNT_INITIAL_CAPITAL=0\nMAX_SINGLE_WEIGHT=0.3\n")
            dashboard.ENV_PATH = path

            result = dashboard.update_env_settings({
                "values": {
                    "TOTAL_CAPITAL": "12000000",
                    "ACCOUNT_INITIAL_CAPITAL": "500000000",
                    "MAX_SINGLE_WEIGHT": "0.25",
                }
            })

            self.assertFalse(result["requires_restart"])
            self.assertEqual(dashboard.trader.TOTAL_CAPITAL, 12000000.0)
            self.assertEqual(dashboard.trader.config.total_capital, 12000000.0)
            self.assertEqual(dashboard.trader.config.account_initial_capital, 500000000.0)
            self.assertEqual(dashboard.trader.MAX_SINGLE_WEIGHT, 0.25)
            self.assertEqual(dashboard.trader.config.max_single_weight, 0.25)
            self.assertIn("TOTAL_CAPITAL=12000000", path.content)
            self.assertIn("ACCOUNT_INITIAL_CAPITAL=500000000", path.content)
            self.assertIn("MAX_SINGLE_WEIGHT=0.25", path.content)
        finally:
            dashboard.ENV_PATH = original_env_path
            dashboard.trader.TOTAL_CAPITAL = original_total_capital
            dashboard.trader.MAX_SINGLE_WEIGHT = original_max_single_weight
            dashboard.trader.config.total_capital = original_config_total_capital
            dashboard.trader.config.account_initial_capital = original_account_initial_capital
            dashboard.trader.config.max_single_weight = original_config_max_single_weight

    def test_env_update_saves_openai_strategy_settings(self):
        original_env_path = dashboard.ENV_PATH
        original_ai_enabled = dashboard.trader.config.ai_strategy_enabled
        original_openai_key = dashboard.trader.config.openai_api_key
        original_openai_model = dashboard.trader.config.openai_model
        try:
            path = MemoryTextPath("AI_STRATEGY_ENABLED=false\nOPENAI_MODEL=gpt-5-mini\n")
            dashboard.ENV_PATH = path

            result = dashboard.update_env_settings({
                "values": {
                    "AI_STRATEGY_ENABLED": "true",
                    "OPENAI_API_KEY": "sk-test-openai-key",
                    "OPENAI_MODEL": "gpt-5-mini",
                }
            })

            self.assertFalse(result["requires_restart"])
            self.assertTrue(dashboard.trader.config.ai_strategy_enabled)
            self.assertEqual(dashboard.trader.config.openai_api_key, "sk-test-openai-key")
            self.assertEqual(dashboard.trader.config.openai_model, "gpt-5-mini")
            self.assertIn("AI_STRATEGY_ENABLED=true", path.content)
            self.assertIn("OPENAI_API_KEY=sk-test-openai-key", path.content)
            self.assertIn("OPENAI_MODEL=gpt-5-mini", path.content)
        finally:
            dashboard.ENV_PATH = original_env_path
            dashboard.trader.config.ai_strategy_enabled = original_ai_enabled
            dashboard.trader.config.openai_api_key = original_openai_key
            dashboard.trader.config.openai_model = original_openai_model

    def test_kis_account_validation_accepts_8_or_10_digits(self):
        self.assertEqual(dashboard._validate_env_value("KISTOCK_ACCOUNT", "12345678"), "12345678")
        self.assertEqual(dashboard._validate_env_value("KISTOCK_ACCOUNT", "12345678-01"), "1234567801")
        with self.assertRaises(dashboard.HTTPException):
            dashboard._validate_env_value("KISTOCK_ACCOUNT", "1234567")

    def test_required_env_missing_accepts_8_digit_account(self):
        original_account = dashboard.trader.config.kistock_account
        try:
            dashboard.trader.config.kistock_account = "12345678"
            missing = dashboard._required_env_missing()
            self.assertNotIn("KISTOCK_ACCOUNT_FORMAT", missing)
        finally:
            dashboard.trader.config.kistock_account = original_account

    def test_balance_route_accepts_8_digit_account(self):
        original_account = dashboard.trader.config.kistock_account
        original_required_env_missing = dashboard._required_env_missing
        original_get_api = dashboard._get_api
        original_get_balance_data = dashboard._get_balance_data
        try:
            dashboard.trader.config.kistock_account = "12345678"
            dashboard._required_env_missing = lambda: []
            dashboard._get_api = lambda: object()
            dashboard._get_balance_data = lambda api: {
                "output1": [],
                "output2": [{"dnca_tot_amt": "1000", "tot_evlu_amt": "1000"}],
            }

            balance = dashboard.get_balance()

            self.assertEqual(balance["cash"], 1000)
        finally:
            dashboard.trader.config.kistock_account = original_account
            dashboard._required_env_missing = original_required_env_missing
            dashboard._get_api = original_get_api
            dashboard._get_balance_data = original_get_balance_data

    def test_balance_hides_holdings_with_active_sell_approvals(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_required_env_missing = dashboard._required_env_missing
        original_get_api = dashboard._get_api
        original_get_balance_data = dashboard._get_balance_data
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                dashboard._required_env_missing = lambda: []
                dashboard._get_api = lambda: object()
                dashboard._get_balance_data = lambda api: {
                    "output1": [
                        {
                            "pdno": "005930",
                            "prdt_name": "Samsung",
                            "hldg_qty": "2",
                            "prpr": "70000",
                        },
                        {
                            "pdno": "000660",
                            "prdt_name": "SK Hynix",
                            "hldg_qty": "1",
                            "prpr": "120000",
                        },
                    ],
                    "output2": [{"dnca_tot_amt": "1000", "tot_evlu_amt": "331000"}],
                }

                dashboard._create_approval_row({
                    "symbol": "005930",
                    "name": "Samsung",
                    "action": "sell",
                    "qty": 2,
                    "price": 0,
                    "reason": "dashboard sell current holding",
                    "source": "dashboard_holding_sell",
                })

                balance = dashboard.get_balance()

                self.assertEqual([holding["symbol"] for holding in balance["holdings"]], ["000660"])
                self.assertEqual(balance["pending_sell_symbols"], ["005930"])

                with dashboard.trader.connect_db() as conn:
                    conn.execute("UPDATE approvals SET status = 'executed' WHERE symbol = '005930'")

                balance = dashboard.get_balance()

                self.assertEqual([holding["symbol"] for holding in balance["holdings"]], ["000660"])
                self.assertEqual(balance["pending_sell_symbols"], ["005930"])
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard._required_env_missing = original_required_env_missing
            dashboard._get_api = original_get_api
            dashboard._get_balance_data = original_get_balance_data

    def test_auto_approval_state_can_toggle(self):
        original_state = dashboard.AUTO_APPROVAL_STATE
        try:
            dashboard.AUTO_APPROVAL_STATE = MemoryCachePath()
            self.assertFalse(dashboard._auto_approval_enabled())

            dashboard._save_auto_approval(True)
            self.assertTrue(dashboard._auto_approval_enabled())

            dashboard._save_auto_approval(False)
            self.assertFalse(dashboard._auto_approval_enabled())
        finally:
            dashboard.AUTO_APPROVAL_STATE = original_state

    def test_candidate_cache_invalidates_when_strategy_profile_changes(self):
        original_cache = dashboard.CANDIDATE_CACHE
        try:
            dashboard.CANDIDATE_CACHE = MemoryCachePath()
            strategy_v1 = {
                "id": "strategy_a",
                "strategy_version": 1,
                "profile_hash": "hash-a",
            }
            strategy_v2 = {
                "id": "strategy_a",
                "strategy_version": 2,
                "profile_hash": "hash-b",
            }

            with patch("src.db.repository.load_ai_strategies", return_value=[strategy_v1]):
                dashboard._save_candidate_cache(2, [{"ticker": "005930"}], [], 1, "strategy_a", "opt")
                cached = dashboard._load_candidate_cache(2, "strategy_a", "opt")
                self.assertIsNotNone(cached)

            with patch("src.db.repository.load_ai_strategies", return_value=[strategy_v2]):
                self.assertIsNone(dashboard._load_candidate_cache(2, "strategy_a", "opt"))
        finally:
            dashboard.CANDIDATE_CACHE = original_cache

    def test_enabling_auto_approval_processes_pending_orders(self):
        original_state = dashboard.AUTO_APPROVAL_STATE
        original_db_path = dashboard.trader.config.trade_db_path
        original_get_api = dashboard._get_api
        original_save_trade = dashboard.trader.save_trade
        original_slack_order = dashboard._slack_order
        original_dry_run = dashboard.trader.DRY_RUN
        original_order_submission = dashboard.trader.ORDER_SUBMISSION_ENABLED

        class _FakeAPI:
            def place_order(self, symbol, order_type, price, qty):
                return {"rt_cd": "0", "msg1": "DRY_RUN"}

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.AUTO_APPROVAL_STATE = MemoryCachePath()
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                dashboard._get_api = lambda: _FakeAPI()
                dashboard.trader.save_trade = lambda *args, **kwargs: None
                dashboard._slack_order = lambda *args, **kwargs: None
                dashboard.trader.DRY_RUN = True
                dashboard.trader.ORDER_SUBMISSION_ENABLED = False

                created = dashboard.create_approval({
                    "symbol": "005930",
                    "name": "Samsung",
                    "action": "buy",
                    "qty": 1,
                    "price": 70000,
                    "reason": "test",
                    "source": "test",
                })
                self.assertEqual(created["status"], "pending")

                result = dashboard.set_auto_approval({"enabled": True})
                self.assertEqual(result["processed_count"], 1)

                approvals = dashboard.get_approvals()["approvals"]
                self.assertEqual(approvals[0]["status"], "executed")
                self.assertEqual(approvals[0]["response_msg"], "DRY_RUN")
        finally:
            dashboard.AUTO_APPROVAL_STATE = original_state
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard._get_api = original_get_api
            dashboard.trader.save_trade = original_save_trade
            dashboard._slack_order = original_slack_order
            dashboard.trader.DRY_RUN = original_dry_run
            dashboard.trader.ORDER_SUBMISSION_ENABLED = original_order_submission

    def test_approval_execution_is_claimed_once_and_records_broker_result(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_get_api = dashboard._get_api
        original_slack_order = dashboard._slack_order
        original_auto_approval_state = dashboard.AUTO_APPROVAL_STATE
        original_dry_run = dashboard.trader.DRY_RUN
        original_trading_env = dashboard.trader.TRADING_ENV
        original_order_submission = dashboard.trader.ORDER_SUBMISSION_ENABLED

        class _FakeAPI:
            def __init__(self):
                self.calls = 0

            def place_order(self, symbol, order_type, price, qty):
                self.calls += 1
                return {"rt_cd": "0", "msg1": "ok", "output": {"ODNO": "D12345"}}

        fake_api = _FakeAPI()

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.config.trade_db_path = db_path
                dashboard.AUTO_APPROVAL_STATE = MemoryCachePath()
                dashboard._get_api = lambda: fake_api
                dashboard._slack_order = lambda *args, **kwargs: None
                dashboard.trader.DRY_RUN = False
                dashboard.trader.TRADING_ENV = "demo"
                dashboard.trader.ORDER_SUBMISSION_ENABLED = True

                created = dashboard.create_approval({
                    "symbol": "005930",
                    "name": "Samsung",
                    "action": "buy",
                    "qty": 1,
                    "price": 70000,
                    "reason": "demo order",
                    "source": "test",
                })

                result = dashboard.approve_order(created["id"])
                self.assertEqual(result["status"], "executed")
                self.assertEqual(fake_api.calls, 1)

                with self.assertRaises(dashboard.HTTPException):
                    dashboard.approve_order(created["id"])
                self.assertEqual(fake_api.calls, 1)

                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute("SELECT * FROM trades").fetchall()

                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["broker_order_id"], "D12345")
                self.assertEqual(rows[0]["order_status"], "submitted")
                self.assertIn("KIS 모의투자 주문 접수 완료", rows[0]["response_msg"])
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard._get_api = original_get_api
            dashboard._slack_order = original_slack_order
            dashboard.AUTO_APPROVAL_STATE = original_auto_approval_state
            dashboard.trader.DRY_RUN = original_dry_run
            dashboard.trader.TRADING_ENV = original_trading_env
            dashboard.trader.ORDER_SUBMISSION_ENABLED = original_order_submission

    def test_get_approvals_reclaims_stale_executing_orders(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_stale_seconds = dashboard.AUTO_APPROVAL_STALE_EXECUTING_SECONDS

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                dashboard.AUTO_APPROVAL_STALE_EXECUTING_SECONDS = 0
                approval_id = dashboard.trader.queue_approval(
                    "005930",
                    "Samsung",
                    "buy",
                    1,
                    70000,
                    "test",
                )
                with dashboard.trader.connect_db() as conn:
                    conn.execute(
                        """
                        UPDATE approvals
                        SET status = 'executing',
                            response_msg = 'Submitting order to broker',
                            updated_at = '2000-01-01 00:00:00'
                        WHERE id = ?
                        """,
                        (approval_id,),
                    )

                approvals = dashboard.get_approvals()["approvals"]

                row = next(item for item in approvals if item["id"] == approval_id)
                self.assertEqual(row["status"], "failed")
                self.assertIn("Submitting order to broker", row["response_msg"])
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard.AUTO_APPROVAL_STALE_EXECUTING_SECONDS = original_stale_seconds

    def test_retry_approval_creates_pending_sell_for_remaining_sellable_qty(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_get_api = dashboard._get_api
        original_get_balance_data = dashboard._get_balance_data
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                db_path = f"{tmpdir}/trades.db"
                dashboard.trader.config.trade_db_path = db_path
                dashboard.trader.init_db()
                dashboard._get_api = lambda: object()
                approval_id = dashboard._create_approval_row({
                    "symbol": "005360",
                    "name": "Monami",
                    "action": "sell",
                    "qty": 10,
                    "price": 1782,
                    "reason": "sell all",
                    "source": "dashboard_sell_all",
                })
                with sqlite3.connect(db_path) as conn:
                    conn.execute("UPDATE approvals SET status = 'executed' WHERE id = ?", (approval_id,))
                dashboard.trader.save_trade(
                    "005360",
                    "Monami",
                    "sell",
                    10,
                    1782,
                    "sell all",
                    True,
                    True,
                    broker_result={"odno": "0001"},
                    order_status="partial",
                    filled_qty=4,
                    filled_price=1780,
                    source_approval_id=approval_id,
                )

                dashboard._get_balance_data = lambda api, allow_cache=False: {
                    "output1": [{
                        "pdno": "005360",
                        "prdt_name": "Monami",
                        "hldg_qty": "8",
                        "ord_psbl_qty": "3",
                        "prpr": "1700",
                    }],
                    "output2": [{}],
                }

                result = dashboard.retry_approval_order(approval_id)

                self.assertEqual(result["status"], "pending")
                self.assertEqual(result["qty"], 3)
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT * FROM approvals WHERE id = ?",
                        (result["id"],),
                    ).fetchone()
                self.assertEqual(row["symbol"], "005360")
                self.assertEqual(row["action"], "sell")
                self.assertEqual(row["qty"], 3)
                self.assertEqual(row["price"], 0)
                self.assertEqual(row["source"], "dashboard_retry")
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard._get_api = original_get_api
            dashboard._get_balance_data = original_get_balance_data

    def test_start_approval_batch_returns_immediately_and_tracks_progress(self):
        original_thread = dashboard.threading.Thread
        original_path = dashboard.APPROVAL_BATCH_RESULT_PATH
        started = []

        class FakeThread:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def start(self):
                started.append(self.kwargs)

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.APPROVAL_BATCH_RESULT_PATH = Path(tmpdir) / "approval-batch.json"
                dashboard.threading.Thread = FakeThread
                dashboard._approval_batch_state.clear()

                result = dashboard.start_approval_batch({
                    "action": "retry",
                    "approval_ids": [3, 3, 4],
                })

                self.assertEqual(result["status"], "running")
                self.assertEqual(result["total_count"], 2)
                self.assertEqual(result["processed_count"], 0)
                self.assertEqual(len(started), 1)
                self.assertTrue(started[0]["daemon"])
                status = dashboard.get_approval_batch_status()
                self.assertTrue(status["available"])
                self.assertEqual(status["job_id"], result["job_id"])
        finally:
            dashboard.threading.Thread = original_thread
            dashboard.APPROVAL_BATCH_RESULT_PATH = original_path
            dashboard._approval_batch_state.clear()

    def test_retry_approval_blocks_when_sellable_quantity_is_zero(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_get_api = dashboard._get_api
        original_get_balance_data = dashboard._get_balance_data
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                db_path = f"{tmpdir}/trades.db"
                dashboard.trader.config.trade_db_path = db_path
                dashboard.trader.init_db()
                dashboard._get_api = lambda: object()
                approval_id = dashboard._create_approval_row({
                    "symbol": "005360",
                    "name": "Monami",
                    "action": "sell",
                    "qty": 10,
                    "price": 1782,
                    "reason": "sell all",
                    "source": "dashboard_sell_all",
                })
                with sqlite3.connect(db_path) as conn:
                    conn.execute("UPDATE approvals SET status = 'failed' WHERE id = ?", (approval_id,))
                dashboard.trader.save_trade(
                    "005360",
                    "Monami",
                    "sell",
                    10,
                    1782,
                    "sell all",
                    False,
                    True,
                    broker_result={},
                    order_status="failed",
                    filled_qty=0,
                    source_approval_id=approval_id,
                )
                dashboard._get_balance_data = lambda api, allow_cache=False: {
                    "output1": [{
                        "pdno": "005360",
                        "prdt_name": "Monami",
                        "hldg_qty": "10",
                        "ord_psbl_qty": "0",
                    }],
                    "output2": [{}],
                }

                with self.assertRaises(dashboard.HTTPException) as ctx:
                    dashboard.retry_approval_order(approval_id)

                self.assertEqual(ctx.exception.status_code, 409)
                self.assertIn("sellable quantity is zero", str(ctx.exception.detail))
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard._get_api = original_get_api
            dashboard._get_balance_data = original_get_balance_data

    def test_cancel_retry_cancels_open_sell_order_and_creates_pending_retry(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_get_api = dashboard._get_api
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                db_path = f"{tmpdir}/trades.db"
                dashboard.trader.config.trade_db_path = db_path
                dashboard.trader.init_db()
                approval_id = dashboard._create_approval_row({
                    "symbol": "005360",
                    "name": "Monami",
                    "action": "sell",
                    "qty": 3,
                    "price": 1721,
                    "reason": "sell retry",
                    "source": "trader",
                })
                with sqlite3.connect(db_path) as conn:
                    conn.execute("UPDATE approvals SET status = 'failed' WHERE id = ?", (approval_id,))
                dashboard.trader.save_trade(
                    "005360",
                    "Monami",
                    "sell",
                    10,
                    1721,
                    "sell retry",
                    False,
                    True,
                    broker_result={},
                    order_status="failed",
                    source_approval_id=approval_id,
                )
                dashboard.trader.save_trade(
                    "005360",
                    "Monami",
                    "sell",
                    10,
                    1782,
                    "old open sell",
                    True,
                    True,
                    broker_order_id="0001",
                    order_status="filled",
                    filled_qty=0,
                )

                class FakeAPI:
                    def __init__(self):
                        self.canceled = []

                    def get_trade_history(self, start_date, end_date):
                        return [{
                            "ord_dt": "20260727",
                            "ord_tmd": "130203",
                            "odno": "0001",
                            "sll_buy_dvsn_cd": "01",
                            "pdno": "005360",
                            "ord_qty": "10",
                            "ord_unpr": "1782",
                            "tot_ccld_qty": "0",
                            "avg_prvs": "0",
                            "rmn_qty": "10",
                            "cncl_yn": "N",
                            "ord_gno_brno": "00950",
                        }]

                    def cancel_order(self, order_no, **kwargs):
                        self.canceled.append((order_no, kwargs))
                        return {"rt_cd": "0", "msg1": "cancel ok", "output": {"odno": order_no}}

                fake_api = FakeAPI()
                dashboard._get_api = lambda: fake_api

                result = dashboard.cancel_blocking_sell_and_retry_approval(approval_id)

                self.assertEqual(result["status"], "pending")
                self.assertEqual(result["qty"], 10)
                self.assertEqual(result["canceled_order_id"], "0001")
                self.assertEqual(fake_api.canceled[0][0], "0001")
                self.assertEqual(fake_api.canceled[0][1]["original_order_branch"], "00950")
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    canceled = conn.execute(
                        "SELECT order_status FROM trades WHERE broker_order_id = '0001'",
                    ).fetchone()
                    retry = conn.execute(
                        "SELECT source, status, action, qty, price FROM approvals WHERE id = ?",
                        (result["id"],),
                    ).fetchone()
                self.assertEqual(canceled["order_status"], "canceled")
                self.assertEqual(retry["source"], "dashboard_retry")
                self.assertEqual(retry["status"], "pending")
                self.assertEqual(retry["action"], "sell")
                self.assertEqual(retry["qty"], 10)
                self.assertEqual(retry["price"], 0)
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard._get_api = original_get_api

    def test_order_status_sync_reopens_filled_trade_when_history_has_remaining_qty(self):
        original_db_path = dashboard.trader.config.trade_db_path
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                db_path = f"{tmpdir}/trades.db"
                dashboard.trader.config.trade_db_path = db_path
                dashboard.trader.init_db()
                approval_id = dashboard._create_approval_row({
                    "symbol": "005360",
                    "name": "Monami",
                    "action": "sell",
                    "qty": 10,
                    "price": 1782,
                    "reason": "sell",
                    "source": "trader",
                })
                with sqlite3.connect(db_path) as conn:
                    conn.execute("UPDATE approvals SET status = 'executed' WHERE id = ?", (approval_id,))
                dashboard.trader.save_trade(
                    "005360",
                    "Monami",
                    "sell",
                    10,
                    1782,
                    "sell",
                    True,
                    True,
                    broker_order_id="0001",
                    order_status="filled",
                    filled_qty=10,
                    source_approval_id=approval_id,
                )

                order_date = dashboard.trader.datetime.now(dashboard.trader.KST).strftime("%Y%m%d")

                class FakeAPI:
                    def get_trade_history(self, start_date, end_date):
                        return [{
                            "ord_dt": order_date,
                            "ord_tmd": "130203",
                            "odno": "0001",
                            "sll_buy_dvsn_cd": "01",
                            "pdno": "005360",
                            "ord_qty": "10",
                            "ord_unpr": "1782",
                            "tot_ccld_qty": "0",
                            "avg_prvs": "0",
                            "rmn_qty": "10",
                            "cncl_yn": "N",
                        }]

                result = dashboard._sync_order_status_from_history(FakeAPI(), days=3)

                self.assertEqual(result["updated_count"], 1)
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute("SELECT order_status, filled_qty FROM trades").fetchone()
                self.assertEqual(row["order_status"], "open")
                self.assertEqual(row["filled_qty"], 0)
        finally:
            dashboard.trader.config.trade_db_path = original_db_path

    def test_order_status_sync_closes_canceled_history_order(self):
        original_db_path = dashboard.trader.config.trade_db_path
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.init_db()
                dashboard.trader.save_trade(
                    "005360",
                    "Monami",
                    "sell",
                    10,
                    1782,
                    "sell",
                    True,
                    True,
                    broker_order_id="0001",
                    order_status="open",
                    pre_order_qty=20,
                )
                order_date = dashboard.trader.datetime.now(dashboard.trader.KST).strftime("%Y%m%d")

                class _FakeAPI:
                    def get_trade_history(self, start_date, end_date):
                        return [{
                            "ord_dt": order_date,
                            "odno": "0001",
                            "sll_buy_dvsn_cd": "01",
                            "pdno": "005360",
                            "ord_qty": "10",
                            "tot_ccld_qty": "3",
                            "rmn_qty": "7",
                            "cncl_yn": "Y",
                        }]

                dashboard._sync_order_status_from_history(_FakeAPI(), days=1)

                with sqlite3.connect(dashboard.trader.config.trade_db_path) as conn:
                    row = conn.execute(
                        "SELECT order_status, filled_qty FROM trades"
                    ).fetchone()
                self.assertEqual(row, ("canceled", 3))
        finally:
            dashboard.trader.config.trade_db_path = original_db_path

    def test_order_status_sync_expires_prior_day_order_with_remainder(self):
        original_db_path = dashboard.trader.config.trade_db_path
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.init_db()
                yesterday = (
                    dashboard.trader.datetime.now(dashboard.trader.KST)
                    - dashboard.trader.timedelta(days=1)
                )
                trade_ts = yesterday.strftime("%Y-%m-%d 10:00:00")
                dashboard.trader.save_trade(
                    "005360",
                    "Monami",
                    "sell",
                    10,
                    1782,
                    "sell",
                    True,
                    True,
                    broker_order_id="0001",
                    order_status="open",
                    pre_order_qty=20,
                )
                with sqlite3.connect(dashboard.trader.config.trade_db_path) as conn:
                    conn.execute("UPDATE trades SET ts = ?", (trade_ts,))

                class _FakeAPI:
                    def get_trade_history(self, start_date, end_date):
                        return [{
                            "ord_dt": yesterday.strftime("%Y%m%d"),
                            "odno": "0001",
                            "sll_buy_dvsn_cd": "01",
                            "pdno": "005360",
                            "ord_qty": "10",
                            "tot_ccld_qty": "0",
                            "rmn_qty": "10",
                        }]

                dashboard._sync_order_status_from_history(_FakeAPI(), days=1)

                with sqlite3.connect(dashboard.trader.config.trade_db_path) as conn:
                    status = conn.execute("SELECT order_status FROM trades").fetchone()[0]
                self.assertEqual(status, "canceled")
        finally:
            dashboard.trader.config.trade_db_path = original_db_path

    def test_order_status_sync_marks_submitted_demo_order_filled(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_dry_run = dashboard.trader.DRY_RUN

        class _FakeAPI:
            def get_trade_history(self, start_date, end_date):
                return [{
                    "odno": "D12345",
                    "tot_ccld_qty": "1",
                    "avg_prvs": "70100",
                }]

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.config.trade_db_path = db_path
                dashboard.trader.DRY_RUN = False
                dashboard.trader.save_trade(
                    "005930",
                    "Samsung",
                    "buy",
                    1,
                    70000,
                    "demo order",
                    True,
                    True,
                    broker_order_id="D12345",
                    order_status="submitted",
                    filled_qty=0,
                    filled_price=0,
                )

                result = dashboard._sync_order_status_from_history(_FakeAPI(), days=1)

                self.assertEqual(result["checked_count"], 1)
                self.assertEqual(result["updated_count"], 1)
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute("SELECT * FROM trades").fetchone()
                self.assertEqual(row["order_status"], "filled")
                self.assertEqual(row["filled_qty"], 1)
                self.assertEqual(row["filled_price"], 70100)
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard.trader.DRY_RUN = original_dry_run

    def test_order_status_sync_does_not_match_reused_order_id_for_other_symbol(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_dry_run = dashboard.trader.DRY_RUN

        class _FakeAPI:
            def get_trade_history(self, start_date, end_date):
                return [{
                    "odno": "D12345",
                    "pdno": "000660",
                    "sll_buy_dvsn_cd": "02",
                    "ord_dt": "20260626",
                    "ord_tmd": "093015",
                    "tot_ccld_qty": "10",
                    "avg_prvs": "120000",
                }]

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.config.trade_db_path = db_path
                dashboard.trader.DRY_RUN = False
                dashboard.trader.save_trade(
                    "005930",
                    "Samsung",
                    "buy",
                    1,
                    70000,
                    "demo order",
                    True,
                    True,
                    broker_order_id="D12345",
                    order_status="submitted",
                    filled_qty=0,
                    filled_price=0,
                )

                result = dashboard._sync_order_status_from_history(_FakeAPI(), days=1)

                self.assertEqual(result["checked_count"], 1)
                self.assertEqual(result["updated_count"], 0)
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute("SELECT * FROM trades").fetchone()
                self.assertEqual(row["order_status"], "submitted")
                self.assertEqual(row["filled_qty"], 0)
                self.assertEqual(row["filled_price"], 0)
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard.trader.DRY_RUN = original_dry_run

    def test_order_status_sync_cancels_unmatched_sell_without_balance_reservation(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_get_balance_data = dashboard._get_balance_data
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.init_db()
                dashboard.trader.save_trade(
                    "005360",
                    "Monami",
                    "sell",
                    10,
                    1782,
                    "sell",
                    True,
                    True,
                    broker_order_id="S12345",
                    order_status="open",
                    pre_order_qty=20,
                )
                dashboard._get_balance_data = lambda api, allow_cache=False: {
                    "output1": [{
                        "pdno": "005360",
                        "prdt_name": "Monami",
                        "hldg_qty": "20",
                        "ord_psbl_qty": "20",
                        "prpr": "1700",
                    }],
                    "output2": [{}],
                }

                class _FakeAPI:
                    def get_trade_history(self, start_date, end_date):
                        return []

                result = dashboard._sync_order_status_from_history(_FakeAPI(), days=1)

                self.assertEqual(result["unmatched_count"], 1)
                self.assertEqual(result["updated_count"], 1)
                with sqlite3.connect(dashboard.trader.config.trade_db_path) as conn:
                    row = conn.execute("SELECT order_status FROM trades").fetchone()
                self.assertEqual(row[0], "canceled")
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard._get_balance_data = original_get_balance_data

    def test_order_status_sync_keeps_unmatched_sell_with_balance_reservation_open(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_get_balance_data = dashboard._get_balance_data
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.init_db()
                dashboard.trader.save_trade(
                    "005360",
                    "Monami",
                    "sell",
                    10,
                    1782,
                    "sell",
                    True,
                    True,
                    broker_order_id="S12345",
                    order_status="open",
                    pre_order_qty=20,
                )
                dashboard._get_balance_data = lambda api, allow_cache=False: {
                    "output1": [{
                        "pdno": "005360",
                        "prdt_name": "Monami",
                        "hldg_qty": "20",
                        "ord_psbl_qty": "10",
                        "prpr": "1700",
                    }],
                    "output2": [{}],
                }

                class _FakeAPI:
                    def get_trade_history(self, start_date, end_date):
                        return []

                result = dashboard._sync_order_status_from_history(_FakeAPI(), days=1)

                self.assertEqual(result["updated_count"], 0)
                with sqlite3.connect(dashboard.trader.config.trade_db_path) as conn:
                    row = conn.execute("SELECT order_status FROM trades").fetchone()
                self.assertEqual(row[0], "open")
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard._get_balance_data = original_get_balance_data

    def test_order_history_window_enforces_minimum_month_lookback(self):
        start_date, end_date = dashboard._order_history_window(1)
        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")

        self.assertGreaterEqual((end - start).days, 30)

    def test_order_status_sync_falls_back_to_balance_when_history_fails(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_dry_run = dashboard.trader.DRY_RUN
        original_get_balance_data = dashboard._get_balance_data

        class _FakeAPI:
            def get_trade_history(self, start_date, end_date):
                raise RuntimeError("history 500")

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.config.trade_db_path = db_path
                dashboard.trader.DRY_RUN = False
                dashboard._get_balance_data = lambda api, allow_cache=False: {
                    "output1": [
                        {
                            "pdno": "005930",
                            "prdt_name": "Samsung",
                            "hldg_qty": "6",
                            "prpr": "70200",
                            "evlu_amt": "421200",
                        }
                    ],
                    "output2": [{"dnca_tot_amt": "100000", "tot_evlu_amt": "521200"}],
                }
                dashboard.trader.save_trade(
                    "005930",
                    "Samsung",
                    "buy",
                    1,
                    70000,
                    "demo order",
                    True,
                    True,
                    broker_order_id="D12345",
                    order_status="submitted",
                    filled_qty=0,
                    filled_price=0,
                    pre_order_qty=5,
                )

                result = dashboard._sync_order_status_from_history(_FakeAPI(), days=1)

                self.assertEqual(result["fallback"], "balance")
                self.assertEqual(result["history_error"], "history 500")
                self.assertEqual(result["updated_count"], 1)
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute("SELECT * FROM trades").fetchone()
                self.assertEqual(row["order_status"], "filled")
                self.assertEqual(row["filled_qty"], 1)
                self.assertEqual(row["filled_price"], 70200)
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard.trader.DRY_RUN = original_dry_run
            dashboard._get_balance_data = original_get_balance_data

    def test_filled_trade_history_sync_imports_lookback_rows(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_fetch_cloud_trades = dashboard.fetch_cloud_trades

        class _FakeAPI:
            def __init__(self):
                self.window = None

            def get_trade_history(self, start_date, end_date):
                self.window = (start_date, end_date)
                return [
                    {
                        "odno": "H12345",
                        "pdno": "005930",
                        "prdt_name": "Samsung",
                        "sll_buy_dvsn_cd": "02",
                        "ord_dt": "20260520",
                        "ord_tmd": "093015",
                        "tot_ccld_qty": "3",
                        "avg_prvs": "70100",
                    }
                ]

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.config.trade_db_path = db_path
                dashboard.fetch_cloud_trades = lambda: []
                api = _FakeAPI()

                result = dashboard._sync_filled_trades_from_history(api, days=30)

                self.assertEqual(result["history_count"], 1)
                self.assertEqual(result["imported_count"], 1)
                self.assertNotEqual(api.window[0], api.window[1])
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute("SELECT * FROM trades").fetchone()
                self.assertEqual(row["ts"], "2026-05-20 09:30:15")
                self.assertEqual(row["symbol"], "005930")
                self.assertEqual(row["action"], "buy")
                self.assertEqual(row["qty"], 3)
                self.assertEqual(row["price"], 70100)
                self.assertEqual(row["order_status"], "filled")
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard.fetch_cloud_trades = original_fetch_cloud_trades

    def test_balance_sync_adjustment_is_reconciled_not_submitted(self):
        import src.dashboard.routes.stock as stock_routes

        original_db_path = dashboard.trader.config.trade_db_path
        original_get_api = stock_routes._get_api
        original_get_balance_data = stock_routes._get_balance_data
        original_fetch_cloud_trades = stock_routes.fetch_cloud_trades
        original_clear_balance_cache = stock_routes._clear_balance_cache
        original_dry_run = stock_routes.trader.DRY_RUN

        class _FakeAPI:
            def get_trade_history(self, start_date, end_date):
                return []

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.config.trade_db_path = db_path
                stock_routes._get_api = lambda: _FakeAPI()
                stock_routes._get_balance_data = lambda api, allow_cache=True: {
                    "output1": [
                        {
                            "pdno": "005360",
                            "prdt_name": "Monami",
                            "hldg_qty": "10",
                            "pchs_avg_pric": "1700",
                            "prpr": "1710",
                            "evlu_pfls_amt": "100",
                        }
                    ],
                    "output2": [{}],
                }
                stock_routes.fetch_cloud_trades = lambda: []
                stock_routes._clear_balance_cache = lambda: None
                stock_routes.trader.DRY_RUN = False

                result = stock_routes.sync_trades(days=1)

                self.assertEqual(result["balance_synced_count"], 1)
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute("SELECT * FROM trades").fetchone()
                self.assertEqual(row["symbol"], "005360")
                self.assertEqual(row["order_status"], "reconciled")
                self.assertEqual(row["broker_order_id"], "")
                self.assertEqual(row["filled_qty"], 10)
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            stock_routes._get_api = original_get_api
            stock_routes._get_balance_data = original_get_balance_data
            stock_routes.fetch_cloud_trades = original_fetch_cloud_trades
            stock_routes._clear_balance_cache = original_clear_balance_cache
            stock_routes.trader.DRY_RUN = original_dry_run

    def test_broker_sync_removes_only_local_mismatch_rows(self):
        import src.dashboard.routes.stock as stock_routes

        original_db_path = dashboard.trader.config.trade_db_path
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.init_db()
                dashboard.trader.save_trade(
                    "005360", "Monami", "sell", 10, 1700, "failed local order",
                    False, True, order_status="failed",
                )
                dashboard.trader.save_trade(
                    "005930", "Samsung", "buy", 2, 70000, "balance sync adjustment",
                    True, False, order_status="reconciled", filled_qty=2,
                )
                dashboard.trader.save_trade(
                    "000660", "SK Hynix", "buy", 1, 120000, "broker fill",
                    True, True, broker_order_id="B123", order_status="filled",
                    filled_qty=1,
                )

                result = stock_routes._remove_non_broker_trade_rows()

                self.assertEqual(result["removed_count"], 2)
                with sqlite3.connect(dashboard.trader.config.trade_db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT symbol, broker_order_id, order_status FROM trades"
                    ).fetchall()
                self.assertEqual(
                    [dict(row) for row in rows],
                    [{
                        "symbol": "000660",
                        "broker_order_id": "B123",
                        "order_status": "filled",
                    }],
                )
        finally:
            dashboard.trader.config.trade_db_path = original_db_path

    def test_broker_sync_reports_removed_mismatch_rows(self):
        import src.dashboard.routes.stock as stock_routes

        original_db_path = dashboard.trader.config.trade_db_path
        original_get_api = stock_routes._get_api
        original_get_balance_data = stock_routes._get_balance_data
        original_fetch_cloud_trades = stock_routes.fetch_cloud_trades
        original_clear_balance_cache = stock_routes._clear_balance_cache
        original_dry_run = stock_routes.trader.DRY_RUN
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                dashboard.trader.init_db()
                dashboard.trader.save_trade(
                    "005360", "Monami", "sell", 10, 1700, "failed local order",
                    False, True, order_status="failed",
                )

                class _FakeAPI:
                    calls = 0

                    def get_trade_history(self, start_date, end_date):
                        self.calls += 1
                        return []

                fake_api = _FakeAPI()
                stock_routes._get_api = lambda: fake_api
                stock_routes._get_balance_data = lambda api, allow_cache=False: {
                    "output1": [],
                    "output2": [{}],
                }
                stock_routes.fetch_cloud_trades = lambda: []
                stock_routes._clear_balance_cache = lambda: None
                stock_routes.trader.DRY_RUN = False

                result = stock_routes.sync_trades(days=1)

                self.assertEqual(result["removed_mismatch_count"], 1)
                self.assertTrue(result["order_status_sync"]["ok"])
                self.assertEqual(fake_api.calls, 1)
                with sqlite3.connect(dashboard.trader.config.trade_db_path) as conn:
                    count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                self.assertEqual(count, 0)
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            stock_routes._get_api = original_get_api
            stock_routes._get_balance_data = original_get_balance_data
            stock_routes.fetch_cloud_trades = original_fetch_cloud_trades
            stock_routes._clear_balance_cache = original_clear_balance_cache
            stock_routes.trader.DRY_RUN = original_dry_run

    def test_trade_sync_status_persists_compact_result(self):
        import src.dashboard.routes.stock as stock_routes

        original_path = stock_routes.TRADE_SYNC_RESULT_PATH
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                stock_routes.TRADE_SYNC_RESULT_PATH = Path(tmpdir) / "last-sync.json"
                stock_routes._save_trade_sync_result({
                    "ok": True,
                    "synced_count": 12,
                    "balance_synced_count": 12,
                    "history_imported_count": 0,
                    "history_updated_count": 780,
                    "removed_mismatch_count": 13,
                    "history_error": None,
                    "order_status_error": None,
                    "history_sync": {"orders": [{"large": "payload"}]},
                })

                result = stock_routes.get_trade_sync_status()

                self.assertTrue(result["available"])
                self.assertEqual(result["synced_count"], 12)
                self.assertEqual(result["removed_mismatch_count"], 13)
                self.assertIn("completed_at", result)
                self.assertNotIn("history_sync", result)
        finally:
            stock_routes.TRADE_SYNC_RESULT_PATH = original_path

    def test_sell_all_holdings_queues_market_sell_for_each_current_holding(self):
        original_db_path = dashboard.trader.config.trade_db_path
        original_get_api = dashboard._get_api
        original_get_balance_data = dashboard._get_balance_data
        original_auto_approval = dashboard._auto_approval_enabled
        original_clear_balance_cache = dashboard._clear_balance_cache
        original_required_env_missing = dashboard._required_env_missing

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                dashboard._get_api = lambda: object()
                dashboard._get_balance_data = lambda api, allow_cache=True: {
                    "output1": [
                        {
                            "pdno": "005930",
                            "prdt_name": "Samsung",
                            "hldg_qty": "2",
                            "prpr": "70000",
                        },
                        {
                            "pdno": "000660",
                            "prdt_name": "SK Hynix",
                            "hldg_qty": "0",
                            "prpr": "120000",
                        },
                    ],
                    "output2": [{}],
                }
                dashboard._auto_approval_enabled = lambda: False
                dashboard._clear_balance_cache = lambda: None
                dashboard._required_env_missing = lambda: []

                result = dashboard.sell_all_holdings({})

                self.assertEqual(result["created_count"], 1)
                self.assertEqual(result["pending_count"], 1)
                self.assertEqual(result["submitted_count"], 0)
                self.assertIn("주문 접수", result["fill_status_note"])
                approvals = dashboard.get_approvals()["approvals"]
                self.assertEqual(approvals[0]["symbol"], "005930")
                self.assertEqual(approvals[0]["action"], "sell")
                self.assertEqual(approvals[0]["qty"], 2)
                self.assertEqual(approvals[0]["price"], 0)
                self.assertEqual(approvals[0]["source"], "dashboard_sell_all")
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            dashboard._get_api = original_get_api
            dashboard._get_balance_data = original_get_balance_data
            dashboard._auto_approval_enabled = original_auto_approval
            dashboard._clear_balance_cache = original_clear_balance_cache
            dashboard._required_env_missing = original_required_env_missing

    def test_sell_all_holdings_registers_pending_rows_before_auto_approval(self):
        import src.dashboard.routes.stock as stock_routes

        original_db_path = dashboard.trader.config.trade_db_path
        original_get_api = stock_routes._get_api
        original_get_balance_data = stock_routes._get_balance_data
        original_auto_approval = stock_routes._auto_approval_enabled
        original_batch_async = stock_routes._run_auto_approval_batch_async
        original_clear_balance_cache = stock_routes._clear_balance_cache
        original_required_env_missing = stock_routes._required_env_missing

        queued_ids = []
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                stock_routes._get_api = lambda: object()
                stock_routes._get_balance_data = lambda api, allow_cache=True: {
                    "output1": [
                        {
                            "pdno": "005930",
                            "prdt_name": "Samsung",
                            "hldg_qty": "2",
                            "prpr": "70000",
                        },
                    ],
                    "output2": [{}],
                }
                stock_routes._auto_approval_enabled = lambda: True
                stock_routes._run_auto_approval_batch_async = lambda approval_ids: queued_ids.extend(approval_ids)
                stock_routes._clear_balance_cache = lambda: None
                stock_routes._required_env_missing = lambda: []

                result = stock_routes.sell_all_holdings({})

                self.assertEqual(result["created_count"], 1)
                self.assertEqual(result["pending_count"], 1)
                self.assertEqual(result["submitted_count"], 0)
                self.assertTrue(result["auto_approval_queued"])
                approvals = stock_routes.get_approvals()["approvals"]
                self.assertEqual(approvals[0]["status"], "pending")
                self.assertTrue(approvals[0]["auto_approval_in_progress"])
                self.assertEqual(queued_ids, [approvals[0]["id"]])
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            stock_routes._get_api = original_get_api
            stock_routes._get_balance_data = original_get_balance_data
            stock_routes._auto_approval_enabled = original_auto_approval
            stock_routes._run_auto_approval_batch_async = original_batch_async
            stock_routes._clear_balance_cache = original_clear_balance_cache
            stock_routes._required_env_missing = original_required_env_missing

    def test_holding_sell_queues_before_auto_approval(self):
        import src.dashboard.routes.stock as stock_routes

        original_db_path = dashboard.trader.config.trade_db_path
        original_auto_approval = stock_routes._auto_approval_enabled
        original_batch_async = stock_routes._run_auto_approval_batch_async

        queued_ids = []
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                stock_routes._auto_approval_enabled = lambda: True
                stock_routes._run_auto_approval_batch_async = lambda approval_ids: queued_ids.extend(approval_ids)

                result = stock_routes.create_approval({
                    "symbol": "005930",
                    "name": "Samsung",
                    "action": "sell",
                    "qty": 2,
                    "price": 0,
                    "reason": "dashboard sell current holding",
                    "source": "dashboard_holding_sell",
                })

                self.assertEqual(result["status"], "pending")
                self.assertFalse(result["auto_approved"])
                self.assertTrue(result["auto_approval_queued"])
                approvals = stock_routes.get_approvals()["approvals"]
                self.assertEqual(queued_ids, [approvals[0]["id"]])
                self.assertTrue(approvals[0]["auto_approval_in_progress"])
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            stock_routes._auto_approval_enabled = original_auto_approval
            stock_routes._run_auto_approval_batch_async = original_batch_async

    def test_sell_all_holdings_uses_sellable_quantity_and_skips_locked_holdings(self):
        import src.dashboard.routes.stock as stock_routes

        original_db_path = dashboard.trader.config.trade_db_path
        original_get_api = stock_routes._get_api
        original_get_balance_data = stock_routes._get_balance_data
        original_auto_approval = stock_routes._auto_approval_enabled
        original_clear_balance_cache = stock_routes._clear_balance_cache
        original_required_env_missing = stock_routes._required_env_missing

        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = f"{tmpdir}/trades.sqlite"
                stock_routes._get_api = lambda: object()
                stock_routes._get_balance_data = lambda api, allow_cache=True: {
                    "output1": [
                        {
                            "pdno": "005930",
                            "prdt_name": "Samsung",
                            "hldg_qty": "10",
                            "ord_psbl_qty": "3",
                            "prpr": "70000",
                        },
                        {
                            "pdno": "000660",
                            "prdt_name": "SK Hynix",
                            "hldg_qty": "5",
                            "ord_psbl_qty": "0",
                            "prpr": "120000",
                        },
                    ],
                    "output2": [{}],
                }
                stock_routes._auto_approval_enabled = lambda: False
                stock_routes._clear_balance_cache = lambda: None
                stock_routes._required_env_missing = lambda: []

                result = stock_routes.sell_all_holdings({})

                self.assertEqual(result["created_count"], 1)
                self.assertEqual(result["skipped_count"], 1)
                self.assertEqual(result["skipped"][0]["symbol"], "000660")
                approvals = stock_routes.get_approvals()["approvals"]
                self.assertEqual(approvals[0]["symbol"], "005930")
                self.assertEqual(approvals[0]["qty"], 3)
        finally:
            dashboard.trader.config.trade_db_path = original_db_path
            stock_routes._get_api = original_get_api
            stock_routes._get_balance_data = original_get_balance_data
            stock_routes._auto_approval_enabled = original_auto_approval
            stock_routes._clear_balance_cache = original_clear_balance_cache
            stock_routes._required_env_missing = original_required_env_missing

    def test_watchlist_inherits_shared_symbols_for_non_isolated_strategy(self):
        with patch("src.db.repository.load_watchlist_data", return_value={
            "symbols": ["005930", "000660"],
            "ai_auto_add": False,
            "ai_auto_add_threshold": 3.0,
        }), patch(
            "src.db.repository.load_strategy_universe_symbols",
            return_value=[],
        ), patch(
            "src.db.repository.get_watchlist_extra_info",
            return_value={
                "price": 0,
                "score": 0,
                "reason": "",
                "change_rate": None,
                "rsi": None,
                "updated_at": "",
            },
        ):
            result = dashboard.get_watchlist("rsi_limit_strategy")

        self.assertTrue(result["inherited"])
        self.assertEqual(result["universe_source"], "shared")
        self.assertEqual([item["symbol"] for item in result["symbols"]], ["005930", "000660"])

    def test_watchlist_view_inherits_shared_symbols_for_empty_isolated_strategy(self):
        with patch("src.db.repository.load_watchlist_data", return_value={
            "symbols": ["005930"],
            "ai_auto_add": False,
            "ai_auto_add_threshold": 3.0,
        }), patch(
            "src.db.repository.load_strategy_universe_symbols",
            return_value=[],
        ):
            result = dashboard.get_watchlist("plunge_bounce_strategy")

        self.assertTrue(result["inherited"])
        self.assertEqual(result["universe_source"], "shared")
        self.assertEqual([item["symbol"] for item in result["symbols"]], ["005930"])

    def test_watchlist_replaces_placeholder_name_with_metadata(self):
        with patch("src.db.repository.load_watchlist_data", return_value={
            "symbols": ["002700"],
            "names": {"002700": "우량 종목"},
            "ai_auto_add": False,
            "ai_auto_add_threshold": 3.0,
        }), patch(
            "src.db.repository.get_watchlist_extra_info",
            return_value={
                "price": 0,
                "score": 0,
                "reason": "",
                "change_rate": None,
                "rsi": None,
                "updated_at": "",
            },
        ), patch(
            "src.market_metadata.load_kr_stock_metadata",
            return_value={"002700": {"name": "신일전자", "sector": "전기제품"}},
        ):
            result = dashboard.get_watchlist()

        self.assertEqual(result["symbols"][0]["name"], "신일전자")
        self.assertEqual(result["symbols"][0]["sector"], "전기제품")

    def test_watchlist_scan_uses_threshold_from_same_request(self):
        saved = []
        with patch.object(dashboard, "_required_env_missing", return_value=[]), \
                patch.object(dashboard, "_get_api", return_value=object()), \
                patch.object(dashboard, "_get_balance_data", return_value={"output1": [], "output2": [{}]}), \
                patch.object(dashboard, "_parse_balance", return_value={"holdings": []}), \
                patch("src.dashboard.core.build_dashboard_candidates", return_value={
                    "scanned": 3,
                    "candidates": [
                        {"ticker": "005930", "name": "Samsung", "score": 1.0},
                        {"ticker": "000660", "name": "SK Hynix", "score": 2.0},
                        {"ticker": "035420", "name": "NAVER", "score": 3.0},
                    ],
                }), \
                patch("src.db.repository.load_watchlist_data", return_value={
                    "symbols": ["000660"],
                    "ai_auto_add": True,
                    "ai_auto_add_threshold": 1.0,
                }), \
                patch("src.db.repository.save_watchlist_data", side_effect=lambda data: saved.append(dict(data))), \
                patch("src.strategy.seven_split.sync_watchlist_runtime"):
            result = asyncio.run(dashboard.trigger_watchlist_ai_scan(
                dashboard.WatchlistTogglePayload(enabled=True, threshold=2.0)
            ))

        self.assertEqual(result["threshold_used"], 2.0)
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(result["already_registered_count"], 1)
        self.assertEqual(result["added_count"], 1)
        self.assertEqual(result["added_symbols"][0]["symbol"], "035420")
        self.assertEqual(saved[0]["ai_auto_add_threshold"], 2.0)

    def test_watchlist_scan_removes_scanned_symbols_below_threshold(self):
        saved = []
        with patch.object(dashboard, "_required_env_missing", return_value=[]), \
                patch.object(dashboard, "_get_api", return_value=object()), \
                patch.object(dashboard, "_get_balance_data", return_value={"output1": [], "output2": [{}]}), \
                patch.object(dashboard, "_parse_balance", return_value={"holdings": []}), \
                patch("src.dashboard.core.build_dashboard_candidates", return_value={
                    "scanned": 3,
                    "candidates": [
                        {"ticker": "000660", "name": "SK Hynix", "score": 2.0},
                        {"ticker": "035420", "name": "NAVER", "score": 3.0},
                    ],
                    "scan_summary": [
                        {"ticker": "005930", "name": "Samsung", "score": 1.0},
                        {"ticker": "000660", "name": "SK Hynix", "score": 2.0},
                        {"ticker": "035420", "name": "NAVER", "score": 3.0},
                    ],
                }), \
                patch("src.db.repository.load_watchlist_data", return_value={
                    "symbols": ["005930", "000660"],
                    "ai_auto_add": True,
                    "ai_auto_add_threshold": 2.0,
                }), \
                patch("src.db.repository.save_watchlist_data", side_effect=lambda data: saved.append(dict(data))), \
                patch("src.strategy.seven_split.sync_watchlist_runtime"):
            result = asyncio.run(dashboard.trigger_watchlist_ai_scan(None))

        self.assertEqual(result["threshold_used"], 2.0)
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["removed_symbols"][0]["symbol"], "005930")
        self.assertEqual(result["added_count"], 1)
        self.assertEqual(result["added_symbols"][0]["symbol"], "035420")
        self.assertEqual(saved[-1]["symbols"], ["000660", "035420"])

    def test_watchlist_scan_removes_two_point_symbols_when_auto_add_threshold_is_higher(self):
        saved = []
        with patch.object(dashboard, "_required_env_missing", return_value=[]), \
                patch.object(dashboard, "_get_api", return_value=object()), \
                patch.object(dashboard, "_get_balance_data", return_value={"output1": [], "output2": [{}]}), \
                patch.object(dashboard, "_parse_balance", return_value={"holdings": []}), \
                patch("src.dashboard.core.build_dashboard_candidates", return_value={
                    "scanned": 3,
                    "candidates": [
                        {"ticker": "035420", "name": "NAVER", "score": 3.0},
                    ],
                    "scan_summary": [
                        {"ticker": "005930", "name": "Samsung", "score": 1.0},
                        {"ticker": "000660", "name": "SK Hynix", "score": 2.0},
                        {"ticker": "035420", "name": "NAVER", "score": 3.0},
                    ],
                }), \
                patch("src.db.repository.load_watchlist_data", return_value={
                    "symbols": ["005930", "000660"],
                    "ai_auto_add": True,
                    "ai_auto_add_threshold": 3.0,
                }), \
                patch("src.db.repository.save_watchlist_data", side_effect=lambda data: saved.append(dict(data))), \
                patch("src.strategy.seven_split.sync_watchlist_runtime"):
            result = asyncio.run(dashboard.trigger_watchlist_ai_scan(None))

        self.assertEqual(result["threshold_used"], 3.0)
        self.assertEqual([item["symbol"] for item in result["removed_symbols"]], ["005930", "000660"])
        self.assertEqual(saved[-1]["symbols"], ["035420"])

    def test_candidate_orders_use_scan_price_without_quote_lookup(self):
        original_max_positions = dashboard.trader.MAX_POSITIONS
        try:
            dashboard.trader.MAX_POSITIONS = 1
            orders = dashboard._build_candidate_orders_from_scan(
                [
                    {"ticker": "005930", "current_price": 70003, "score": 2, "reasons": ["test"]},
                    {"ticker": "000660", "current_price": 100000, "score": 2, "reasons": ["test"]},
                ],
                held_count=0,
                cash=1_000_000,
            )
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["ticker"], "005930")
            self.assertEqual(orders[0]["limit_price"], 70000)
            self.assertLessEqual(orders[0]["estimated_cost"], 1_000_000)
        finally:
            dashboard.trader.MAX_POSITIONS = original_max_positions


if __name__ == "__main__":
    unittest.main()
