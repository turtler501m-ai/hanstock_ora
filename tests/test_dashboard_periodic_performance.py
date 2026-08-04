import unittest
from datetime import datetime
from unittest.mock import patch

from src.dashboard import (
    _account_trades,
    _build_periodic_performance,
    _period_bucket,
    trader,
)
from src.dashboard.core import _INDEX_SYMBOL_ALIASES, _safe_index_rows


class DashboardPeriodicPerformanceTests(unittest.TestCase):
    def test_market_indices_never_fall_back_to_etf_prices(self):
        self.assertNotIn("069500", _INDEX_SYMBOL_ALIASES["KOSPI"])
        self.assertNotIn("229200", _INDEX_SYMBOL_ALIASES["KOSDAQ"])
        self.assertEqual(_INDEX_SYMBOL_ALIASES["KOSPI"][0], "^KS11")
        self.assertEqual(_INDEX_SYMBOL_ALIASES["KOSDAQ"][0], "^KQ11")

    def test_abnormal_benchmark_move_is_rejected(self):
        rows = _safe_index_rows([
            {"date": "2026-07-30", "close": 5593.56},
            {"date": "2026-07-31", "close": 6595.45},
        ])

        self.assertEqual(rows, [{"date": "2026-07-30", "close": 5593.56}])

    def setUp(self) -> None:
        self.original_dry_run = trader.DRY_RUN
        self.original_trading_env = trader.TRADING_ENV

    def tearDown(self) -> None:
        trader.DRY_RUN = self.original_dry_run
        trader.TRADING_ENV = self.original_trading_env

    def test_period_bucket_has_new_keys(self):
        bucket = _period_bucket()
        self.assertIn("cost_of_sold", bucket)
        self.assertIn("realized_pnl_rate", bucket)
        self.assertIn("details", bucket)
        self.assertEqual(bucket["cost_of_sold"], 0)
        self.assertEqual(bucket["realized_pnl_rate"], 0.0)
        self.assertEqual(bucket["details"], [])

    def test_account_trades_filters_dry_run_correctly(self):
        trades = [
            {"ok": 1, "dry_run": 1, "reason": "buy strategy", "symbol": "005930", "action": "buy", "qty": 10, "price": 70000, "ts": "2026-05-27 10:00:00"},
            {"ok": 1, "dry_run": 0, "reason": "sell strategy", "symbol": "005930", "action": "sell", "qty": 10, "price": 75000, "ts": "2026-05-27 11:00:00"},
        ]

        # Case 1: DRY_RUN=false, TRADING_ENV=real -> Bypasses dry_run=1
        trader.DRY_RUN = False
        trader.TRADING_ENV = "real"
        real_trades = _account_trades(trades)
        self.assertEqual(len(real_trades), 1)
        self.assertEqual(real_trades[0]["dry_run"], 0)

        # Case 2: DRY_RUN=true -> Includes dry_run=1
        trader.DRY_RUN = True
        demo_trades = _account_trades(trades)
        self.assertEqual(len(demo_trades), 2)

        # Case 3: TRADING_ENV=demo -> Includes dry_run=1 even if DRY_RUN=false
        trader.DRY_RUN = False
        trader.TRADING_ENV = "demo"
        demo_trades_2 = _account_trades(trades)
        self.assertEqual(len(demo_trades_2), 2)

    def test_build_periodic_performance_computes_correct_realized_rates(self):
        trader.DRY_RUN = True
        trades = [
            # Buy 10 shares of Samsung Electronics at 70,000 KRW (total cost = 700,000)
            {"ok": 1, "dry_run": 1, "reason": "buy", "symbol": "005930", "action": "buy", "qty": 10, "price": 70000, "ts": "2026-05-27 10:00:00"},
            # Sell 5 shares of Samsung Electronics at 77,000 KRW (selling price = 385,000, cost of sold = 350,000, pnl = 35,000, return = 10%)
            {"ok": 1, "dry_run": 1, "reason": "sell", "symbol": "005930", "action": "sell", "qty": 5, "price": 77000, "ts": "2026-05-27 11:00:00"},
        ]

        with patch("src.dashboard.core._load_index_rows", return_value={}):
            perf = _build_periodic_performance(trades)
        daily = perf["daily"]
        
        self.assertEqual(len(daily), 1)
        day_bucket = daily[0]
        self.assertEqual(day_bucket["period"], "2026-05-27")
        self.assertEqual(day_bucket["buy_amount"], 700000)
        self.assertEqual(day_bucket["sell_amount"], 385000)
        self.assertEqual(day_bucket["realized_pnl"], 35000)
        self.assertEqual(day_bucket["cost_of_sold"], 350000)
        self.assertEqual(day_bucket["realized_pnl_rate"], 10.0)
        self.assertEqual(day_bucket["net_cashflow"], -315000)
        self.assertEqual(len(day_bucket["details"]), 2)
        sell_detail = day_bucket["details"][1]
        self.assertEqual(sell_detail["symbol"], "005930")
        self.assertEqual(sell_detail["action"], "sell")
        self.assertEqual(sell_detail["amount"], 385000)
        self.assertEqual(sell_detail["realized_pnl"], 35000)
        self.assertEqual(sell_detail["realized_pnl_rate"], 10.0)

    def test_build_periodic_performance_ignores_implausible_partial_fill_price(self):
        trader.DRY_RUN = False
        trader.TRADING_ENV = "real"
        trades = [
            {
                "ok": 1,
                "dry_run": 0,
                "symbol": "026940",
                "action": "buy",
                "qty": 1159,
                "price": 2750,
                "filled_qty": 336,
                "filled_price": 223507,
                "order_status": "partial",
                "ts": "2026-06-25 12:34:46",
            },
            {
                "ok": 1,
                "dry_run": 0,
                "symbol": "026940",
                "action": "buy",
                "qty": 3274,
                "price": 2705,
                "filled_qty": 3274,
                "filled_price": 2705,
                "order_status": "filled",
                "ts": "2026-06-25 13:03:06",
            },
            {
                "ok": 1,
                "dry_run": 0,
                "symbol": "026940",
                "action": "sell",
                "qty": 395,
                "price": 2645,
                "filled_qty": 395,
                "filled_price": 2645,
                "order_status": "filled",
                "ts": "2026-06-25 15:02:53",
            },
        ]

        with patch("src.dashboard.core._load_index_rows", return_value={}):
            perf = _build_periodic_performance(trades)

        self.assertEqual(perf["daily"][0]["realized_pnl"], -23700)

    def test_periodic_performance_adds_strategy_validation_and_attribution(self):
        trader.DRY_RUN = True
        trades = []
        for day in range(1, 7):
            strategy_id = "alpha"
            trades.extend([
                {
                    "ok": 1, "dry_run": 1, "strategy_id": strategy_id,
                    "symbol": f"00000{day}", "name": f"종목{day}", "action": "buy",
                    "qty": 1, "price": 100, "ts": f"2026-05-{day:02d} 10:00:00",
                },
                {
                    "ok": 1, "dry_run": 1, "strategy_id": strategy_id,
                    "symbol": f"00000{day}", "name": f"종목{day}", "action": "sell",
                    "qty": 1, "price": 110, "ts": f"2026-05-{day:02d} 11:00:00",
                },
            ])

        with patch("src.dashboard.core._load_index_rows", return_value={}):
            perf = _build_periodic_performance(trades)

        detail = perf["daily"][0]["details"][0]
        self.assertEqual(detail["strategy_id"], "alpha")
        self.assertEqual(detail["strategy_name"], "alpha")
        validation = perf["strategy_validation"][0]
        self.assertEqual(validation["closed_count"], 6)
        self.assertEqual(validation["win_rate"], 100.0)
        self.assertEqual(validation["validation_status"], "effective")

    def test_periodic_performance_adds_daily_and_monthly_index_changes(self):
        trader.DRY_RUN = True
        trades = [{
            "ok": 1, "dry_run": 1, "symbol": "005930", "action": "buy",
            "qty": 1, "price": 70000, "ts": "2026-05-03 10:00:00",
        }]
        indices = {
            "KOSPI": [
                {"date": "2026-05-01", "close": 2500},
                {"date": "2026-05-02", "close": 2525},
                {"date": "2026-05-03", "close": 2500},
            ],
            "KOSDAQ": [
                {"date": "2026-05-01", "close": 800},
                {"date": "2026-05-02", "close": 808},
                {"date": "2026-05-03", "close": 816},
            ],
        }

        with patch("src.dashboard.core._load_index_rows", return_value=indices):
            performance = _build_periodic_performance(trades)
            row = performance["daily"][0]

        self.assertEqual(row["kospi"], 2500.0)
        self.assertEqual(row["kosdaq"], 816.0)
        self.assertEqual(row["kospi_change_pct"], -0.99)
        self.assertEqual(row["kosdaq_change_pct"], 0.99)
        self.assertNotIn("kospi_volatility", row)
        self.assertNotIn("kosdaq_volatility", row)

        monthly_row = performance["monthly"][0]
        self.assertEqual(monthly_row["kospi"], 2500.0)
        self.assertEqual(monthly_row["kosdaq"], 816.0)


if __name__ == "__main__":
    unittest.main()
