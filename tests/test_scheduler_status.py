import unittest

from src.db.scheduler_repository import normalize_scheduler_status


class SchedulerStatusTests(unittest.TestCase):
    def test_normalizes_terminal_outcomes(self):
        cases = [
            ({"ok": True}, "success"),
            ({"ok": False}, "failed"),
            ({"blocked": ["risk gate"]}, "blocked"),
            ({"skipped": True}, "skipped"),
            ({"errors": ["one"], "results": [{"symbol": "005930"}]}, "partial"),
            ({"errors": ["one"]}, "failed"),
        ]
        for result, expected in cases:
            with self.subTest(result=result):
                self.assertEqual(normalize_scheduler_status(result), expected)


if __name__ == "__main__":
    unittest.main()
