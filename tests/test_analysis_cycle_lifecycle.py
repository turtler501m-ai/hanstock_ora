import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

from src.dashboard.routes.stock import start_analysis_cycle, trigger_scheduler_run
from src.dashboard.services.analysis_cycle_service import (
    AnalysisCycleError,
    load_or_capture_common_stage,
    mark_common_analysis_stage,
    start_common_analysis_cycle,
)
from src.db.analysis_repository import (
    get_analysis_cycle_stage,
    record_analysis_cycle_stage,
)


class AnalysisCycleLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch(
            "src.config.config.trade_db_path",
            Path(self.temp_dir.name) / "analysis-cycle.db",
        )
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_cycle_persists_snapshot_results_and_completes(self):
        cycle = start_common_analysis_cycle("news_ai_v1", "demo")
        record_analysis_cycle_stage(
            cycle["id"],
            "account_balance",
            payload={"cash": 1_000_000, "holdings": []},
        )
        mark_common_analysis_stage(
            cycle["id"],
            "candidates",
            payload={"candidates": [{"ticker": "005930"}]},
        )
        mark_common_analysis_stage(
            cycle["id"],
            "signals",
            payload={"signals": []},
        )
        completed = mark_common_analysis_stage(
            cycle["id"],
            "execution_plan",
            payload={"plan": []},
        )

        snapshot = get_analysis_cycle_stage(cycle["id"], "account_balance")
        candidates = get_analysis_cycle_stage(cycle["id"], "candidates")
        self.assertEqual(snapshot["payload"]["cash"], 1_000_000)
        self.assertEqual(candidates["payload"]["candidates"][0]["ticker"], "005930")
        self.assertEqual(completed["status"], "completed")

    def test_failed_stage_marks_cycle_failed(self):
        cycle = start_common_analysis_cycle("news_ai_v1", "demo")
        failed = mark_common_analysis_stage(
            cycle["id"],
            "signals",
            status="failed",
            details={"error": "provider timeout"},
        )
        self.assertEqual(failed["status"], "failed")
        mark_common_analysis_stage(cycle["id"], "candidates", payload={"candidates": []})
        mark_common_analysis_stage(cycle["id"], "execution_plan", payload={"plan": []})
        still_failed = mark_common_analysis_stage(cycle["id"], "signals", payload={"signals": []})
        self.assertEqual(still_failed["status"], "failed")

    def test_execution_plan_completes_cycle_without_optional_signals_stage(self):
        cycle = start_common_analysis_cycle("news_ai_v1", "demo")
        mark_common_analysis_stage(
            cycle["id"],
            "candidates",
            payload={"candidates": []},
        )
        completed = mark_common_analysis_stage(
            cycle["id"],
            "execution_plan",
            payload={"plan": []},
        )

        self.assertEqual(completed["status"], "completed")

    def test_account_snapshot_is_captured_once_per_cycle(self):
        cycle = start_common_analysis_cycle("news_ai_v1", "demo")
        captures = []

        def capture():
            captures.append(True)
            return {"cash": 1_000_000, "holdings": [{"symbol": "005930"}]}

        first = load_or_capture_common_stage(cycle["id"], "account_balance", capture)
        second = load_or_capture_common_stage(cycle["id"], "account_balance", capture)

        self.assertEqual(len(captures), 1)
        self.assertEqual(first, second)

    def test_isolated_strategies_cannot_start_common_cycle(self):
        for strategy_id in (
            "plunge_bounce_strategy",
            "heikin_ashi_scalping_strategy",
        ):
            with self.subTest(strategy_id=strategy_id):
                with self.assertRaises(AnalysisCycleError):
                    start_common_analysis_cycle(strategy_id, "demo")

    def test_unknown_strategy_is_rejected_instead_of_falling_back(self):
        with patch(
            "src.dashboard.routes.stock.stock_service.resolve_dashboard_strategy",
            return_value=None,
        ):
            with self.assertRaises(HTTPException) as raised:
                start_analysis_cycle({"strategy_id": "missing-strategy"})
        self.assertEqual(raised.exception.status_code, 404)

    def test_common_scheduler_accepts_isolated_strategy_for_isolated_runner(self):
        with patch(
            "src.dashboard.routes.stock._dashboard_scheduler_service.claim",
            return_value=True,
        ), patch(
            "src.dashboard.routes.stock.threading.Thread",
        ) as thread:
            result = trigger_scheduler_run(
                {
                    "mode": "analysis_only",
                    "strategy_ids": ["plunge_bounce_strategy"],
                }
            )

        self.assertEqual(result["status"], "started")
        self.assertEqual(result["strategy_ids"], ["plunge_bounce_strategy"])
        thread.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
