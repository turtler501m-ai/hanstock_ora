import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import json

from src.dashboard.services.scheduler_service import DashboardSchedulerService
from src.dashboard import (
    _bg_run_multiple_scheduled_cycles,
    _scheduler_running_lock,
    _scheduler_run_state,
    get_scheduler_status,
    trigger_scheduler_run,
)

class SchedulerApiTests(unittest.TestCase):
    def setUp(self):
        # Reset global state
        with _scheduler_running_lock:
            _scheduler_run_state["is_running"] = False
            _scheduler_run_state["mode"] = None
            _scheduler_run_state["started_at"] = None
            _scheduler_run_state["completed_at"] = None
            _scheduler_run_state["result"] = None
            _scheduler_run_state["error"] = None
        
        # Clear database
        from src.db.repository import init_db, connect_db
        init_db()
        with connect_db() as conn:
            conn.execute("DELETE FROM scheduler_results")
            conn.commit()

    @patch("src.dashboard.Path.exists", return_value=False)
    @patch("src.db.repository.load_latest_scheduler_result", return_value=None)
    def test_get_scheduler_status_handles_missing_file_gracefully(self, mock_load, mock_exists):
        status = get_scheduler_status()
        self.assertIn("config", status)
        self.assertIn("last_result", status)
        self.assertIsNone(status["last_result"])
        self.assertFalse(status["run_state"]["is_running"])

    @patch("src.dashboard.Path.exists", return_value=True)
    @patch("src.dashboard.Path.read_text", return_value='{"mode": "daily_auto", "result": {"results": []}}')
    def test_get_scheduler_status_loads_existing_result(self, mock_read, mock_exists):
        status = get_scheduler_status()
        self.assertIsNotNone(status["last_result"])
        self.assertEqual(status["last_result"]["mode"], "daily_auto")

    @patch(
        "src.db.repository.load_ai_strategies",
        return_value=[
            {"id": "selected_strategy", "model": "none", "name": "Selected", "selected": True},
            {"id": "requested_strategy", "model": "none", "name": "Requested", "selected": False},
        ],
    )
    def test_get_scheduler_status_uses_requested_strategy_context(self, mock_load):
        status = get_scheduler_status(strategy_id="requested_strategy")

        self.assertEqual(status["active_strategy_id"], "requested_strategy")
        self.assertEqual(status["active_strategy_name"], "Requested")

    @patch(
        "src.db.repository.load_ai_strategies",
        return_value=[
            {"id": "selected_strategy", "model": "none", "name": "Selected", "selected": True},
        ],
    )
    def test_get_scheduler_status_reports_strategy_id_not_model(self, mock_load):
        status = get_scheduler_status()

        self.assertEqual(status["active_strategy_id"], "selected_strategy")
        self.assertEqual(status["active_strategy_name"], "Selected")

    @patch("src.db.repository.load_strategy_universe", return_value=["005930", "000660"])
    @patch(
        "src.db.repository.list_strategy_schedules",
        return_value=[
            {
                "strategy_id": "heikin_ashi_scalping_strategy",
                "enabled": 1,
                "interval_minutes": 5,
                "start_hm": "0900",
                "end_hm": "1530",
                "weekdays": "1-5",
                "mode": "execute",
                "auto_approve": 1,
                "last_run_at": None,
            }
        ],
    )
    @patch(
        "src.db.repository.load_ai_strategies",
        return_value=[
            {
                "id": "heikin_ashi_scalping_strategy",
                "model": "none",
                "name": "알파 하이킨아시",
                "selected": False,
            }
        ],
    )
    def test_get_scheduler_status_adds_korean_schedule_display(self, mock_load, mock_schedules, mock_universe):
        status = get_scheduler_status(strategy_id="heikin_ashi_scalping_strategy")

        self.assertEqual(status["active_strategy_name"], "알파 하이킨아시")
        self.assertEqual(status["strategy_dispatch"]["summary"], "사용 1개 / 전체 1개 / 감시종목 2개")
        schedule = status["strategy_dispatch"]["schedules"][0]
        self.assertEqual(schedule["display_name"], "알파 하이킨아시")
        self.assertEqual(schedule["enabled_label"], "사용 중")
        self.assertEqual(schedule["interval_label"], "5분마다")
        self.assertEqual(schedule["window_label"], "월-금 09:00-15:30")
        self.assertEqual(schedule["mode_label"], "주문실행")
        self.assertEqual(schedule["auto_approve_label"], "자동승인")

    @patch("src.db.repository.load_strategy_universe", return_value=[])
    @patch(
        "src.db.repository.list_strategy_schedules",
        return_value=[{
            "strategy_id": "narrative_momentum_strategy",
            "enabled": 1,
            "interval_minutes": 30,
            "start_hm": "0900",
            "end_hm": "1530",
            "weekdays": "1-5",
            "mode": "analysis_only",
            "auto_approve": 0,
            "last_run_at": None,
        }],
    )
    @patch("src.db.repository.load_ai_strategies", return_value=[])
    def test_scheduler_status_includes_narrative_momentum_schedule(
        self, mock_load, mock_schedules, mock_universe
    ):
        status = get_scheduler_status()

        self.assertEqual(status["strategy_dispatch"]["enabled_count"], 1)
        self.assertEqual(status["strategy_dispatch"]["schedule_count"], 1)
        self.assertEqual(
            status["strategy_dispatch"]["schedules"][0]["strategy_id"],
            "narrative_momentum_strategy",
        )

    @patch("src.db.repository.load_strategy_universe", return_value=[])
    @patch(
        "src.db.repository.list_strategy_schedules",
        return_value=[{
            "strategy_id": "ai_stock_default_v1",
            "enabled": 1,
            "interval_minutes": 5,
            "start_hm": "0900",
            "end_hm": "1530",
            "weekdays": "1-5",
            "mode": "execute",
            "auto_approve": 1,
            "last_run_at": None,
        }],
    )
    @patch(
        "src.db.repository.load_ai_strategies",
        return_value=[
            {
                "id": "easy_aggressive_live",
                "model": "none",
                "name": "쉬운 공격형 전략",
                "selected": True,
                "status": "approved",
            },
            {
                "id": "easy_balanced_live",
                "model": "none",
                "name": "쉬운 균형형 전략",
                "selected": True,
                "status": "approved",
            },
        ],
    )
    def test_scheduler_status_names_ai_slot_after_applied_strategy(
        self, mock_load, mock_schedules, mock_universe
    ):
        status = get_scheduler_status()

        schedules = status["strategy_dispatch"]["schedules"]
        self.assertEqual(
            [schedule["strategy_id"] for schedule in schedules],
            ["easy_aggressive_live", "easy_balanced_live"],
        )
        self.assertEqual(
            [schedule["display_name"] for schedule in schedules],
            ["쉬운 공격형 전략", "쉬운 균형형 전략"],
        )
        self.assertTrue(all(schedule["shared_schedule"] for schedule in schedules))
        self.assertTrue(all(
            schedule["schedule_strategy_id"] == "ai_stock_default_v1"
            for schedule in schedules
        ))
        self.assertTrue(all(
            schedule["execution_policy_label"] in {
                "자동 주문 실행", "승인 대기열 등록", "계획만 생성"
            }
            for schedule in schedules
        ))

    def test_get_scheduler_status_merges_last_30_days_scheduler_results(self):
        from datetime import datetime, timedelta
        from src.db.scheduler_repository import KST
        from src.db.repository import save_scheduler_result

        today = datetime.now(KST)
        in_range_day = today - timedelta(days=10)
        old_day = today - timedelta(days=40)
        save_scheduler_result(
            "execute",
            f"{in_range_day.strftime('%Y-%m-%d')}T09:00:00+09:00",
            {"results": [{"symbol": "005930", "reason": "first"}], "auto_approved": []},
        )
        save_scheduler_result(
            "execute",
            f"{today.strftime('%Y-%m-%d')} 10:00:00",
            {"results": [{"symbol": "000660", "reason": "second"}], "auto_approved": []},
        )
        save_scheduler_result(
            "execute",
            f"{old_day.strftime('%Y-%m-%d')} 10:00:00",
            {"results": [{"symbol": "035420", "reason": "old"}], "auto_approved": []},
        )

        status = get_scheduler_status()
        rows = status["last_result"]["result"]["results"]
        runs = status["last_result"]["result"]["execution_runs"]

        self.assertEqual([row["symbol"] for row in rows], ["005930", "000660"])
        self.assertEqual([row["round"] for row in rows], [1, 2])
        self.assertEqual(len(runs), 2)
        self.assertEqual([run["round"] for run in runs], [1, 2])
        self.assertEqual(status["last_result"]["range_days"], 30)
        self.assertIn("run_date", rows[0])

    def test_get_scheduler_status_enriches_names_and_approved_order_details(self):
        from datetime import datetime
        from src.db.scheduler_repository import KST
        from src.db.repository import connect_db, save_scheduler_result

        now = datetime.now(KST)
        created_at = now.strftime("%Y-%m-%d %H:%M:%S")
        with connect_db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO approvals (
                    created_at, updated_at, symbol, name, action, qty, price,
                    reason, source, status, response_msg
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at, created_at, "251340", "KODEX 코스닥150선물인버스",
                    "buy", 3, 2800, "test", "scheduler-test", "executed", "ok",
                ),
            )
            approval_id = cursor.lastrowid
            conn.commit()

        save_scheduler_result(
            "execute",
            now.isoformat(),
            {
                "results": [{"symbol": "251340", "name": "251340", "category": "candidate"}],
                "auto_approved": [{"id": approval_id, "status": "executed"}],
            },
        )

        status = get_scheduler_status()
        result = status["last_result"]["result"]
        plan = next(row for row in result["results"] if row["symbol"] == "251340")
        order = next(row for row in result["auto_approved"] if row["id"] == approval_id)

        self.assertEqual(plan["name"], "KODEX 코스닥150선물인버스")
        self.assertEqual(order["symbol"], "251340")
        self.assertEqual(order["name"], "KODEX 코스닥150선물인버스")
        self.assertEqual(order["action"], "buy")
        self.assertEqual(order["qty"], 3)

    def test_get_scheduler_status_compacts_large_result_but_keeps_counts(self):
        from datetime import datetime
        from src.db.scheduler_repository import KST
        from src.db.repository import save_scheduler_result

        now = datetime.now(KST)
        long_reason = "x" * 700
        save_scheduler_result(
            "execute",
            f"{now.strftime('%Y-%m-%d')}T09:00:00+09:00",
            {
                "results": [
                    {
                        "symbol": f"{idx:06d}",
                        "name": f"stock-{idx}",
                        "decision": "queue",
                        "reason": long_reason,
                    }
                    for idx in range(120)
                ],
                "auto_approved": [{"id": 1, "status": "executed"}],
                "auto_approval_errors": [],
            },
        )

        status = get_scheduler_status()
        last_result = status["last_result"]
        result = last_result["result"]

        self.assertTrue(last_result["compact"])
        self.assertEqual(len(result["results"]), 100)
        self.assertEqual(result["results"][0]["symbol"], "000020")
        self.assertEqual(result["summary_counts"]["plan_count"], 120)
        self.assertEqual(result["summary_counts"]["queue_count"], 119)
        self.assertEqual(result["summary_counts"]["approved_count"], 1)
        self.assertEqual(result["summary_counts"]["success_count"], 1)
        self.assertLessEqual(len(result["results"][0]["reason"]), 500)

    def test_scheduler_status_compaction_summarizes_candidate_scan(self):
        from src.dashboard.routes.stock import _compact_scheduler_status_result

        last_result = {
            "mode": "execute",
            "recorded_at": "2026-06-19T09:00:00+09:00",
            "result": {
                "results": [],
                "auto_approved": [],
                "candidate_scan": {
                    "scanned": 120,
                    "candidates": [
                        {"symbol": f"{idx:06d}", "score": idx, "payload": {"large": "x" * 700}}
                        for idx in range(30)
                    ],
                    "scan_summary": [{"symbol": f"{idx:06d}"} for idx in range(120)],
                },
            },
        }

        result = _compact_scheduler_status_result(last_result)["result"]

        self.assertEqual(result["candidate_scan"]["scanned_count"], 120)
        self.assertEqual(result["candidate_scan"]["candidates_count"], 30)
        self.assertEqual(result["candidate_scan"]["summary_count"], 120)
        self.assertEqual(len(result["candidate_scan"]["candidates"]), 20)
        self.assertNotIn("payload", result["candidate_scan"]["candidates"][0])

    def test_get_scheduler_status_can_return_full_result_when_requested(self):
        from datetime import datetime
        from src.db.scheduler_repository import KST
        from src.db.repository import save_scheduler_result

        now = datetime.now(KST)
        save_scheduler_result(
            "execute",
            f"{now.strftime('%Y-%m-%d')}T09:00:00+09:00",
            {
                "results": [{"symbol": f"{idx:06d}"} for idx in range(105)],
                "auto_approved": [],
            },
        )

        status = get_scheduler_status(compact=False)

        self.assertNotIn("compact", status["last_result"])
        self.assertEqual(len(status["last_result"]["result"]["results"]), 105)

    def test_get_scheduler_status_compacts_run_state_result(self):
        with _scheduler_running_lock:
            _scheduler_run_state["is_running"] = False
            _scheduler_run_state["mode"] = "execute"
            _scheduler_run_state["result"] = {
                "results": [{"symbol": f"{idx:06d}", "reason": "x" * 700} for idx in range(120)],
                "auto_approved": [],
            }

        status = get_scheduler_status()

        self.assertTrue(status["run_state"]["result_compact"])
        self.assertEqual(len(status["run_state"]["result"]["results"]), 100)
        self.assertEqual(status["run_state"]["result"]["summary_counts"]["plan_count"], 120)

    def test_get_scheduler_status_can_return_full_run_state_when_requested(self):
        with _scheduler_running_lock:
            _scheduler_run_state["is_running"] = False
            _scheduler_run_state["mode"] = "execute"
            _scheduler_run_state["result"] = {
                "results": [{"symbol": f"{idx:06d}"} for idx in range(105)],
                "auto_approved": [],
            }

        status = get_scheduler_status(compact=False)

        self.assertNotIn("result_compact", status["run_state"])
        self.assertEqual(len(status["run_state"]["result"]["results"]), 105)

    @patch(
        "src.db.repository.load_ai_strategies",
        return_value=[{"id": "approved_ai", "selected": True, "status": "approved"}],
    )
    @patch("src.dashboard.threading.Thread")
    def test_trigger_scheduler_run_starts_background_thread(self, mock_thread, _mock_strategies):
        mock_thread_instance = MagicMock()
        mock_thread.return_value = mock_thread_instance

        response = trigger_scheduler_run(payload={"mode": "daily_auto"})
        self.assertEqual(response["status"], "started")
        self.assertEqual(response["mode"], "daily_auto")
        self.assertTrue(response["run_id"])
        self.assertEqual(_scheduler_run_state["run_id"], response["run_id"])
        self.assertTrue(_scheduler_run_state["is_running"])
        mock_thread.assert_called_once()
        mock_thread_instance.start.assert_called_once()

    @patch("src.db.repository.load_ai_strategies", return_value=[])
    def test_trigger_scheduler_run_rejects_missing_execution_target(self, _mock_strategies):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            trigger_scheduler_run(payload={"mode": "analysis_only"})

        self.assertEqual(ctx.exception.status_code, 409)

    @patch(
        "src.db.repository.load_ai_strategies",
        return_value=[
            {"id": "alpha", "selected": True, "status": "approved"},
            {"id": "beta", "selected": True, "status": "approved"},
        ],
    )
    @patch("src.dashboard.threading.Thread")
    def test_trigger_scheduler_run_accepts_multiple_strategies(self, mock_thread, _mock_strategies):
        mock_thread.return_value = MagicMock()

        response = trigger_scheduler_run(payload={
            "mode": "analysis_only",
            "strategy_ids": ["alpha", "beta", "alpha"],
        })

        self.assertEqual(response["strategy_ids"], ["alpha", "beta"])
        self.assertIsNone(response["strategy_id"])
        self.assertEqual(response["max_runtime_seconds"], 600)
        thread_args = mock_thread.call_args.kwargs["args"]
        self.assertEqual(thread_args[3], ["alpha", "beta"])

    @patch(
        "src.db.repository.load_ai_strategies",
        return_value=[{"id": "draft_ai", "selected": True, "status": "verified"}],
    )
    def test_trigger_scheduler_run_rejects_unapproved_strategy(self, _mock_strategies):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            trigger_scheduler_run(
                payload={"mode": "analysis_only", "strategy_ids": ["draft_ai"]}
            )

        self.assertEqual(ctx.exception.status_code, 409)

    @patch("src.dashboard.services.scheduler_service.PersistentRuntimeState")
    def test_dashboard_scheduler_service_forwards_strategy_ids(self, runtime_state):
        class FakeState(dict):
            def replace(self, payload):
                self.clear()
                self.update(payload)

        runtime_state.return_value = FakeState()
        service = DashboardSchedulerService("test", now_fn=lambda: "now")
        captured = {}

        def runner(**kwargs):
            captured.update(kwargs)
            return {"ok": True}

        service.run(
            runner,
            mode="analysis_only",
            include_ai_rebalance=True,
            auto_approve=False,
            strategy_ids=["alpha", "beta"],
            allowed_categories={"candidate"},
        )

        self.assertEqual(captured["mode"], "analysis_only")
        self.assertEqual(captured["strategy_ids"], ["alpha", "beta"])
        self.assertEqual(captured["allowed_categories"], {"candidate"})
        self.assertNotIn("force_strategy_id", captured)

    @patch("src.dashboard.core._run_scheduled_cycles_for_strategies")
    def test_multiple_strategy_runner_does_not_receive_force_strategy_id(
        self, runner
    ):
        runner.return_value = {"ok": True, "runs": [], "errors": []}

        _bg_run_multiple_scheduled_cycles(
            "daily_auto",
            False,
            True,
            ["alpha", "beta"],
            {"position", "candidate"},
        )

        runner.assert_called_once_with(
            mode="daily_auto",
            include_ai_rebalance=False,
            auto_approve=True,
            strategy_ids=["alpha", "beta"],
            allowed_categories={"position", "candidate"},
        )

    def test_trigger_scheduler_run_prevents_double_execution(self):
        with _scheduler_running_lock:
            _scheduler_run_state["is_running"] = True

        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            trigger_scheduler_run(payload={"mode": "daily_auto"})
        
        self.assertEqual(ctx.exception.status_code, 409)

if __name__ == "__main__":
    unittest.main()
