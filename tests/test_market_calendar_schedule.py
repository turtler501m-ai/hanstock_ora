import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src import scheduler, strategy_scheduler
from src.db import scheduler_repository


KST = timezone(timedelta(hours=9))


class MarketCalendarScheduleTests(unittest.TestCase):
    def test_domestic_schedule_is_not_due_on_exchange_holiday(self):
        schedule = {
            "enabled": True,
            "weekdays": "1-5",
            "start_hm": "0900",
            "end_hm": "1530",
            "interval_minutes": 5,
            "last_run_at": None,
        }
        now = datetime(2026, 7, 27, 10, 0, tzinfo=KST)
        with patch(
            "src.utils.market_calendar.is_market_session", return_value=False
        ):
            self.assertFalse(scheduler_repository.is_schedule_due(schedule, now=now))

    def test_dispatch_main_returns_failure_when_a_strategy_fails(self):
        with patch.object(
            strategy_scheduler, "dispatch_due_schedules", return_value=[]
        ), patch.object(
            strategy_scheduler, "_last_dispatch_failures", ["broken: error"]
        ):
            self.assertEqual(strategy_scheduler.main(), 1)

    def test_daily_auto_main_skips_exchange_holiday(self):
        with patch.object(
            scheduler.sys, "argv", ["scheduler", "--mode", "daily_auto"]
        ), patch.object(
            scheduler, "is_market_session", return_value=False
        ), patch.object(
            scheduler, "run_scheduled_cycle"
        ) as run_mock:
            self.assertEqual(scheduler.main(), 0)
        run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
