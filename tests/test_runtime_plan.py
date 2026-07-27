import unittest
from unittest.mock import Mock, patch

from src import trader


class RuntimePlanTests(unittest.TestCase):
    def make_api(self, *, balance, daily=None, quote=None):
        api = Mock()
        api.get_balance.return_value = balance
        api.get_daily.return_value = daily if daily is not None else []
        api.get_quote.return_value = quote if quote is not None else {"current": 0, "ask1": 0, "bid1": 0}
        api.place_order = Mock(return_value={"rt_cd": "0", "msg1": "ok"})
        return api

    def test_run_combines_position_and_candidate_plan_rows(self):
        balance = {
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "Samsung",
                    "evlu_pfls_rt": "5.0",
                }
            ],
            "output2": [
                {
                    "dnca_tot_amt": "1000000",
                    "tot_evlu_amt": "2000000",
                    "evlu_pfls_smtl_amt": "50000",
                }
            ],
        }
        api = self.make_api(balance=balance)

        with (
            patch("src.trader.check_secrets"),
            patch("src.trader.init_db"),
            patch("src.trader.init_approval_db"),
            patch("src.trader.KIStockAPI", return_value=api),
            patch("src.trader.slack_session_start"),
            patch("src.trader.slack_candidates"),
            patch("src.trader.slack_session_end"),
            patch("src.trader.generate_signal", return_value={
                "action": "sell",
                "qty": 2,
                "price": 71000,
                "reason": "trim winner",
                "indicators": {"rsi": 74, "sma20": 10, "sma60": 9, "bb_lo": 8, "bb_hi": 12},
            }),
            patch("src.trader.build_scan_universe", return_value=["000660"]),
            patch("src.trader.find_candidates", return_value={
                "candidates": [{"ticker": "000660", "name": "SK Hynix", "score": 4, "reasons": ["rsi", "macd"]}],
                "scan_summary": [],
                "scanned": 1,
                "min_score": 2,
                "scan_error": None,
            }),
            patch("src.trader.build_orders", return_value=[
                {
                    "ticker": "000660",
                    "quantity": 3,
                    "limit_price": 120000,
                    "estimated_cost": 360360.0,
                    "score": 4,
                    "reasons": ["rsi", "macd"],
                }
            ]),
            patch(
                "src.trader.execute_plan_row",
                side_effect=lambda _api, _context, row: {**row, "ok": True, "decision": "execute"},
            ) as execute_plan_row,
        ):
            result = trader.run()

        self.assertEqual([row["category"] for row in result["plan"]], ["position", "candidate"])
        self.assertEqual([row["symbol"] for row in result["plan"]], ["005930", "000660"])
        self.assertEqual([row["decision"] for row in result["results"]], ["execute", "execute"])
        self.assertEqual([row["action"] for row in result["results"]], ["sell", "buy"])
        self.assertEqual(execute_plan_row.call_count, 2)

    def test_run_skips_buys_but_keeps_position_plan_when_daily_loss_halt_is_active(self):
        balance = {
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "Samsung",
                    "evlu_pfls_rt": "-12.0",
                }
            ],
            "output2": [
                {
                    "dnca_tot_amt": "1000000",
                    "tot_evlu_amt": "2000000",
                    "evlu_pfls_smtl_amt": "-400000",
                }
            ],
        }
        api = self.make_api(balance=balance)

        with (
            patch("src.trader.check_secrets"),
            patch("src.trader.init_db"),
            patch("src.trader.init_approval_db"),
            patch("src.trader.KIStockAPI", return_value=api),
            patch("src.trader.slack_session_start"),
            patch("src.trader.slack_session_end"),
            patch("src.trader.check_daily_loss", return_value=True),
            patch("src.trader.daily_loss_halt_triggered", return_value=True),
            patch("src.trader.generate_signal", return_value={
                "action": "buy",
                "qty": 1,
                "price": 70000,
                "reason": "average down",
                "indicators": {"rsi": 25, "sma20": 10, "sma60": 11, "bb_lo": 9, "bb_hi": 12},
            }),
            patch("src.trader.find_candidates") as find_candidates,
            patch("src.trader.execute_plan_row") as execute_plan_row,
        ):
            result = trader.run()

        self.assertEqual([row["action"] for row in result["plan"]], ["buy"])
        self.assertEqual(result["results"][0]["decision"], "skip")
        self.assertEqual(result["results"][0]["skip_reason"], "daily loss halt blocks buy orders only")
        execute_plan_row.assert_not_called()
        find_candidates.assert_not_called()

    def test_run_executes_sells_when_daily_loss_halt_is_active(self):
        balance = {
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "Samsung",
                    "evlu_pfls_rt": "-12.0",
                }
            ],
            "output2": [
                {
                    "dnca_tot_amt": "1000000",
                    "tot_evlu_amt": "2000000",
                    "evlu_pfls_smtl_amt": "-400000",
                }
            ],
        }
        api = self.make_api(balance=balance)

        with (
            patch("src.trader.check_secrets"),
            patch("src.trader.init_db"),
            patch("src.trader.init_approval_db"),
            patch("src.trader.KIStockAPI", return_value=api),
            patch("src.trader.slack_session_start"),
            patch("src.trader.slack_session_end"),
            patch("src.trader.check_daily_loss", return_value=True),
            patch("src.trader.daily_loss_halt_triggered", return_value=True),
            patch("src.trader.generate_signal", return_value={
                "action": "sell",
                "qty": 1,
                "price": 70000,
                "reason": "stop loss",
                "indicators": {"rsi": 25, "sma20": 10, "sma60": 11, "bb_lo": 9, "bb_hi": 12},
            }),
            patch("src.trader.find_candidates") as find_candidates,
            patch(
                "src.trader.execute_plan_row",
                side_effect=lambda _api, _context, row: {**row, "ok": True, "decision": "execute"},
            ) as execute_plan_row,
        ):
            result = trader.run()

        self.assertEqual([row["action"] for row in result["plan"]], ["sell"])
        self.assertEqual(result["results"][0]["decision"], "execute")
        execute_plan_row.assert_called_once()
        find_candidates.assert_not_called()

    def test_run_skips_position_buys_when_buying_cash_is_unavailable(self):
        balance = {
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "Samsung",
                    "evlu_pfls_rt": "-8.0",
                    "evlu_amt": "2000000",
                }
            ],
            "output2": [
                {
                    "dnca_tot_amt": "-100000",
                    "scts_evlu_amt": "2000000",
                    "tot_evlu_amt": "1900000",
                    "evlu_pfls_smtl_amt": "-100000",
                }
            ],
        }
        api = self.make_api(balance=balance)

        with (
            patch("src.trader.check_secrets"),
            patch("src.trader.init_db"),
            patch("src.trader.init_approval_db"),
            patch("src.trader.KIStockAPI", return_value=api),
            patch("src.trader.slack_session_start"),
            patch("src.trader.slack_session_end"),
            patch("src.trader.check_daily_loss", return_value=False),
            patch("src.trader.daily_loss_halt_triggered", return_value=False),
            patch("src.trader.generate_signal", return_value={
                "action": "buy",
                "qty": 1,
                "price": 70000,
                "reason": "average down",
                "indicators": {"rsi": 35, "sma20": 10, "sma60": 9, "bb_lo": 8, "bb_hi": 12},
            }),
            patch("src.trader.build_scan_universe", return_value=[]),
            patch("src.trader.find_candidates", return_value={
                "candidates": [],
                "scan_summary": [],
                "scanned": 0,
                "min_score": 2,
                "scan_error": None,
            }),
            patch("src.trader.execute_plan_row") as execute_plan_row,
        ):
            result = trader.run()

        self.assertEqual(result["results"][0]["decision"], "skip")
        self.assertEqual(result["results"][0]["skip_reason"], "buying cash unavailable")
        execute_plan_row.assert_not_called()

    def test_run_analysis_only_returns_plan_without_submitting_orders(self):
        balance = {
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "Samsung",
                    "evlu_pfls_rt": "8.0",
                }
            ],
            "output2": [
                {
                    "dnca_tot_amt": "1000000",
                    "tot_evlu_amt": "2000000",
                    "evlu_pfls_smtl_amt": "50000",
                }
            ],
        }
        api = self.make_api(balance=balance)

        with (
            patch("src.trader.check_secrets"),
            patch("src.trader.init_db"),
            patch("src.trader.init_approval_db"),
            patch("src.trader.KIStockAPI", return_value=api),
            patch("src.trader.slack_session_start"),
            patch("src.trader.slack_candidates"),
            patch("src.trader.slack_session_end"),
            patch("src.trader.generate_signal", return_value={
                "action": "sell",
                "qty": 1,
                "price": 72000,
                "reason": "take profit",
                "indicators": {"rsi": 78, "sma20": 12, "sma60": 10, "bb_lo": 9, "bb_hi": 14},
            }),
            patch("src.trader.build_scan_universe", return_value=["000660"]),
            patch("src.trader.find_candidates", return_value={
                "candidates": [{"ticker": "000660", "name": "SK Hynix", "score": 3, "reasons": ["breakout"]}],
                "scan_summary": [],
                "scanned": 1,
                "min_score": 2,
                "scan_error": None,
            }),
            patch("src.trader.build_orders", return_value=[
                {
                    "ticker": "000660",
                    "quantity": 2,
                    "limit_price": 121000,
                    "estimated_cost": 242242.0,
                    "score": 3,
                    "reasons": ["breakout"],
                }
            ]),
            patch("src.trader.queue_approval", side_effect=[101, 102]) as queue_approval,
            patch("src.trader.save_trade") as save_trade,
            patch("src.trader.slack_order") as slack_order,
        ):
            result = trader.run(mode="analysis_only")

        self.assertEqual([row["category"] for row in result["plan"]], ["position", "candidate"])
        self.assertTrue(all(row["decision"] == "queue" for row in result["results"]))
        self.assertTrue(all(row["ok"] for row in result["results"]))
        self.assertEqual(queue_approval.call_count, 2)
        api.place_order.assert_not_called()
        save_trade.assert_not_called()
        slack_order.assert_not_called()

    def test_run_uses_real_check_api_for_plan_and_broker_api_for_execution(self):
        balance = {
            "output1": [{"pdno": "005930", "prdt_name": "Samsung", "evlu_pfls_rt": "-12.0"}],
            "output2": [{"dnca_tot_amt": "1000000", "tot_evlu_amt": "2000000", "evlu_pfls_smtl_amt": "0"}],
        }
        broker_api = self.make_api(balance=balance)
        market_data_api = Mock()
        runtime_bundle = {
            "plan": [
                {
                    "category": "position",
                    "symbol": "005930",
                    "name": "Samsung",
                    "action": "sell",
                    "qty": 1,
                    "price": 0,
                    "reason": "stop loss",
                }
            ],
            "candidate_scan": {"candidates": []},
            "daily_loss_halt": False,
            "remaining_cash": 1000000,
        }

        with (
            patch("src.trader.check_secrets"),
            patch("src.trader.init_db"),
            patch("src.trader.init_approval_db"),
            patch("src.trader.KIStockAPI", return_value=broker_api),
            patch("src.trader.build_market_data_api", return_value=market_data_api) as build_market_data_api,
            patch("src.trader.build_runtime_plan", return_value=runtime_bundle) as build_runtime_plan,
            patch("src.trader.slack_session_start"),
            patch("src.trader.slack_session_end"),
            patch(
                "src.trader.execute_plan_row",
                side_effect=lambda api, _context, row: {
                    **row,
                    "ok": api is broker_api,
                    "decision": "execute",
                },
            ) as execute_plan_row,
        ):
            result = trader.run()

        build_market_data_api.assert_called_once_with(broker_api)
        build_runtime_plan.assert_called_once_with(market_data_api, balance)
        self.assertTrue(result["results"][0]["ok"])
        execute_plan_row.assert_called_once()

    def test_run_can_limit_execution_to_ai_rebalance_category(self):
        balance = {
            "output1": [],
            "output2": [
                {
                    "dnca_tot_amt": "100000",
                    "tot_evlu_amt": "100000",
                    "evlu_pfls_smtl_amt": "0",
                }
            ],
        }
        api = self.make_api(balance=balance)
        runtime_bundle = {
            "plan": [
                {
                    "symbol": "005930",
                    "name": "Samsung",
                    "action": "sell",
                    "qty": 1,
                    "price": 70000,
                    "reason": "trim",
                    "category": "position",
                },
                {
                    "symbol": "000660",
                    "name": "SK Hynix",
                    "action": "sell",
                    "qty": 1,
                    "price": 120000,
                    "reason": "AI rebalance",
                    "category": "ai_rebalance",
                },
            ],
            "candidate_scan": {"candidates": []},
            "remaining_cash": 100000,
            "cash": 100000,
            "daily_loss_halt": False,
            "ai_rebalance_rows": [],
        }

        with (
            patch("src.trader.check_secrets"),
            patch("src.trader.init_db"),
            patch("src.trader.init_approval_db"),
            patch("src.trader.KIStockAPI", return_value=api),
            patch("src.trader.slack_session_start"),
            patch("src.trader.slack_session_end"),
            patch("src.trader.check_daily_loss", return_value=False),
            patch("src.trader.build_runtime_plan", return_value=runtime_bundle),
            patch("src.trader.queue_approval", return_value=202) as queue_approval,
        ):
            result = trader.run(
                mode="analysis_only",
                include_ai_rebalance=True,
                execution_categories={"ai_rebalance"},
            )

        self.assertEqual(result["results"][0]["decision"], "skip")
        self.assertEqual(result["results"][0]["skip_reason"], "category filtered")
        self.assertEqual(result["results"][1]["approval_id"], 202)
        queue_approval.assert_called_once_with(
            "000660",
            "SK Hynix",
            "sell",
            1,
            120000,
            "AI rebalance",
            source="trader",
        )

    def test_execute_plan_row_keeps_router_pending_result_as_queue(self):
        api = Mock()
        router = Mock()
        router.route.return_value = {
            "ok": True,
            "msg": "Added to approval queue",
            "status": "pending",
            "approval_id": 321,
        }
        row = {
            "symbol": "005930",
            "name": "Samsung",
            "action": "buy",
            "qty": 3,
            "price": 70000,
            "reason": "approval required",
        }

        result = trader.execute_plan_row(api, {"router": router}, row)

        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "queue")
        self.assertEqual(result["approval_id"], 321)


if __name__ == "__main__":
    unittest.main()
