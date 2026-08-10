import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import scheduler, strategy_scheduler


class SchedulerModeTests(unittest.TestCase):
    def setUp(self):
        self.ai_strategy_lookup = patch(
            "src.db.repository.load_ai_strategies",
            return_value=[],
        )
        self.ai_strategy_lookup.start()
        self.approval_lookup = patch(
            "src.dashboard._approval_by_id",
            side_effect=lambda approval_id: {
                "id": approval_id,
                "status": "pending",
                "response_msg": "",
            },
        )
        self.approval_lookup.start()

    def tearDown(self):
        self.approval_lookup.stop()
        self.ai_strategy_lookup.stop()

    def test_daily_auto_result_uses_dashboard_path_with_active_isolated_strategy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "daily_auto_last_result.json"
            with patch.dict(
                "os.environ",
                {"HANSTOCK_SCHEDULER_RESULT_PATH": str(result_path)},
            ):
                scheduler._write_cycle_result(
                    {"plan": [{"symbol": "005930"}]},
                    mode="daily_auto",
                    strategy_id="heikin_ashi_scalping_strategy",
                )

            self.assertTrue(result_path.exists())
            self.assertFalse(
                (Path(temp_dir) / "heikin_ashi_scalping_last_result.json").exists()
            )

    def test_strategy_dispatch_limits_isolated_strategy_to_candidate_orders(self):
        schedule = {
            "strategy_id": "plunge_bounce_strategy",
            "mode": "execute",
            "auto_approve": True,
        }

        with patch.object(strategy_scheduler, "list_strategy_schedules", return_value=[schedule]), \
                patch.object(strategy_scheduler, "is_schedule_due", return_value=True), \
                patch.object(strategy_scheduler, "run_scheduled_cycle") as cycle_mock, \
                patch.object(strategy_scheduler, "mark_strategy_schedule_run") as mark_mock:
            ran = strategy_scheduler.dispatch_due_schedules()

        self.assertEqual(ran, ["plunge_bounce_strategy"])
        cycle_mock.assert_called_once_with(
            "execute",
            auto_approve=True,
            force_strategy_id="plunge_bounce_strategy",
            allowed_categories={"candidate"},
            persist_result=False,
        )
        mark_mock.assert_called_once_with("plunge_bounce_strategy")

    def test_strategy_dispatch_runs_narrative_momentum_cycle(self):
        schedule = {
            "strategy_id": "narrative_momentum_strategy",
            "mode": "execute",
            "auto_approve": False,
        }
        expected = {"strategy_id": "narrative_momentum_strategy", "summary": {"candidate_count": 2}}

        with patch.object(strategy_scheduler, "list_strategy_schedules", return_value=[schedule]), \
                patch.object(strategy_scheduler, "is_schedule_due", return_value=True), \
                patch.object(strategy_scheduler, "run_narrative_momentum_cycle", return_value=expected) as run_mock, \
                patch.object(strategy_scheduler, "save_scheduler_result") as save_mock, \
                patch.object(strategy_scheduler, "mark_strategy_schedule_run") as mark_mock:
            ran = strategy_scheduler.dispatch_due_schedules()

        self.assertEqual(ran, ["narrative_momentum_strategy"])
        run_mock.assert_called_once_with(save_candidates=True, auto_collect=True)
        save_mock.assert_called_once()
        self.assertEqual(save_mock.call_args.args[0], "execute")
        self.assertEqual(save_mock.call_args.args[2], expected)
        mark_mock.assert_called_once_with("narrative_momentum_strategy")

    def test_strategy_dispatch_ignores_direct_ai_strategy_schedule(self):
        schedule = {
            "strategy_id": "main_ai_strategy",
            "market": "KR",
            "mode": "execute",
            "auto_approve": False,
        }
        with patch.object(
            strategy_scheduler,
            "list_strategy_schedules",
            return_value=[schedule],
        ), patch.object(
            strategy_scheduler, "is_schedule_due", return_value=True
        ), patch(
            "src.db.repository.load_ai_strategies",
            return_value=[{"id": "main_ai_strategy"}],
        ), patch(
            "src.ai_stock.automation_service.run_strategy",
            return_value={"automation": {}},
        ) as autonomy_run, patch.object(
            strategy_scheduler, "save_scheduler_result"
        ), patch.object(
            strategy_scheduler, "mark_strategy_schedule_run"
        ):
            ran = strategy_scheduler.dispatch_due_schedules()

        self.assertEqual(ran, [])
        autonomy_run.assert_not_called()

    def test_ai_schedule_slot_runs_strategy_applied_in_ai_tab(self):
        schedule = {
            "strategy_id": "ai_stock_default_v1",
            "market": "KR",
            "mode": "execute",
            "auto_approve": True,
        }
        applied = {
            "id": "easy_aggressive_live",
            "selected": True,
            "status": "approved",
        }
        with patch.object(
            strategy_scheduler,
            "list_strategy_schedules",
            return_value=[schedule],
        ), patch.object(
            strategy_scheduler, "is_schedule_due", return_value=True
        ), patch(
            "src.db.repository.load_ai_strategies",
            return_value=[applied],
        ), patch(
            "src.ai_stock.automation_service.run_strategy",
            return_value={"automation": {}},
        ) as autonomy_run, patch.object(
            strategy_scheduler, "save_scheduler_result"
        ), patch.object(
            strategy_scheduler, "mark_strategy_schedule_run"
        ) as mark:
            ran = strategy_scheduler.dispatch_due_schedules()

        self.assertEqual(ran, ["easy_aggressive_live"])
        autonomy_run.assert_called_once_with(
            market="KR",
            strategy_id="easy_aggressive_live",
            run_type="scheduled",
        )
        mark.assert_called_once_with("ai_stock_default_v1")

    def test_ai_schedule_slot_reports_policy_block_in_saved_result(self):
        schedule = {
            "strategy_id": "ai_stock_default_v1",
            "market": "KR",
            "mode": "execute",
            "auto_approve": True,
        }
        applied = {
            "id": "easy_aggressive_live",
            "selected": True,
            "status": "approved",
        }
        with patch.object(
            strategy_scheduler, "list_strategy_schedules", return_value=[schedule]
        ), patch.object(
            strategy_scheduler, "is_schedule_due", return_value=True
        ), patch(
            "src.db.repository.load_ai_strategies", return_value=[applied]
        ), patch(
            "src.ai_stock.automation_service.run_strategy",
            return_value={"automation": {"blocked": ["quote is stale"]}},
        ), patch(
            "src.ai_stock.realtime_service.run_realtime_cycle", return_value={}
        ), patch.object(
            strategy_scheduler, "save_scheduler_result"
        ) as save_result, patch.object(
            strategy_scheduler, "mark_strategy_schedule_run"
        ) as mark:
            ran = strategy_scheduler.dispatch_due_schedules()

        self.assertEqual(ran, ["easy_aggressive_live"])
        saved = save_result.call_args.args[2]
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["blocked"], ["KR:quote is stale"])
        mark.assert_called_once_with("ai_stock_default_v1")

    def test_ai_schedule_slot_does_not_duplicate_explicit_schedule(self):
        from src.strategy_ids import resolve_ai_schedule_strategy_ids

        resolved = resolve_ai_schedule_strategy_ids(
            [
                "seven_split",
                "ai_stock_default_v1",
                "issue_sector_rotation_strategy",
            ],
            strategies=[
                {
                    "id": "issue_sector_rotation_strategy",
                    "selected": True,
                    "status": "approved",
                },
                {
                    "id": "easy_safe_live",
                    "selected": True,
                    "status": "approved",
                },
            ],
        )

        self.assertEqual(
            resolved,
            [
                "seven_split",
                "issue_sector_rotation_strategy",
                "easy_safe_live",
            ],
        )

    def test_ai_schedule_slot_includes_selected_strategy_without_validation_gate(self):
        from src.strategy_ids import resolve_ai_schedule_strategy_ids

        resolved = resolve_ai_schedule_strategy_ids(
            ["ai_stock_default_v1"],
            strategies=[
                {"id": "paper_only", "selected": True, "status": "paper_passed"},
                {"id": "ready", "selected": True, "status": "approved"},
                {"id": "plunge_bounce_strategy", "selected": True, "status": "approved"},
                {"id": "unchecked", "selected": False, "status": "approved"},
            ],
        )

        self.assertEqual(resolved, ["paper_only", "ready"])

    def test_strategy_dispatch_ignores_legacy_issue_sector_schedule(self):
        schedule = {
            "strategy_id": "issue_sector_rotation_strategy",
            "market": "KR",
            "mode": "execute",
            "auto_approve": True,
        }
        expected = {"strategy_id": "issue_sector_rotation_strategy", "results": []}
        with patch.object(
            strategy_scheduler,
            "list_strategy_schedules",
            return_value=[schedule],
        ), patch.object(
            strategy_scheduler, "is_schedule_due", return_value=True
        ), patch(
            "src.db.repository.load_ai_strategies",
            return_value=[{"id": "issue_sector_rotation_strategy"}],
        ), patch.object(
            strategy_scheduler,
            "run_scheduled_cycle",
            return_value=expected,
        ) as run_cycle, patch(
            "src.ai_stock.automation_service.run_strategy"
        ) as autonomy_run, patch.object(
            strategy_scheduler, "mark_strategy_schedule_run"
        ):
            ran = strategy_scheduler.dispatch_due_schedules()

        self.assertEqual(ran, [])
        run_cycle.assert_not_called()
        autonomy_run.assert_not_called()

    def test_strategy_dispatch_runs_only_independent_strategy_schedules(self):
        schedules = [
            {"strategy_id": "issue_sector_rotation_strategy", "mode": "execute", "auto_approve": True},
            {"strategy_id": "seven_split", "mode": "execute", "auto_approve": True},
            {"strategy_id": "plunge_bounce_strategy", "mode": "execute", "auto_approve": True},
            {"strategy_id": "heikin_ashi_scalping_strategy", "mode": "execute", "auto_approve": True},
        ]
        expected = {"strategy_id": "seven_split", "results": []}
        with patch.object(
            strategy_scheduler,
            "list_strategy_schedules",
            return_value=schedules,
        ), patch.object(
            strategy_scheduler, "is_schedule_due", return_value=True
        ), patch.object(
            strategy_scheduler,
            "run_scheduled_cycle",
            return_value=expected,
        ) as run_cycle, patch.object(
            strategy_scheduler,
            "mark_strategy_schedule_run",
        ) as mark_mock:
            ran = strategy_scheduler.dispatch_due_schedules()

        self.assertEqual(
            ran,
            ["plunge_bounce_strategy", "heikin_ashi_scalping_strategy"],
        )
        self.assertEqual(run_cycle.call_count, 2)
        self.assertEqual(
            [call.kwargs["force_strategy_id"] for call in run_cycle.call_args_list],
            ["plunge_bounce_strategy", "heikin_ashi_scalping_strategy"],
        )
        self.assertEqual(
            [call.args[0] for call in mark_mock.call_args_list],
            ["plunge_bounce_strategy", "heikin_ashi_scalping_strategy"],
        )

    def test_strategy_dispatch_reuses_one_pre_order_sync_for_independent_strategies(self):
        schedules = [
            {"strategy_id": "plunge_bounce_strategy", "mode": "execute", "auto_approve": True},
            {"strategy_id": "heikin_ashi_scalping_strategy", "mode": "execute", "auto_approve": True},
        ]
        shared_sync = {"ok": True, "updated_count": 2}
        with patch.object(
            strategy_scheduler, "list_strategy_schedules", return_value=schedules
        ), patch.object(
            strategy_scheduler, "is_schedule_due", return_value=True
        ), patch.object(
            strategy_scheduler, "_sync_order_status_before_cycle", return_value=shared_sync
        ) as sync_mock, patch.object(
            strategy_scheduler, "run_scheduled_cycle", return_value={"results": []}
        ) as run_cycle, patch.object(
            strategy_scheduler, "mark_strategy_schedule_run"
        ):
            ran = strategy_scheduler.dispatch_due_schedules()

        self.assertEqual(
            ran,
            ["plunge_bounce_strategy", "heikin_ashi_scalping_strategy"],
        )
        sync_mock.assert_called_once_with()
        self.assertEqual(run_cycle.call_count, 2)
        self.assertTrue(all(
            call.kwargs["pre_order_status_sync"] is shared_sync
            for call in run_cycle.call_args_list
        ))

    def test_run_scheduled_cycle_uses_supplied_pre_order_sync_without_fetching_again(self):
        shared_sync = {"ok": True, "updated_count": 4}
        with patch.object(
            scheduler, "_sync_order_status_before_cycle"
        ) as sync_mock, patch.object(
            scheduler.trader, "run", return_value={"results": []}
        ):
            result = scheduler.run_scheduled_cycle(
                mode="execute",
                pre_order_status_sync=shared_sync,
            )

        sync_mock.assert_not_called()
        self.assertIs(result["pre_order_status_sync"], shared_sync)

    def test_run_scheduled_cycle_delegates_execute_mode(self):
        expected = {"mode": "execute", "results": []}

        with patch.object(scheduler.trader, "run", return_value=expected) as run_mock:
            result = scheduler.run_scheduled_cycle(mode="execute")

        self.assertEqual(result, expected)
        run_mock.assert_called_once_with(mode="execute")

    def test_run_scheduled_cycle_delegates_analysis_only_mode(self):
        expected = {"mode": "analysis_only", "results": []}

        with patch.object(scheduler.trader, "run", return_value=expected) as run_mock:
            result = scheduler.run_scheduled_cycle(mode="analysis_only")

        self.assertEqual(result, expected)
        run_mock.assert_called_once_with(mode="analysis_only")

    def test_main_uses_default_execute_mode(self):
        with patch.object(sys, "argv", ["scheduler"]), patch.object(
            scheduler, "run_scheduled_cycle"
        ) as cycle_mock:
            exit_code = scheduler.main()

        self.assertEqual(exit_code, 0)
        cycle_mock.assert_called_once_with(
            mode="execute",
            include_ai_rebalance=False,
            auto_approve=False,
        )

    def test_main_accepts_execute_mode_argument(self):
        with patch.object(sys, "argv", ["scheduler", "--mode", "execute"]), patch.object(
            scheduler, "run_scheduled_cycle"
        ) as cycle_mock:
            exit_code = scheduler.main()

        self.assertEqual(exit_code, 0)
        cycle_mock.assert_called_once_with(
            mode="execute",
            include_ai_rebalance=False,
            auto_approve=False,
        )

    def test_main_accepts_analysis_only_mode_argument(self):
        with patch.object(
            sys, "argv", ["scheduler", "--mode", "analysis_only"]
        ), patch.object(scheduler, "run_scheduled_cycle") as cycle_mock:
            exit_code = scheduler.main()

        self.assertEqual(exit_code, 0)
        cycle_mock.assert_called_once_with(
            mode="analysis_only",
            include_ai_rebalance=False,
            auto_approve=False,
        )

    def test_daily_auto_runs_analysis_with_ai_rebalance_and_approves_only_ai(self):
        expected = {
            "results": [
                {"approval_id": 123, "category": "ai_rebalance"},
                {"approval_id": 456, "category": "candidate"},
                {"decision": "skip"},
            ]
        }

        with patch.object(scheduler.trader, "run", return_value=expected) as run_mock, patch(
            "src.dashboard._approve_pending_approval",
            return_value={"id": 123, "status": "executed"},
        ) as approve_mock, patch.object(scheduler.time, "sleep") as sleep_mock:
            result = scheduler.run_scheduled_cycle(mode="daily_auto")

        run_mock.assert_called_once_with(
            mode="analysis_only",
            include_ai_rebalance=True,
            execution_categories={"ai_rebalance"},
        )
        approve_mock.assert_called_once_with(123, "scheduled auto approval")
        sleep_mock.assert_called_once()
        self.assertEqual(result["auto_approved"], [{"id": 123, "status": "executed"}])
        self.assertEqual(result["auto_approval_errors"], [])

    def test_daily_auto_syncs_order_status_after_auto_approval(self):
        expected = {
            "results": [
                {"approval_id": 123, "category": "ai_rebalance"},
            ]
        }

        with patch.object(scheduler.trader, "run", return_value=expected), patch(
            "src.dashboard._approve_pending_approval",
            return_value={"id": 123, "status": "executed"},
        ), patch.object(scheduler.trader.config, "dry_run", False), patch(
            "src.dashboard._get_api", return_value=object()
        ) as get_api, patch(
            "src.dashboard._sync_order_status_from_history",
            return_value={"ok": True, "updated_count": 1},
        ) as sync_status, patch.object(scheduler.time, "sleep"), patch.object(
            scheduler, "_write_cycle_result"
        ):
            result = scheduler.run_scheduled_cycle(mode="daily_auto")

        self.assertEqual(get_api.call_count, 2)
        self.assertEqual(sync_status.call_count, 2)
        self.assertEqual(result["pre_order_status_sync"]["updated_count"], 1)
        self.assertEqual(result["order_status_sync"]["updated_count"], 1)

    def test_execute_syncs_stale_orders_before_building_new_plan(self):
        with patch.object(
            scheduler.trader, "run", return_value={"results": []}
        ), patch.object(scheduler.trader.config, "dry_run", False), patch(
            "src.dashboard._get_api", return_value=object()
        ), patch(
            "src.dashboard._sync_order_status_from_history",
            return_value={"ok": True, "updated_count": 3},
        ) as sync_status:
            result = scheduler.run_scheduled_cycle(mode="execute")

        sync_status.assert_called_once()
        self.assertEqual(result["pre_order_status_sync"]["updated_count"], 3)
        self.assertNotIn("order_status_sync", result)

    def test_daily_auto_treats_already_processed_approval_as_done(self):
        expected = {
            "results": [
                {"approval_id": 123, "category": "ai_rebalance"},
            ]
        }

        with patch.object(scheduler.trader, "run", return_value=expected), patch(
            "src.dashboard._approval_by_id",
            return_value={"id": 123, "status": "executed", "response_msg": "already submitted"},
        ), patch(
            "src.dashboard._approve_pending_approval",
        ) as approve_mock, patch.object(scheduler.time, "sleep"), patch.object(
            scheduler, "_write_cycle_result"
        ):
            result = scheduler.run_scheduled_cycle(mode="daily_auto")

        approve_mock.assert_not_called()
        self.assertEqual(result["auto_approval_errors"], [])
        self.assertEqual(result["auto_approved"], [{
            "id": 123,
            "status": "executed",
            "response_msg": "already submitted",
            "already_processed": True,
        }])

    def test_daily_auto_treats_raced_already_executing_approval_as_done(self):
        expected = {
            "results": [
                {"approval_id": 123, "category": "ai_rebalance"},
            ]
        }

        with patch.object(scheduler.trader, "run", return_value=expected), patch(
            "src.dashboard._approval_by_id",
            side_effect=[
                {"id": 123, "status": "pending", "response_msg": ""},
                {"id": 123, "status": "executing", "response_msg": "Submitting order to broker"},
            ],
        ), patch(
            "src.dashboard._approve_pending_approval",
            side_effect=RuntimeError("409: approval is already executing"),
        ), patch.object(scheduler.time, "sleep"), patch.object(
            scheduler, "_write_cycle_result"
        ):
            result = scheduler.run_scheduled_cycle(mode="daily_auto")

        self.assertEqual(result["auto_approval_errors"], [])
        self.assertEqual(result["auto_approved"], [{
            "id": 123,
            "status": "executing",
            "response_msg": "Submitting order to broker",
            "already_processed": True,
        }])

    def test_order_status_sync_failure_is_recorded_without_failing_cycle(self):
        expected = {
            "results": [
                {"approval_id": 123, "category": "ai_rebalance"},
            ]
        }

        with patch.object(scheduler.trader, "run", return_value=expected), patch(
            "src.dashboard._approve_pending_approval",
            return_value={"id": 123, "status": "executed"},
        ), patch.object(scheduler.trader.config, "dry_run", False), patch(
            "src.dashboard._get_api", return_value=object()
        ), patch(
            "src.dashboard._sync_order_status_from_history",
            side_effect=RuntimeError("history unavailable"),
        ), patch.object(scheduler.time, "sleep"), patch.object(
            scheduler, "_write_cycle_result"
        ):
            result = scheduler.run_scheduled_cycle(mode="daily_auto")

        self.assertEqual(result["auto_approved"], [{"id": 123, "status": "executed"}])
        self.assertEqual(result["order_status_sync_error"]["message"], "history unavailable")

    def test_daily_auto_retries_trader_run_after_transient_failure(self):
        expected = {"results": []}

        with patch.object(
            scheduler.trader,
            "run",
            side_effect=[RuntimeError("temporary"), expected],
        ) as run_mock, patch.object(scheduler.time, "sleep") as sleep_mock, patch.object(
            scheduler, "_write_cycle_result"
        ):
            result = scheduler.run_scheduled_cycle(mode="daily_auto")

        self.assertEqual(result["results"], [])
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["retry_errors"][0]["message"], "temporary")
        self.assertEqual(run_mock.call_count, 2)
        sleep_mock.assert_called_once()

    def test_daily_auto_returns_failed_result_after_retries_exhausted(self):
        with patch.object(
            scheduler.trader,
            "run",
            side_effect=RuntimeError("network down"),
        ) as run_mock, patch.object(scheduler.time, "sleep"), patch.object(
            scheduler, "_write_cycle_result"
        ):
            result = scheduler.run_scheduled_cycle(mode="daily_auto")

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["errors"]), 3)
        self.assertEqual(run_mock.call_count, 3)
        self.assertEqual(result["auto_approved"], [])

    def test_approval_failure_is_recorded_without_stopping_other_approvals(self):
        expected = {
            "results": [
                {"approval_id": 123, "category": "ai_rebalance"},
                {"approval_id": 124, "category": "ai_rebalance"},
            ]
        }

        with patch.object(scheduler.trader, "run", return_value=expected), patch(
            "src.dashboard._approve_pending_approval",
            side_effect=[
                RuntimeError("broker busy"),
                RuntimeError("broker busy"),
                {"id": 124, "status": "executed"},
            ],
        ) as approve_mock, patch.object(scheduler.time, "sleep"), patch.object(
            scheduler, "_write_cycle_result"
        ):
            result = scheduler.run_scheduled_cycle(mode="daily_auto")

        self.assertEqual(result["auto_approved"], [{"id": 124, "status": "executed"}])
        self.assertEqual(result["auto_approval_errors"][0]["approval_id"], 123)
        self.assertEqual(result["auto_approval_errors"][0]["message"], "broker busy")
        self.assertEqual(approve_mock.call_count, 3)

    def test_daily_auto_sends_single_slack_result_summary(self):
        expected = {
            "plan": [{"category": "ai_rebalance"}],
            "results": [{"approval_id": 123, "category": "ai_rebalance", "decision": "queue"}],
        }

        with patch.object(scheduler.trader, "run", return_value=expected), patch(
            "src.dashboard._approve_pending_approval",
            return_value={"id": 123, "status": "executed"},
        ), patch.object(scheduler.time, "sleep"), patch.object(
            scheduler, "_write_cycle_result"
        ), patch.object(scheduler, "send_slack") as send_slack:
            scheduler.run_scheduled_cycle(mode="daily_auto")

        self.assertEqual(send_slack.call_count, 1)
        self.assertIn("정상 완료", send_slack.call_args_list[0].kwargs["text"])

    def test_daily_auto_slack_summary_counts_only_unprocessed_queue(self):
        expected = {
            "plan": [{}, {}, {}],
            "results": [
                {"approval_id": 123, "category": "ai_rebalance", "decision": "queue"},
                {"approval_id": 124, "category": "ai_rebalance", "decision": "queue"},
                {"approval_id": 125, "category": "ai_rebalance", "decision": "queue"},
            ],
        }

        with patch.object(scheduler.trader, "run", return_value=expected), patch(
            "src.dashboard._approve_pending_approval",
            side_effect=[
                {"id": 123, "status": "executed"},
                {"id": 124, "status": "executed"},
                {"id": 125, "status": "executed"},
            ],
        ), patch.object(scheduler.time, "sleep"), patch.object(
            scheduler, "_write_cycle_result"
        ), patch.object(scheduler, "send_slack") as send_slack:
            scheduler.run_scheduled_cycle(mode="daily_auto")

        text = send_slack.call_args_list[-1].kwargs["blocks"][0]["text"]["text"]
        self.assertIn("계획/승인대기/완료: 3 / 0 / 3", text)

    def test_daily_auto_slack_summary_marks_approval_errors(self):
        expected = {"results": [{"approval_id": 123, "category": "ai_rebalance"}]}

        with patch.object(scheduler.trader, "run", return_value=expected), patch(
            "src.dashboard._approve_pending_approval",
            side_effect=RuntimeError("broker busy"),
        ), patch.object(scheduler.time, "sleep"), patch.object(
            scheduler, "_write_cycle_result"
        ), patch.object(scheduler, "send_slack") as send_slack:
            result = scheduler.run_scheduled_cycle(mode="daily_auto")

        self.assertEqual(len(result["auto_approval_errors"]), 2)
        self.assertIn("문제 발생", send_slack.call_args_list[-1].kwargs["text"])

    def test_main_rejects_invalid_mode(self):
        with patch.object(sys, "argv", ["scheduler", "--mode", "invalid"]), patch.object(
            scheduler, "run_scheduled_cycle"
        ) as cycle_mock:
            with self.assertRaises(SystemExit) as exc:
                scheduler.main()

        self.assertEqual(exc.exception.code, 2)
        cycle_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
