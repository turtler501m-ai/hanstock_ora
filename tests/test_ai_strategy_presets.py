import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.dashboard as dashboard
from src.dashboard.routes import stock
from src.db import repository


class AiStrategyPresetTests(unittest.TestCase):
    def test_hanstock_easy_preset_requires_explicit_lifecycle_approval(self):
        original_db_path = dashboard.trader.config.trade_db_path
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                dashboard.trader.config.trade_db_path = str(Path(tmpdir) / "trades.sqlite")
                backup_path = Path(tmpdir) / "ai_strategies.json"
                with patch.object(repository, "AI_STRATEGIES_FILE", backup_path), patch.object(
                    stock,
                    "_build_strategy_backtest",
                    return_value={"ok": True, "success": True, "status": "passed"},
                ):
                    result = stock.apply_ai_strategy_preset("balanced")
                    strategies = repository.load_ai_strategies()
        finally:
            dashboard.trader.config.trade_db_path = original_db_path

        selected = [item for item in strategies if item.get("selected")]
        self.assertTrue(result["ok"])
        self.assertEqual(result["preset"], "balanced")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["id"], result["strategy"]["id"])
        self.assertEqual(selected[0]["status"], "paper_passed")
        self.assertEqual(selected[0]["provider"], "none")
        self.assertEqual(selected[0]["profile"]["risk"]["paper_trading_required_days"], 0)
        self.assertEqual(selected[0]["profile"]["risk"]["max_total_open_risk_pct"], 2.0)
        self.assertEqual(selected[0]["profile"]["risk"]["max_strategy_exposure_pct"], 30.0)
        self.assertTrue(selected[0]["profile"]["market_regime_filter"])

    def test_unknown_hanstock_preset_is_rejected(self):
        with self.assertRaises(Exception):
            stock.apply_ai_strategy_preset("unknown")

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

    def test_one_click_demo_runs_qualification_before_autonomy(self):
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
        ), patch.object(
            stock,
            "_qualify_demo_strategy_one_click",
            return_value={"mode": "one_click"},
        ) as qualify, patch(
            "src.ai_stock.automation_service.run_strategy",
            return_value=expected,
        ):
            result = stock.run_ai_strategy_autonomy("s1", {"market": "KR"})

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["qualification"]["mode"], "one_click"
        )
        qualify.assert_called_once_with("s1")


if __name__ == "__main__":
    unittest.main()
