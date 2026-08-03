import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response

from src.config import config
from src.db.performance_repository import (
    get_strategy_performance_review,
    build_account_equity_performance,
    list_daily_nav,
    list_account_cashflows,
    record_account_cashflow,
    record_account_equity_snapshot,
    replace_daily_nav,
    save_strategy_performance_review,
)
from src.strategy.forward_performance import build_strategy_forward_performance
from src.dashboard.routes import stock
from src.dashboard import core as dashboard_core
from src.db import repository


class ForwardPerformanceTests(unittest.TestCase):
    def test_cash_flow_matched_benchmark_and_strategy_return(self):
        trades = [
            {"ts": "2026-01-02 10:00:00", "strategy_id": "alpha", "symbol": "005930", "action": "buy", "qty": 10, "price": 100},
            {"ts": "2026-01-03 10:00:00", "strategy_id": "alpha", "symbol": "005930", "action": "sell", "qty": 5, "price": 120},
        ]
        prices = {"005930": [{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 120}]}
        benchmarks = {
            "KOSPI": [{"date": "2026-01-01", "close": 1000}, {"date": "2026-01-03", "close": 1050}],
            "KOSDAQ": [{"date": "2026-01-01", "close": 500}, {"date": "2026-01-03", "close": 500}],
        }

        row = build_strategy_forward_performance(
            trades, prices, benchmarks, as_of="2026-01-03"
        )[0]

        self.assertEqual(row["net_contribution"], 1000)
        self.assertEqual(row["current_equity"], 1200)
        self.assertEqual(row["return_pct"], 20.0)
        self.assertEqual(row["kospi_return_pct"], 5.0)
        self.assertEqual(row["excess_vs_kospi_pct"], 15.0)
        self.assertEqual(row["data_quality"], "estimated")
        self.assertIn("costs_not_included", row["quality_issues"])

    def test_later_contribution_buys_benchmark_at_later_close(self):
        trades = [
            {"ts": "2026-01-02", "strategy_id": "alpha", "symbol": "A", "action": "buy", "qty": 1, "price": 100},
            {"ts": "2026-01-03", "strategy_id": "alpha", "symbol": "B", "action": "buy", "qty": 1, "price": 100},
        ]
        prices = {
            "A": [{"date": "2026-01-03", "close": 100}],
            "B": [{"date": "2026-01-03", "close": 100}],
        }
        benchmarks = {
            "KOSPI": [{"date": "2026-01-01", "close": 100}, {"date": "2026-01-02", "close": 200}, {"date": "2026-01-03", "close": 200}],
            "KOSDAQ": [{"date": "2026-01-01", "close": 100}, {"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 100}],
        }

        row = build_strategy_forward_performance(
            trades, prices, benchmarks, as_of="2026-01-03"
        )[0]

        self.assertEqual(row["kospi_return_pct"], 50.0)
        self.assertEqual(row["return_pct"], 0.0)

    def test_as_of_excludes_future_trades_and_prices(self):
        trades = [
            {"ts": "2026-01-02", "strategy_id": "alpha", "symbol": "A", "action": "buy", "qty": 1, "price": 100},
            {"ts": "2026-01-05", "strategy_id": "alpha", "symbol": "A", "action": "sell", "qty": 1, "price": 500},
        ]
        prices = {"A": [{"date": "2026-01-03", "close": 110}, {"date": "2026-01-05", "close": 500}]}
        benchmarks = {
            "KOSPI": [{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 110}, {"date": "2026-01-05", "close": 500}],
            "KOSDAQ": [{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 100}],
        }

        row = build_strategy_forward_performance(
            trades, prices, benchmarks, as_of="2026-01-03"
        )[0]

        self.assertEqual(row["current_equity"], 110)
        self.assertEqual(row["return_pct"], 10.0)
        self.assertEqual(row["order_count"], 1)

    def test_missing_close_is_explicitly_estimated(self):
        trades = [{"ts": "2026-01-02", "strategy_id": "alpha", "symbol": "A", "action": "buy", "qty": 1, "price": 100}]
        row = build_strategy_forward_performance(trades, {}, {}, as_of="2026-01-03")[0]
        self.assertEqual(row["data_quality"], "estimated")
        self.assertIn("missing_market_close", row["quality_issues"])
        self.assertEqual(row["missing_price_symbols"], ["A"])
        self.assertIsNone(row["return_pct"])
        self.assertIsNone(row["current_equity"])

    def test_missing_one_benchmark_contribution_blocks_comparison(self):
        trades = [
            {"ts": "2026-01-01", "strategy_id": "alpha", "symbol": "A", "action": "buy", "qty": 1, "price": 100},
            {"ts": "2026-01-03", "strategy_id": "alpha", "symbol": "B", "action": "buy", "qty": 1, "price": 100},
        ]
        prices = {"A": [{"date": "2026-01-03", "close": 100}], "B": [{"date": "2026-01-03", "close": 100}]}
        benchmarks = {
            "KOSPI": [{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 110}],
            "KOSDAQ": [{"date": "2026-01-01", "close": 100}, {"date": "2026-01-03", "close": 100}],
        }
        row = build_strategy_forward_performance(trades, prices, benchmarks, as_of="2026-01-03")[0]
        self.assertIsNone(row["kospi_return_pct"])
        self.assertIn("incomplete_kospi_contributions", row["quality_issues"])

    def test_price_rows_do_not_need_to_be_sorted(self):
        trades = [{"ts": "2026-01-01", "strategy_id": "alpha", "symbol": "A", "action": "buy", "qty": 1, "price": 100}]
        prices = {"A": [{"date": "2026-01-03", "close": 130}, {"date": "2026-01-02", "close": 110}]}
        benchmarks = {
            "KOSPI": [{"date": "2026-01-03", "close": 100}, {"date": "2026-01-01", "close": 100}],
            "KOSDAQ": [{"date": "2026-01-01", "close": 100}, {"date": "2026-01-03", "close": 100}],
        }
        row = build_strategy_forward_performance(trades, prices, benchmarks, as_of="2026-01-02")[0]
        self.assertEqual(row["market_value"], 110)

    def test_sell_beyond_strategy_owned_quantity_is_flagged(self):
        trades = [
            {"ts": "2026-01-02", "strategy_id": "alpha", "symbol": "A", "action": "buy", "qty": 1, "price": 100},
            {"ts": "2026-01-03", "strategy_id": "alpha", "symbol": "A", "action": "sell", "qty": 2, "price": 110},
        ]
        benchmarks = {
            "KOSPI": [{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 100}],
            "KOSDAQ": [{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 100}],
        }
        row = build_strategy_forward_performance(trades, {}, benchmarks, as_of="2026-01-03")[0]
        self.assertIn("strategy_ownership_mismatch", row["quality_issues"])

    def test_daily_nav_is_flow_neutral_and_mdd_uses_twr_index(self):
        trades = [
            {"ts": "2026-01-02 10:00:00", "strategy_id": "alpha", "symbol": "A", "action": "buy", "qty": 1, "price": 100},
            {"ts": "2026-01-03 10:00:00", "strategy_id": "alpha", "symbol": "B", "action": "buy", "qty": 1, "price": 100},
        ]
        prices = {
            "A": [{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 100}],
            "B": [{"date": "2026-01-03", "close": 100}],
        }
        benchmarks = {
            "KOSPI": [{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 100}],
            "KOSDAQ": [{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 100}],
        }
        row = build_strategy_forward_performance(trades, prices, benchmarks, as_of="2026-01-03")[0]
        self.assertEqual(row["returns"]["twr_pct"], 0.0)
        self.assertEqual(row["nav"]["max_drawdown_pct"], 0.0)
        self.assertEqual(row["nav"]["observations"], 2)
        self.assertEqual(row["daily_nav"][1]["external_flow"], 100)

    def test_benchmark_twr_uses_each_finalized_session_return(self):
        trades = [{"ts": "2026-01-02", "strategy_id": "alpha", "symbol": "A", "action": "buy", "qty": 1, "price": 100}]
        prices = {"A": [{"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 110}]}
        benchmarks = {
            "KOSPI": [{"date": "2026-01-01", "close": 100}, {"date": "2026-01-02", "close": 110}, {"date": "2026-01-03", "close": 121}],
            "KOSDAQ": [{"date": "2026-01-01", "close": 100}, {"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 100}],
        }
        row = build_strategy_forward_performance(trades, prices, benchmarks, as_of="2026-01-03")[0]
        self.assertEqual(row["returns"]["twr_pct"], 10.0)
        self.assertEqual(row["returns"]["kospi_twr_pct"], 21.0)
        self.assertEqual(row["returns"]["excess_twr_vs_kospi_pct"], -11.0)

    def test_missing_daily_close_blocks_nav_chain(self):
        trades = [{"ts": "2026-01-02", "strategy_id": "alpha", "symbol": "A", "action": "buy", "qty": 1, "price": 100}]
        benchmarks = {
            "KOSPI": [{"date": "2026-01-02", "close": 100}],
            "KOSDAQ": [{"date": "2026-01-02", "close": 100}],
        }
        row = build_strategy_forward_performance(trades, {}, benchmarks, as_of="2026-01-02")[0]
        self.assertFalse(row["nav"]["available"])
        self.assertIsNone(row["returns"]["twr_pct"])
        self.assertEqual(row["quality"]["status"], "blocked")

    def test_trade_after_last_index_session_blocks_nav(self):
        trades = [{"ts": "2026-01-03", "strategy_id": "alpha", "symbol": "A", "action": "buy", "qty": 1, "price": 100}]
        prices = {"A": [{"date": "2026-01-03", "close": 100}]}
        benchmarks = {
            "KOSPI": [{"date": "2026-01-02", "close": 100}],
            "KOSDAQ": [{"date": "2026-01-02", "close": 100}],
        }
        row = build_strategy_forward_performance(trades, prices, benchmarks, as_of="2026-01-03")[0]
        self.assertFalse(row["nav"]["available"])
        self.assertIn("nav_unavailable", row["quality"]["blocking_issues"])

    def test_historical_missing_close_propagates_to_top_quality(self):
        trades = [{"ts": "2026-01-02", "strategy_id": "alpha", "symbol": "A", "action": "buy", "qty": 1, "price": 100}]
        prices = {"A": [{"date": "2026-01-03", "close": 100}]}
        benchmarks = {
            "KOSPI": [{"date": "2026-01-01", "close": 100}, {"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 100}],
            "KOSDAQ": [{"date": "2026-01-01", "close": 100}, {"date": "2026-01-02", "close": 100}, {"date": "2026-01-03", "close": 100}],
        }
        row = build_strategy_forward_performance(trades, prices, benchmarks, as_of="2026-01-03")[0]
        self.assertEqual(row["quality"]["status"], "blocked")
        self.assertIn("missing_market_close", row["quality"]["blocking_issues"])


class PerformanceReviewRepositoryTests(unittest.TestCase):
    def test_manual_review_is_persisted_without_strategy_state_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "performance.sqlite")
            with patch.object(config, "trade_db_path", db_path):
                saved = save_strategy_performance_review("alpha", "reduce", "비중 축소 관찰")
                loaded = get_strategy_performance_review("alpha")
        self.assertEqual(saved["decision"], "reduce")
        self.assertEqual(loaded["note"], "비중 축소 관찰")

    def test_manual_reviews_are_scoped_by_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "performance.sqlite")
            with patch.object(config, "trade_db_path", db_path), patch.object(config, "trading_env", "demo"):
                save_strategy_performance_review("alpha", "monitor", "demo")
            with patch.object(config, "trade_db_path", db_path), patch.object(config, "trading_env", "real"):
                self.assertIsNone(get_strategy_performance_review("alpha"))
                save_strategy_performance_review("alpha", "reduce", "real")
            with patch.object(config, "trade_db_path", db_path), patch.object(config, "trading_env", "demo"):
                self.assertEqual(get_strategy_performance_review("alpha")["note"], "demo")

    def test_invalid_manual_review_decision_is_rejected(self):
        with self.assertRaises(ValueError):
            save_strategy_performance_review("alpha", "auto_trade", "")

    def test_daily_nav_upsert_is_idempotent_and_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "performance.sqlite")
            nav = [{
                "session_date": "2026-01-02", "cash": 0, "market_value": 100,
                "nav": 100, "external_flow": 100, "buy_amount": 100,
                "sell_amount": 0, "daily_return_pct": 0, "twr_index": 100,
                "drawdown_pct": 0, "mdd_pct": 0, "quality_issues": [],
                "calc_version": "daily-nav-v1",
            }]
            with patch.object(config, "trade_db_path", db_path):
                replace_daily_nav("alpha", nav, input_hash="one")
                replace_daily_nav("alpha", nav, input_hash="one")
                rows = list_daily_nav("alpha")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_hash"], "one")

    def test_account_snapshot_and_confirmed_manual_cashflow_are_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "performance.sqlite")
            with patch.object(config, "trade_db_path", db_path):
                snapshot = record_account_equity_snapshot(
                    total_equity=1000, cash=400, stock_value=600,
                    captured_at="2026-01-02T16:00:00+09:00", raw_summary_hash="hash1",
                )
                record_account_cashflow(
                    external_ref="deposit-1", occurred_at="2026-01-02T09:00:00+09:00",
                    amount=500, kind="deposit", confirmed=True,
                )
                rows = list_account_cashflows()
        self.assertEqual(snapshot["total_equity"], 1000)
        self.assertEqual(rows[0]["amount"], 500)
        self.assertEqual(rows[0]["confirmed"], 1)

    def test_cashflow_rejects_non_finite_or_timezone_less_values(self):
        with self.assertRaises(ValueError):
            record_account_cashflow(
                external_ref="bad", occurred_at="2026-01-02T09:00:00",
                amount=float("nan"), kind="deposit",
            )

    def test_broker_account_twr_removes_confirmed_cashflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "performance.sqlite")
            with patch.object(config, "trade_db_path", db_path):
                record_account_equity_snapshot(
                    total_equity=1000, cash=400, stock_value=600,
                    captured_at="2026-01-02T16:00:00+09:00", raw_summary_hash="day1",
                )
                record_account_equity_snapshot(
                    total_equity=1200, cash=500, stock_value=700,
                    captured_at="2026-01-03T16:00:00+09:00", raw_summary_hash="day2",
                )
                record_account_cashflow(
                    external_ref="deposit-1", occurred_at="2026-01-03T09:00:00+09:00",
                    amount=100, kind="deposit", confirmed=True,
                )
                result = build_account_equity_performance()
        self.assertTrue(result["available"])
        self.assertEqual(result["twr_pct"], 9.09)


class ForwardPerformanceRouteTests(unittest.TestCase):
    def test_forward_performance_route_is_no_store_and_filterable(self):
        response = Response()
        with patch.object(stock, "_load_merged_trades", return_value=[{"strategy_id": "alpha"}]), patch.object(
            stock, "_build_forward_strategy_performance", return_value=[{"strategy_id": "alpha"}]
        ) as build, patch.object(stock, "_build_forward_account_performance", return_value=None):
            result = stock.get_forward_performance(response, strategy_id="alpha")
        self.assertEqual(result["strategies"], [{"strategy_id": "alpha"}])
        self.assertTrue(result["manual_review_only"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        build.assert_called_once_with([{"strategy_id": "alpha"}], strategy_id="alpha")

    def test_review_route_never_changes_trading_state(self):
        payload = stock.StrategyPerformanceReviewPayload(decision="pause", note="관찰")
        with patch(
            "src.db.performance_repository.save_strategy_performance_review",
            return_value={"strategy_id": "alpha", "decision": "pause"},
        ), patch("src.db.strategy_repository.load_ai_strategies", return_value=[{"id": "alpha"}]):
            result = stock.update_forward_performance_review("alpha", payload)
        self.assertFalse(result["trading_state_changed"])

    def test_review_route_rejects_invalid_decision(self):
        payload = stock.StrategyPerformanceReviewPayload(decision="invalid")
        with patch("src.db.strategy_repository.load_ai_strategies", return_value=[{"id": "alpha"}]):
            with self.assertRaises(HTTPException) as raised:
                stock.update_forward_performance_review("alpha", payload)
        self.assertEqual(raised.exception.status_code, 400)

    def test_cashflow_route_records_manual_data_without_recalculation(self):
        payload = stock.AccountCashflowPayload(
            external_ref="deposit-1", occurred_at="2026-01-02T09:00:00+09:00",
            amount=100, kind="deposit", confirmed=True,
        )
        with patch(
            "src.db.performance_repository.record_account_cashflow",
            return_value={"external_ref": "deposit-1"},
        ):
            result = stock.save_performance_account_cashflow(payload)
        self.assertFalse(result["performance_recalculated"])


class ForwardPerformanceTradeMergeTests(unittest.TestCase):
    def test_same_second_same_symbol_different_strategies_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "trades.sqlite")
            with patch.object(config, "trade_db_path", db_path):
                repository.init_db()
                with repository.connect_db() as conn:
                    for strategy_id in ("alpha", "beta"):
                        conn.execute(
                            """
                            INSERT INTO trades
                                (ts, symbol, name, action, qty, price, reason, ok, env, dry_run, strategy_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            ("2026-01-02 10:00:00", "005930", "Samsung", "buy", 1, 100,
                             "test", 1, "demo", 1, strategy_id),
                        )
                with patch.object(dashboard_core, "fetch_cloud_trades", return_value=[]):
                    rows = dashboard_core._load_merged_trades()
        self.assertEqual({row["strategy_id"] for row in rows}, {"alpha", "beta"})

    def test_broker_order_number_reuse_on_different_dates_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "trades.sqlite")
            with patch.object(config, "trade_db_path", db_path):
                repository.init_db()
                with repository.connect_db() as conn:
                    for day in ("02", "03"):
                        conn.execute(
                            """
                            INSERT INTO trades
                                (ts, symbol, name, action, qty, price, reason, ok, env, dry_run,
                                 strategy_id, broker_order_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (f"2026-01-{day} 10:00:00", "005930", "Samsung", "buy", 1, 100,
                             "test", 1, "demo", 1, "alpha", "000001"),
                        )
                with patch.object(dashboard_core, "fetch_cloud_trades", return_value=[]):
                    rows = dashboard_core._load_merged_trades()
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
