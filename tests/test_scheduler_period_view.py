import unittest
from inspect import signature
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


class SchedulerPeriodViewTests(unittest.TestCase):
    def test_frontend_offers_daily_weekly_and_monthly_periods(self):
        html = (ROOT / "web/templates/index.html").read_text(encoding="utf-8")
        script = (ROOT / "web/static/js/app.js").read_text(encoding="utf-8")

        self.assertIn('id="sched-result-period"', html)
        self.assertIn('<option value="daily" selected>', html)
        self.assertIn('<option value="weekly">', html)
        self.assertIn('<option value="monthly">', html)
        self.assertIn("new URLSearchParams({ period })", script)
        self.assertIn("?.value || 'daily'", script)

    def test_scheduler_status_defaults_to_daily_period(self):
        from src.dashboard.routes.stock_plan import get_scheduler_status

        self.assertEqual(signature(get_scheduler_status).parameters["period"].default, "daily")

    def test_scheduler_status_aggregates_selected_period(self):
        from src.dashboard import get_scheduler_status
        from src.config import config
        from src.db.repository import init_db, save_scheduler_result
        from src.db.scheduler_repository import KST

        original_db_path = config.trade_db_path
        try:
            with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                config.trade_db_path = str(Path(temp_dir) / "trades.sqlite")
                init_db()
                now = datetime.now(KST)
                for days_ago, symbol in ((0, "005930"), (3, "000660"), (15, "035420")):
                    recorded_at = (now - timedelta(days=days_ago)).isoformat()
                    save_scheduler_result(
                        "execute",
                        recorded_at,
                        {"results": [{"symbol": symbol}], "auto_approved": []},
                    )

                daily = get_scheduler_status(period="daily", compact=False)
                weekly = get_scheduler_status(period="weekly", compact=False)
                monthly = get_scheduler_status(period="monthly", compact=False)

                self.assertEqual(daily["result_range_days"], 1)
                self.assertEqual(daily["last_result"]["period_label"], "일별")
                self.assertEqual(len(daily["last_result"]["result"]["execution_runs"]), 1)
                self.assertEqual(len(weekly["last_result"]["result"]["execution_runs"]), 2)
                self.assertEqual(len(monthly["last_result"]["result"]["execution_runs"]), 3)
        finally:
            config.trade_db_path = original_db_path


if __name__ == "__main__":
    unittest.main()
