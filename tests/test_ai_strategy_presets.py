import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.dashboard as dashboard
from src.dashboard.routes import stock
from src.db import repository


class AiStrategyPresetTests(unittest.TestCase):
    def test_hanstock_easy_preset_is_ready_for_demo_trading(self):
        original_db_path = dashboard.trader.config.trade_db_path
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = str(Path(tmpdir) / "trades.sqlite")
                backup_path = Path(tmpdir) / "ai_strategies.json"
                with patch.object(repository, "AI_STRATEGIES_FILE", backup_path):
                    result = stock.apply_ai_strategy_preset("balanced")
                    strategies = repository.load_ai_strategies()
        finally:
            dashboard.trader.config.trade_db_path = original_db_path

        selected = [item for item in strategies if item.get("selected")]
        self.assertTrue(result["ok"])
        self.assertEqual(result["preset"], "balanced")
        applied = next(
            item for item in selected
            if item["id"] == result["strategy"]["id"]
        )
        self.assertGreaterEqual(len(selected), 1)
        self.assertEqual(applied["status"], "approved")
        self.assertEqual(applied["provider"], "none")
        self.assertNotIn("paper_trading_required_days", applied["profile"]["risk"])
        self.assertNotIn("backtest", applied["profile"])
        self.assertEqual(applied["profile"]["risk"]["max_total_open_risk_pct"], 2.0)
        self.assertEqual(applied["profile"]["risk"]["max_strategy_exposure_pct"], 30.0)
        self.assertTrue(applied["profile"]["market_regime_filter"])

    def test_unknown_hanstock_preset_is_rejected(self):
        with self.assertRaises(Exception):
            stock.apply_ai_strategy_preset("unknown")

    def test_applying_strategy_prepares_disabled_ai_schedule_slot(self):
        strategies = [
            {
                "id": "approved_ai_1",
                "selected": True,
                "status": "approved",
                "strategy_version": 1,
            },
            {
                "id": "approved_ai_2",
                "selected": True,
                "status": "approved",
                "strategy_version": 2,
            },
        ]
        with patch(
            "src.db.repository.load_ai_strategies",
            return_value=[dict(strategy) for strategy in strategies],
        ), patch(
            "src.db.repository.record_ai_strategy_event",
        ), patch(
            "src.db.repository.list_strategy_schedules",
            return_value=[],
        ), patch(
            "src.db.repository.save_strategy_schedule",
        ) as save_schedule:
            result = stock.apply_selected_ai_strategies()

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["applied_strategy_ids"],
            ["approved_ai_1", "approved_ai_2"],
        )
        self.assertEqual(result["schedule_strategy_id"], "ai_stock_default_v1")
        save_schedule.assert_called_once_with(
            "ai_stock_default_v1",
            enabled=False,
            mode="analysis_only",
            auto_approve=False,
        )

    def test_main_hanstock_ai_strategy_can_run_autonomy(self):
        expected = {
            "scan": {"status": "completed"},
            "automation": {"planned": 1, "approved": 1},
            "autonomy": {
                "managed_orders": [{"id": 11}],
                "approvals": [{"approval_id": 22}],
            },
        }
        strategy = {
            "id": "main_ai_strategy",
            "status": "approved",
        }
        with patch(
            "src.db.repository.load_ai_strategies",
            return_value=[strategy],
        ), patch(
            "src.config.config.autonomy_enabled", True
        ), patch(
            "src.ai_stock.automation_service.run_strategy",
            return_value=expected,
        ) as run:
            result = stock.run_ai_strategy_autonomy(
                "main_ai_strategy", {"market": "KR"}
            )

        self.assertTrue(result["ok"])
        run.assert_called_once_with(
            market="KR",
            strategy_id="main_ai_strategy",
            run_type="dashboard_manual",
        )

    def test_demo_autonomy_does_not_run_legacy_qualification(self):
        expected = {
            "scan": {"status": "completed"},
            "automation": {"planned": 0, "approved": 0},
            "autonomy": {"managed_orders": [], "approvals": [], "executions": []},
        }
        with patch(
            "src.db.repository.load_ai_strategies",
            return_value=[{"id": "s1", "status": "draft"}],
        ), patch(
            "src.config.config.autonomy_enabled", True
        ), patch(
            "src.config.config.autonomy_require_approval", False
        ), patch(
            "src.ai_stock.automation_service.run_strategy",
            return_value=expected,
        ):
            result = stock.run_ai_strategy_autonomy("s1", {"market": "KR"})

        self.assertTrue(result["ok"])
        self.assertNotIn("qualification", result)


if __name__ == "__main__":
    unittest.main()
