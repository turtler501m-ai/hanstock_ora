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


if __name__ == "__main__":
    unittest.main()
