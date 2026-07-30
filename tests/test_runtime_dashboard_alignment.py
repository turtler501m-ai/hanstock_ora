import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import src.dashboard as dashboard
from src import trader


class RuntimeDashboardAlignmentTests(unittest.TestCase):
    def _sample_plan(self):
        return [
            {
                "symbol": "005930",
                "name": "Samsung Electronics",
                "category": "position",
                "action": "sell",
                "qty": 1,
                "price": 71000,
                "reason": "take profit",
                "score": None,
                "reasons": [],
                "indicators": {"rsi": 72},
                "metadata": {"return_pct": 8.5},
            },
            {
                "symbol": "000660",
                "name": "SK Hynix",
                "category": "candidate",
                "action": "buy",
                "qty": 2,
                "price": 120000,
                "reason": "new buy score=4 (rsi, macd)",
                "score": 4,
                "reasons": ["rsi", "macd"],
                "indicators": {"rsi": 33, "sma20": 118000},
                "metadata": {"universe": "candidate_scan"},
            },
        ]

    def _assert_dashboard_compatible_plan(self, plan):
        required_keys = {
            "symbol",
            "name",
            "category",
            "action",
            "qty",
            "price",
            "reason",
            "score",
            "reasons",
            "indicators",
            "metadata",
        }
        self.assertEqual(len(plan), 2)
        self.assertEqual([row["category"] for row in plan], ["position", "candidate"])
        for row in plan:
            self.assertTrue(required_keys.issubset(row))
            self.assertIsInstance(row["indicators"], dict)
            self.assertIsInstance(row["metadata"], dict)

    def test_build_runtime_plan_emits_dashboard_compatible_plan_rows(self):
        api = Mock()
        api.get_daily.return_value = [{"stck_clpr": "70000"}]
        api.get_quote.return_value = {"current": 120000, "ask1": 120000, "bid1": 119500}
        balance_data = {
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "Samsung Electronics",
                    "evlu_pfls_rt": "8.5",
                }
            ],
            "output2": [
                {
                    "dnca_tot_amt": "500000",
                    "tot_evlu_amt": "1500000",
                    "evlu_pfls_smtl_amt": "10000",
                }
            ],
        }
        candidate_scan = {
            "candidates": [
                {
                    "ticker": "000660",
                    "name": "SK Hynix",
                    "score": 4,
                    "reasons": ["rsi", "macd"],
                    "rsi": 33,
                    "sma20": 118000,
                }
            ],
            "scan_summary": [{"ticker": "000660", "score": 4, "rsi": 33, "reasons": ["rsi", "macd"]}],
            "scanned": 1,
            "min_score": 2,
            "scan_error": None,
        }

        with ExitStack() as stack:
            stack.enter_context(patch("src.trader.daily_loss_halt_triggered", return_value=False))
            stack.enter_context(
                patch(
                    "src.trader.generate_signal",
                    return_value={
                        "action": "sell",
                        "qty": 1,
                        "price": 71000,
                        "reason": "take profit",
                        "indicators": {"rsi": 72},
                    },
                )
            )
            stack.enter_context(patch("src.trader.build_scan_universe", return_value=["000660"]))
            stack.enter_context(patch("src.trader.find_candidates", return_value=candidate_scan))
            stack.enter_context(
                patch(
                    "src.trader.build_orders",
                    return_value=[
                        {
                            "ticker": "000660",
                            "quantity": 2,
                            "limit_price": 120000,
                            "estimated_cost": 240000,
                            "score": 4,
                            "reasons": ["rsi", "macd"],
                        }
                    ],
                )
            )

            bundle = trader.build_runtime_plan(api, balance_data)

        self._assert_dashboard_compatible_plan(bundle["plan"])
        self.assertEqual(bundle["plan"][0]["metadata"], {"return_pct": 8.5})
        self.assertEqual(bundle["plan"][1]["score"], 4)
        self.assertEqual(bundle["plan"][1]["reasons"], ["rsi", "macd"])
        self.assertEqual(bundle["plan"][1]["indicators"], {"rsi": 33, "sma20": 118000})
        self.assertEqual(bundle["remaining_cash"], 260000)

    def test_build_dashboard_execution_plan_reuses_runtime_plan_rows_without_reshaping(self):
        runtime_plan = self._sample_plan()
        runtime_bundle = {
            "plan": runtime_plan,
            "remaining_cash": 260000,
            "daily_loss_halt": False,
            "candidate_scan": {"scanned": 17, "scan_error": None},
        }

        with ExitStack() as stack:
            stack.enter_context(patch.object(dashboard, "_get_api", return_value=Mock()))
            stack.enter_context(patch.object(dashboard, "_get_balance_data", return_value={"output1": [], "output2": [{}]}))
            stack.enter_context(
                patch.object(
                    dashboard,
                    "_parse_balance",
                    return_value={"cash": 500000, "total_eval": 1500000, "pnl": 10000},
                )
            )
            build_runtime_plan = stack.enter_context(
                patch.object(dashboard.trader, "build_runtime_plan", return_value=runtime_bundle)
            )

            payload = dashboard.build_dashboard_execution_plan()

        self._assert_dashboard_compatible_plan(payload["plan"])
        self.assertEqual(payload["plan"], runtime_plan)
        self.assertEqual(
            payload,
            {
                "mode": "dashboard",
                "strategy_id": "seven_split",
                "plan": runtime_plan,
                "cash": 500000,
                "remaining_cash": 260000,
                "total_eval": 1500000,
                "pnl": 10000,
                "daily_loss_halt": False,
                "scanned": 17,
                "scan_error": None,
            },
        )
        build_runtime_plan.assert_called_once()

    def test_run_returns_runtime_plan_that_matches_dashboard_contract(self):
        runtime_plan = self._sample_plan()
        runtime_bundle = {
            "plan": runtime_plan,
            "position_plan_rows": [runtime_plan[0]],
            "candidate_plan_rows": [runtime_plan[1]],
            "candidate_scan": {
                "candidates": [{"ticker": "000660", "score": 4}],
                "scan_summary": [{"ticker": "000660", "score": 4, "rsi": 33, "reasons": ["rsi", "macd"]}],
                "scanned": 1,
                "min_score": 2,
                "scan_error": None,
            },
            "daily_loss_halt": False,
            "cash": 500000,
            "remaining_cash": 260000,
            "total_eval": 1500000,
            "pnl": 10000,
            "held_symbols": {"005930"},
        }
        api = Mock()
        api.get_balance.return_value = {
            "output1": [{"pdno": "005930"}],
            "output2": [{"dnca_tot_amt": "500000", "prvs_rcdl_excc_amt": "500000", "tot_evlu_amt": "1500000"}],
        }

        with ExitStack() as stack:
            stack.enter_context(patch("src.trader.check_secrets"))
            stack.enter_context(patch("src.trader.init_db"))
            stack.enter_context(patch("src.trader.init_approval_db"))
            stack.enter_context(patch("src.trader.KIStockAPI", return_value=api))
            stack.enter_context(patch("src.trader.slack_session_start"))
            slack_candidates = stack.enter_context(patch("src.trader.slack_candidates"))
            stack.enter_context(patch("src.trader.slack_session_end"))
            stack.enter_context(patch("src.trader.check_daily_loss", return_value=False))
            build_runtime_plan = stack.enter_context(
                patch("src.trader.build_runtime_plan", return_value=runtime_bundle)
            )
            execute_plan_row = stack.enter_context(
                patch(
                    "src.trader.execute_plan_row",
                    side_effect=lambda _api, _context, row: {**row, "decision": "queue", "ok": True},
                )
            )

            result = trader.run(mode="analysis_only")

        self._assert_dashboard_compatible_plan(result["plan"])
        self.assertEqual(result["plan"], runtime_plan)
        self.assertEqual([row["symbol"] for row in result["results"]], ["005930", "000660"])
        self.assertTrue(all(row["decision"] == "queue" for row in result["results"]))
        build_runtime_plan.assert_called_once_with(api, api.get_balance.return_value)
        self.assertEqual(execute_plan_row.call_count, 2)
        slack_candidates.assert_not_called()


if __name__ == "__main__":
    unittest.main()
