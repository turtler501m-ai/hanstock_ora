import unittest

from src.dashboard.presenters.scheduler_presenter import (
    _compact_scheduler_status_result,
)


class SchedulerPresenterTests(unittest.TestCase):
    def test_multi_strategy_result_keeps_run_and_block_reasons(self):
        payload = {
            "result": {
                "status": "success",
                "ok": True,
                "strategy_ids": ["s1"],
                "runs": [
                    {
                        "strategy_id": "s1",
                        "cycle_id": "c1",
                        "result": {
                            "scan": {"candidate_count": 17, "status": "completed"},
                            "automation": {
                                "planned": 0,
                                "blocked": ["invalid candidate price"],
                            },
                            "autonomy": {"error": "invalid candidate price"},
                        },
                    }
                ],
                "errors": [],
            }
        }

        compact = _compact_scheduler_status_result(payload)

        self.assertEqual(compact["result"]["summary_counts"]["run_count"], 1)
        self.assertEqual(compact["result"]["summary_counts"]["blocked_count"], 1)
        self.assertEqual(
            compact["result"]["runs"][0]["blocked"],
            ["invalid candidate price"],
        )


if __name__ == "__main__":
    unittest.main()
