import json
import math
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import src.dashboard as dashboard
from src.db.repository import connect_db, init_db, load_ai_strategies, save_ai_strategies


class AiStrategyLifecycleTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with connect_db() as conn:
            conn.execute("DELETE FROM ai_strategies")
            conn.execute("DELETE FROM ai_strategy_events")
            conn.commit()
        self.original_backtest_pass = getattr(dashboard.trader.config, "ai_require_backtest_pass", True)
        dashboard.trader.config.ai_require_backtest_pass = True
        save_ai_strategies([
            {
                "id": "lifecycle_rule",
                "name": "Lifecycle Rule",
                "provider": "none",
                "model": "none",
                "weight": 0.0,
                "description": "Local rule strategy for lifecycle tests",
                "selected": True,
                "status": "draft",
                "profile": {
                    "model": "none",
                    "ai_weight": 0.0,
                    "risk": {
                        "max_risk_per_trade_pct": 1.0,
                        "paper_trading_required_days": 20,
                    },
                    "backtest": {
                        "commission_bps": 15,
                        "slippage_bps": 5,
                        "market_impact_bps": 5,
                    },
                },
            }
        ])

    def tearDown(self):
        dashboard.trader.config.ai_require_backtest_pass = self.original_backtest_pass


    def test_strategy_context_route_exposes_active_gate(self):
        body = dashboard.get_strategy_context()

        self.assertEqual(body["active_strategy"]["id"], "lifecycle_rule")
        self.assertFalse(body["active_strategy"]["approval_gate"]["ok"])
        self.assertIn("static verification", body["active_strategy"]["approval_gate"]["missing"])
        self.assertFalse(body["active_strategy"]["operation_status"]["ready"])
        self.assertEqual(body["active_strategy"]["operation_status"]["mode"], "blocked")
        self.assertTrue(body["safety"]["require_backtest_pass"])

        list_body = dashboard.get_ai_strategies()
        listed = next(strategy for strategy in list_body["strategies"] if strategy["id"] == "lifecycle_rule")
        self.assertFalse(listed["approval_gate"]["ok"])
        self.assertFalse(listed["operation_status"]["ready"])
        self.assertEqual(listed["display_name"], "Lifecycle Rule")
        self.assertEqual(listed["status_label"], "초안")
        self.assertEqual(listed["selected_label"], "현재 사용")
        self.assertEqual(listed["approval_gate"]["label"], "필수 검증 미완료: 정적검증, 백테스트, 모의운영")
        self.assertEqual(listed["operation_status"]["label"], "승인/검증 필요")

    def test_strategy_apis_normalize_non_finite_validation_values(self):
        strategy = load_ai_strategies()[0]
        strategy["last_validation_result"] = json.dumps(
            {
                "checks": {
                    "backtest": {
                        "success": True,
                        "metrics": {"total_return_pct": math.nan},
                        "equity_curve": [100.0, math.nan],
                    }
                }
            }
        )

        with patch("src.db.repository.load_ai_strategies", return_value=[strategy]):
            context = dashboard.get_strategy_context()
            body = dashboard.get_ai_strategies()

        validation = context["active_strategy"]["validation"]
        self.assertIsNone(validation["checks"]["backtest"]["metrics"]["total_return_pct"])
        json.dumps(context, allow_nan=False)
        raw_validation = body["strategies"][0]["last_validation_result"]
        self.assertNotIn("NaN", raw_validation)
        parsed = json.loads(raw_validation)
        self.assertIsNone(parsed["checks"]["backtest"]["equity_curve"][1])

    def test_approval_requires_static_backtest_and_paper_checks(self):
        with self.assertRaises(HTTPException) as blocked:
            dashboard.approve_ai_strategy("lifecycle_rule")
        self.assertEqual(blocked.exception.status_code, 409)

        static_result = dashboard.static_verify_ai_strategy("lifecycle_rule")
        self.assertTrue(static_result["result"]["success"])

        backtest_fixture = {
            "ok": True,
            "success": True,
            "status": "passed",
            "return_pct": 3.2,
            "max_drawdown_pct": 2.1,
        }
        with patch("src.dashboard.routes.stock._build_strategy_backtest", return_value=backtest_fixture):
            backtest_result = dashboard.backtest_ai_strategy("lifecycle_rule")
        self.assertTrue(backtest_result["result"]["success"])
        self.assertEqual(backtest_result["strategy"]["status"], "backtested")

        start_result = dashboard.start_ai_strategy_paper("lifecycle_rule")
        self.assertEqual(start_result["strategy"]["status"], "paper_running")

        complete_result = dashboard.complete_ai_strategy_paper(
            "lifecycle_rule",
            dashboard.PaperCompletePayload(days=20, observations=20, return_pct=1.2, max_drawdown_pct=2.5),
        )
        self.assertTrue(complete_result["result"]["success"])
        self.assertEqual(complete_result["strategy"]["status"], "paper_passed")

        approved = dashboard.approve_ai_strategy("lifecycle_rule")
        self.assertEqual(approved["strategy"]["status"], "approved")

        context = dashboard.get_strategy_context()
        self.assertTrue(context["active_strategy"]["approval_gate"]["ok"])
        self.assertTrue(context["active_strategy"]["operation_status"]["ready"])

        loaded = load_ai_strategies()
        found = next(strategy for strategy in loaded if strategy["id"] == "lifecycle_rule")
        self.assertEqual(found["status"], "approved")
        self.assertTrue(found["last_backtested_at"])
        self.assertTrue(found["last_paper_started_at"])
        self.assertTrue(found["last_paper_completed_at"])
        self.assertIn("backtest", found["last_validation_result"])
        self.assertIn("paper", found["last_validation_result"])


if __name__ == "__main__":
    unittest.main()
