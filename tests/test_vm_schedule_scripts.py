import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VmScheduleScriptTests(unittest.TestCase):
    def test_domestic_jobs_share_cross_process_lock(self):
        daily = (ROOT / "scripts" / "vm" / "daily-auto.sh").read_text(
            encoding="utf-8"
        )
        dispatch = (ROOT / "scripts" / "vm" / "strategy-dispatch.sh").read_text(
            encoding="utf-8"
        )

        lock_assignment = 'LOCK_FILE="$RUNTIME_DIR/domestic-scheduler.lock"'
        self.assertIn(lock_assignment, daily)
        self.assertIn(lock_assignment, dispatch)
        self.assertIn("flock -n 9", daily)
        self.assertIn("flock -n 9", dispatch)

    def test_domestic_jobs_record_elapsed_time(self):
        daily = (ROOT / "scripts" / "vm" / "daily-auto.sh").read_text(encoding="utf-8")
        dispatch = (ROOT / "scripts" / "vm" / "strategy-dispatch.sh").read_text(encoding="utf-8")

        self.assertIn("duration_seconds=", daily)
        self.assertIn("duration_seconds=", dispatch)

    def test_cron_defaults_avoid_observed_runtime_overlap(self):
        daily_installer = (ROOT / "scripts" / "vm" / "install-daily-auto-cron.sh").read_text(encoding="utf-8")
        dispatch_installer = (ROOT / "scripts" / "vm" / "install-strategy-dispatch-cron.sh").read_text(encoding="utf-8")

        self.assertIn('TIME_SPEC="${1:-0 9,15 * * 1-5}"', daily_installer)
        self.assertIn('TIME_SPEC="${1:-7-57/10 9-15 * * 1-5}"', dispatch_installer)

    def test_trade_sync_runs_after_market_close(self):
        installer = (ROOT / "scripts" / "vm" / "install-trade-sync-cron.sh").read_text(
            encoding="utf-8"
        )
        trigger = (ROOT / "scripts" / "vm" / "trade-sync.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('TIME_SPEC="${1:-10 16 * * 1-5}"', installer)
        self.assertIn('MARKER="${REPO_NAME//_/-}-trade-sync"', installer)
        self.assertIn("/api/trades/sync?days=$DAYS", trigger)
        self.assertIn('LOCK_FILE="$RUNTIME_DIR/trade-sync-trigger.lock"', trigger)


if __name__ == "__main__":
    unittest.main()
