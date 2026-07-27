import unittest
from unittest.mock import Mock, patch

from src.trader import (
    KOSPI_UNIVERSE,
    WATCHLIST,
    KIStockAPI,
    build_orders,
    build_scan_universe,
    calc_bollinger,
    calc_macd,
    calc_rsi,
    calc_sma,
    calc_strategy_profile,
    find_candidates,
    generate_ai_weight_plan,
    generate_portfolio_optimizer_plan,
    generate_signal,
)


class TraderCoreTests(unittest.TestCase):
    def test_indicators_handle_short_price_history(self):
        self.assertEqual(calc_rsi([1, 2, 3]), 50.0)
        self.assertEqual(calc_sma([1, 2, 3], 5), 3)
        self.assertEqual(calc_bollinger([1, 2, 3], 20), (3, 3, 3))

    def test_build_orders_respects_cash_budget(self):
        orders = build_orders(
            [{"ticker": "005930", "score": 2, "reasons": ["test"]}],
            lambda _symbol: {"ask1": 70000, "current": 70000},
            held_count=0,
            cash=1_000_000,
        )
        self.assertEqual(len(orders), 1)
        self.assertLessEqual(orders[0]["estimated_cost"], 1_000_000)

    def test_build_orders_excludes_configured_symbols(self):
        with patch("src.strategy.seven_split.config.hanstock_excluded_symbols", "252670"):
            orders = build_orders(
                [
                    {"ticker": "252670", "score": 5, "reasons": ["blocked"]},
                    {"ticker": "005930", "score": 2, "reasons": ["ok"]},
                ],
                lambda _symbol: {"ask1": 70000, "current": 70000},
                held_count=0,
                cash=1_000_000,
            )

        self.assertEqual([order["ticker"] for order in orders], ["005930"])

    def test_build_scan_universe_prefers_configured_condition_search(self):
        api = Mock()
        api.get_condition_search_result.return_value = ["005930", "000660", "005930"]

        with patch("src.strategy.seven_split.config.kis_condition_search_enabled", True), \
                patch("src.strategy.seven_split.config.kis_condition_user_id", "hts-user"), \
                patch("src.strategy.seven_split.config.kis_condition_seq", "001"), \
                patch("src.strategy.seven_split.config.kis_condition_name", "breakout"):
            universe = build_scan_universe(api, {"000660"})

        api.get_condition_search_result.assert_called_once_with("hts-user", "001", "breakout")
        api.get_volume_rank.assert_not_called()
        self.assertIn("005930", universe)
        self.assertNotIn("000660", universe)

    def test_build_scan_universe_prefers_api_condition_settings(self):
        api = Mock()
        api.kis_condition_search_enabled = True
        api.kis_condition_user_id = "real-check-user"
        api.kis_condition_seq = "900"
        api.kis_condition_name = "real-check-breakout"
        api.get_condition_search_result.return_value = ["005930"]

        with patch("src.strategy.seven_split.config.kis_condition_search_enabled", False):
            universe = build_scan_universe(api, set())

        api.get_condition_search_result.assert_called_once_with(
            "real-check-user",
            "900",
            "real-check-breakout",
        )
        api.get_volume_rank.assert_not_called()
        self.assertIn("005930", universe)

    def test_generate_signal_stop_loss_sells_all(self):
        signal = generate_signal(
            {"prpr": "10000", "hldg_qty": "7", "evlu_pfls_rt": "-20"},
            [],
        )
        self.assertEqual(signal["action"], "sell")
        self.assertEqual(signal["qty"], 7)
        self.assertEqual(signal["price"], 0)

    def test_generate_signal_stop_loss_uses_configured_ten_percent_floor(self):
        with patch("src.strategy.seven_split.config.stop_loss_pct", -10.0):
            signal = generate_signal(
                {"prpr": "10000", "hldg_qty": "7", "evlu_pfls_rt": "-9.96"},
                [],
            )

        self.assertEqual(signal["action"], "sell")
        self.assertEqual(signal["qty"], 7)
        self.assertEqual(signal["reason"], "stop loss -10.0%")

    def test_strategy_profile_exposes_composite_indicators(self):
        prices = [float(i) for i in range(1, 140)]
        highs = [p + 1 for p in prices]
        volumes = [100.0] * 119 + [200.0] * 20
        profile = calc_strategy_profile(prices, highs, volumes)
        self.assertIn("macd_hist", profile)
        self.assertIn("rsi2", profile)
        self.assertGreaterEqual(profile["score"], 0)

    def test_macd_handles_short_history(self):
        macd = calc_macd([1, 2, 3])
        self.assertFalse(macd["bull_cross"])
        self.assertEqual(macd["hist"], 0.0)

    def test_ai_weight_plan_returns_rebalance_rows(self):
        prices = [float(i) for i in range(100, 220)]
        plan = generate_ai_weight_plan(
            [{
                "symbol": "005930",
                "name": "Samsung",
                "qty": 1,
                "price": 200000,
                "value": 200000,
                "prices": prices,
                "highs": [p + 1 for p in prices],
                "volumes": [100.0] * len(prices),
            }],
            total_eval=1_000_000,
        )
        self.assertEqual(len(plan["positions"]), 1)
        self.assertIn("target_weight", plan["positions"][0])

    def test_ai_weight_plan_fallback_is_deterministic(self):
        prices = [float(i) for i in range(100, 220)]
        holdings = [
            {
                "symbol": "005930",
                "name": "Samsung",
                "qty": 1,
                "price": 200000,
                "value": 200000,
                "prices": prices,
                "highs": [p + 1 for p in prices],
                "volumes": [100.0] * len(prices),
            },
            {
                "symbol": "000660",
                "name": "SK Hynix",
                "qty": 1,
                "price": 100000,
                "value": 100000,
                "prices": prices,
                "highs": [p + 1 for p in prices],
                "volumes": [100.0] * len(prices),
            },
        ]

        first = generate_ai_weight_plan(holdings, total_eval=1_000_000)
        second = generate_ai_weight_plan(holdings, total_eval=1_000_000)

        self.assertEqual(
            [position["target_weight"] for position in first["positions"]],
            [position["target_weight"] for position in second["positions"]],
        )

    def test_portfolio_optimizer_plan_returns_method(self):
        prices = [float(i) for i in range(100, 220)]
        plan = generate_portfolio_optimizer_plan(
            [{
                "symbol": "005930",
                "name": "Samsung",
                "qty": 1,
                "price": 200000,
                "value": 200000,
                "prices": prices,
                "highs": [p + 1 for p in prices],
                "volumes": [100.0] * len(prices),
            }],
            total_eval=1_000_000,
        )
        self.assertEqual(plan["method"], "score_tilted_inverse_vol")
        self.assertEqual(len(plan["positions"]), 1)

    def test_ai_rebalance_rows_include_only_executable_positions(self):
        from unittest.mock import patch
        from src import trader

        class _FakeAPI:
            def get_daily(self, _symbol, n=120):
                return [
                    {"stck_clpr": "100", "stck_hgpr": "101", "acml_vol": "1000"},
                    {"stck_clpr": "110", "stck_hgpr": "111", "acml_vol": "1100"},
                ]

        balance = {
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "Samsung",
                    "hldg_qty": "10",
                    "prpr": "70000",
                    "evlu_amt": "700000",
                }
            ]
        }
        ai_plan = {
            "ai_active": False,
            "positions": [
                {
                    "symbol": "005930",
                    "name": "Samsung",
                    "price": 70000,
                    "rebalance_action": "sell",
                    "rebalance_qty": 2,
                    "current_weight": 0.7,
                    "target_weight": 0.5,
                    "target_value": 500000,
                    "delta_value": -200000,
                    "score": 3,
                    "reasons": ["risk trim"],
                },
                {"symbol": "000660", "rebalance_action": "hold", "rebalance_qty": 0},
            ],
        }

        with patch.object(trader, "generate_ai_weight_plan", return_value=ai_plan):
            rows = trader.build_ai_rebalance_rows(_FakeAPI(), balance, 1_000_000)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "ai_rebalance")
        self.assertEqual(rows[0]["action"], "sell")
        self.assertEqual(rows[0]["qty"], 2)

    def test_ai_rebalance_rows_exclude_configured_symbols(self):
        from src import trader

        class _FakeAPI:
            def get_daily(self, _symbol, n=120):
                return []

        balance = {
            "output1": [{
                "pdno": "252670",
                "hldg_qty": "10",
                "prpr": "100",
                "evlu_amt": "1000",
            }]
        }
        ai_plan = {
            "ai_active": False,
            "positions": [{
                "symbol": "252670",
                "name": "Blocked",
                "price": 100,
                "rebalance_action": "sell",
                "rebalance_qty": 1,
            }],
        }

        with patch.object(trader, "generate_ai_weight_plan", return_value=ai_plan), \
                patch("src.strategy.seven_split.config.hanstock_excluded_symbols", "252670"):
            rows = trader.build_ai_rebalance_rows(_FakeAPI(), balance, 1_000_000)

        self.assertEqual(rows, [])

    def test_runtime_plan_can_include_ai_rebalance_rows(self):
        from unittest.mock import patch
        from src import trader

        class _FakeAPI:
            def get_daily(self, _symbol, n=60):
                return []

            def get_volume_rank(self, top_n=50):
                return []

            def get_quote(self, _symbol):
                return {"current": 0, "ask1": 0, "bid1": 0}

        balance = {
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "Samsung",
                    "hldg_qty": "10",
                    "prpr": "70000",
                    "evlu_amt": "700000",
                }
            ],
            "output2": [{"dnca_tot_amt": "100000", "tot_evlu_amt": "1000000", "evlu_pfls_smtl_amt": "0"}],
        }
        api = _FakeAPI()
        with patch.object(
            trader,
            "generate_signal",
            return_value={"action": "hold", "qty": 0, "price": 0, "reason": "", "indicators": {}},
        ), patch.object(
            trader,
            "find_candidates",
            return_value={"candidates": [], "scan_summary": [], "scanned": 0, "min_score": 2, "scan_error": None},
        ), patch.object(
            trader,
            "build_ai_rebalance_rows",
            return_value=[{"symbol": "005930", "action": "sell", "qty": 1, "category": "ai_rebalance"}],
        ) as ai_rows:
            plan = trader.build_runtime_plan(api, balance, include_ai_rebalance=True)

        ai_rows.assert_called_once_with(api, balance, 1_000_000)
        self.assertEqual(plan["ai_rebalance_rows"][0]["category"], "ai_rebalance")

    def test_runtime_plan_uses_configured_capital_instead_of_full_account(self):
        from src import trader

        class _FakeAPI:
            def get_daily(self, _symbol, n=60):
                return []

            def get_volume_rank(self, top_n=50):
                return []

            def get_quote(self, _symbol):
                return {"current": 0, "ask1": 0, "bid1": 0}

        balance = {
            "output1": [{
                "pdno": "005930",
                "hldg_qty": "100",
                "prpr": "500000",
                "evlu_amt": "50000000",
            }],
            "output2": [{
                "dnca_tot_amt": "450000000",
                "scts_evlu_amt": "50000000",
                "tot_evlu_amt": "500000000",
                "evlu_pfls_smtl_amt": "0",
            }],
        }

        api = _FakeAPI()
        with patch.object(trader, "TOTAL_CAPITAL", 100_000_000), \
                patch.object(trader, "CASH_BUFFER", 0.20), \
                patch.object(trader, "generate_signal", return_value={
                    "action": "hold", "qty": 0, "price": 0, "reason": "", "indicators": {},
                }), \
                patch.object(trader, "find_candidates", return_value={
                    "candidates": [], "scan_summary": [], "scanned": 0,
                    "min_score": 2, "scan_error": None,
                }), \
                patch.object(trader, "build_ai_rebalance_rows", return_value=[]) as ai_rows:
            plan = trader.build_runtime_plan(
                api,
                balance,
                include_ai_rebalance=True,
            )

        ai_rows.assert_called_once_with(api, balance, 100_000_000)
        self.assertEqual(plan["operating_capital"], 100_000_000)
        self.assertEqual(plan["buying_cash"], 30_000_000)
        self.assertEqual(plan["remaining_cash"], 30_000_000)

    def test_kospi_universe_has_no_duplicates(self):
        self.assertEqual(len(KOSPI_UNIVERSE), len(set(KOSPI_UNIVERSE)))

    def test_build_scan_universe_always_includes_watchlist(self):
        """거래량 API가 빈 결과를 돌려줘도 WATCHLIST는 항상 포함된다."""
        class _FakeAPI:
            def get_volume_rank(self, top_n=50):
                return []  # API 실패 시뮬레이션

        universe = build_scan_universe(_FakeAPI(), held_symbols=set())
        for code in WATCHLIST:
            self.assertIn(code, universe)

    def test_build_scan_universe_excludes_held(self):
        held = {"005930", "000660"}

        class _FakeAPI:
            def get_volume_rank(self, top_n=50):
                return []

        universe = build_scan_universe(_FakeAPI(), held_symbols=held)
        for code in held:
            self.assertNotIn(code, universe)

    def test_build_scan_universe_excludes_configured_symbols(self):
        class _FakeAPI:
            def get_volume_rank(self, top_n=50):
                return ["252670", "005930"]

        with patch("src.strategy.seven_split.config.hanstock_excluded_symbols", "252670,252710"):
            universe = build_scan_universe(_FakeAPI(), held_symbols=set())

        self.assertNotIn("252670", universe)
        self.assertNotIn("252710", universe)
        self.assertIn("005930", universe)

    def test_isolated_strategy_without_universe_does_not_use_shared_scan_universe(self):
        from src import trader

        class _FakeAPI:
            def get_quote(self, _symbol):
                return {"current": 0, "ask1": 0, "bid1": 0}

        balance = {
            "output1": [],
            "output2": [{"dnca_tot_amt": "100000", "tot_evlu_amt": "100000", "evlu_pfls_smtl_amt": "0"}],
        }

        with patch("src.db.repository.load_strategy_universe_symbols", return_value=[]), \
                patch.object(trader, "build_scan_universe") as shared_universe, \
                patch.object(trader, "find_candidates") as find_candidates_mock:
            plan = trader.build_runtime_plan(
                _FakeAPI(),
                balance,
                force_strategy_id="plunge_bounce_strategy",
            )

        shared_universe.assert_not_called()
        find_candidates_mock.assert_not_called()
        self.assertEqual(plan["candidate_plan_rows"], [])
        self.assertEqual(plan["candidate_scan"]["scanned"], 0)
        self.assertIn("dedicated universe", plan["candidate_scan"]["scan_error"])

    def test_isolated_strategy_does_not_build_whole_account_position_rows(self):
        from src import trader

        class _FakeAPI:
            def get_daily(self, _symbol, n=60):
                return []

            def get_quote(self, _symbol):
                return {"current": 0, "ask1": 0, "bid1": 0}

        balance = {
            "output1": [{
                "pdno": "078930",
                "prdt_name": "GS",
                "hldg_qty": "6369",
                "prpr": "71300",
                "evlu_amt": "454109700",
                "evlu_pfls_rt": "2.0",
            }],
            "output2": [{
                "dnca_tot_amt": "10000000",
                "scts_evlu_amt": "454109700",
                "tot_evlu_amt": "464109700",
                "evlu_pfls_smtl_amt": "0",
            }],
        }

        with patch("src.db.repository.load_strategy_universe_symbols", return_value=[]), \
                patch.object(trader, "generate_signal") as signal_mock:
            plan = trader.build_runtime_plan(
                _FakeAPI(),
                balance,
                force_strategy_id="plunge_bounce_strategy",
            )

        signal_mock.assert_not_called()
        self.assertEqual(plan["position_plan_rows"], [])
        self.assertTrue(all(row.get("category") != "position" for row in plan["plan"]))

    def test_build_scan_universe_uses_volume_rank_when_available(self):
        extra = ["000020", "000030", "000040"]

        class _FakeAPI:
            def get_volume_rank(self, top_n=50):
                return extra

        universe = build_scan_universe(_FakeAPI(), held_symbols=set())
        for code in extra:
            self.assertIn(code, universe)

    def test_find_candidates_returns_dict_structure(self):
        """find_candidates는 candidates, scan_summary, scanned, min_score 키를 가진 dict를 반환한다."""
        result = find_candidates(held_symbols=set(), universe=[], min_score=2)
        self.assertIsInstance(result, dict)
        self.assertIn("candidates", result)
        self.assertIn("scan_summary", result)
        self.assertIn("scanned", result)
        self.assertIn("min_score", result)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["scanned"], 0)
        self.assertEqual(result["min_score"], 2)

    def test_circuit_breaker_can_be_reset(self):
        KIStockAPI.reset_circuit()
        api = KIStockAPI.__new__(KIStockAPI)
        api.notify_errors = False
        for _ in range(KIStockAPI.MAX_ERRORS):
            api._fail()

        status = KIStockAPI.circuit_status()
        self.assertTrue(status["opened"])
        self.assertEqual(status["error_count"], KIStockAPI.MAX_ERRORS)

        KIStockAPI.reset_circuit()
        status = KIStockAPI.circuit_status()
        self.assertFalse(status["opened"])
        self.assertEqual(status["error_count"], 0)

    def test_circuit_breaker_records_api_result(self):
        KIStockAPI.reset_circuit()
        api = KIStockAPI.__new__(KIStockAPI)

        api._record_result({"rt_cd": "1"})
        status = KIStockAPI.circuit_status()
        self.assertEqual(status["error_count"], 1)
        self.assertFalse(status["opened"])

        api._record_result({"rt_cd": "0"})
        status = KIStockAPI.circuit_status()
        self.assertEqual(status["error_count"], 0)
        self.assertFalse(status["opened"])


if __name__ == "__main__":
    unittest.main()
