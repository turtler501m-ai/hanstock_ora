# -*- coding: utf-8 -*-
"""AI스톡 자동화 테스트 (§5.12·§6.6): 정책 게이트·스케줄 진입점."""
import unittest
from unittest.mock import patch

from src.db.repository import init_db, connect_db
from src.db import ai_stock_repository as repo
from src.ai_stock import market_data, automation_service
from src.ai_stock.automation_service import evaluate_gate, set_policy, run_strategy
from src.ai_stock.freshness import now

_AI_TABLES = [
    "ai_stock_timing_signals", "ai_stock_execution_runs", "ai_stock_execution_plans",
    "ai_stock_performance", "ai_stock_watch_events", "ai_stock_watchlist",
    "ai_stock_candidates", "ai_stock_automation_policies", "ai_stock_scans",
]


def _uptrend(n=40):
    return [100.0 + i for i in range(n)]


class _Provider:
    def universe_items(self, market):
        return [{"symbol": "005930", "name": "삼성", "instrument_type": "stock"}]

    def quote(self, market, symbol):
        return {"price": 139.0}

    def daily_series(self, market, symbol):
        return _uptrend()

    def index_series(self, market):
        return {"KOSPI": _uptrend(), "S&P500": _uptrend()}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with connect_db() as conn:
            for t in _AI_TABLES:
                conn.execute(f"DELETE FROM {t}")
            conn.commit()
        self._orig = market_data.get_provider()
        market_data.set_provider(_Provider())

    def tearDown(self):
        market_data.set_provider(self._orig)

    def test_execute_gate_blocked_when_guarded(self):
        # 안전 가드가 닫힌 기본 환경에서 execute 단계는 차단된다.
        orig = automation_service.live_trading_allowed
        automation_service.live_trading_allowed = lambda: False
        try:
            pol = {"automation_level": 6, "auto_execute": 1, "require_paper_passed": 1}
            cand = {"final_score": 90, "rule_score": 80, "risk_score": 10, "data_as_of": now().isoformat()}
            gate = evaluate_gate(policy=pol, candidate=cand, stage="execute")
            self.assertFalse(gate["proceed"])
            self.assertTrue(len(gate["blocked_reason"]) > 0)
            self.assertNotIn("paper_passed_required", gate["blocked_reason"])
        finally:
            automation_service.live_trading_allowed = orig

    def test_execute_gate_ignores_legacy_paper_policy(self):
        orig = automation_service.live_trading_allowed
        automation_service.live_trading_allowed = lambda: True
        try:
            pol = {"automation_level": 6, "auto_execute": 1, "require_paper_passed": 1}
            cand = {"final_score": 90, "rule_score": 80, "risk_score": 10, "data_as_of": now().isoformat()}
            gate = evaluate_gate(policy=pol, candidate=cand, stage="execute")
            self.assertTrue(gate["proceed"])
            self.assertNotIn("paper_passed_required", gate["blocked_reason"])
        finally:
            automation_service.live_trading_allowed = orig

    def test_execute_gate_blocks_unapproved_strategy(self):
        from src.db.strategy_repository import save_ai_strategies

        save_ai_strategies([{
            "id": "ai_stock_draft",
            "name": "Draft",
            "model": "gpt-5-mini",
            "status": "draft",
            "profile": {},
            "strategy_version": 1,
        }])
        orig = automation_service.live_trading_allowed
        automation_service.live_trading_allowed = lambda: True
        try:
            pol = {"automation_level": 6, "auto_execute": 1}
            cand = {
                "strategy_id": "ai_stock_draft",
                "final_score": 90,
                "rule_score": 80,
                "risk_score": 10,
                "data_as_of": now().isoformat(),
            }
            gate = evaluate_gate(policy=pol, candidate=cand, stage="execute")
            self.assertFalse(gate["proceed"])
            self.assertIn("strategy_not_approved", gate["blocked_reason"])
        finally:
            automation_service.live_trading_allowed = orig

    def test_execute_gate_blocks_max_daily_orders(self):
        repo.log_execution_run({
            "strategy_id": "ai_stock_default_v1",
            "market": "KR",
            "candidate_id": 1,
            "run_type": "manual",
            "automation_level": 6,
            "status": "completed",
            "started_at": now().isoformat(),
        })
        orig = automation_service.live_trading_allowed
        automation_service.live_trading_allowed = lambda: True
        try:
            pol = {"automation_level": 6, "auto_execute": 1, "max_daily_orders": 1}
            cand = {
                "market": "KR",
                "strategy_id": "ai_stock_default_v1",
                "final_score": 90,
                "rule_score": 80,
                "risk_score": 10,
                "data_as_of": now().isoformat(),
            }
            gate = evaluate_gate(policy=pol, candidate=cand, stage="execute")
            self.assertFalse(gate["proceed"])
            self.assertIn("max_daily_orders", gate["blocked_reason"])
        finally:
            automation_service.live_trading_allowed = orig

    def test_execute_gate_blocks_stale_candidate_by_default(self):
        orig = automation_service.live_trading_allowed
        automation_service.live_trading_allowed = lambda: True
        try:
            pol = {"automation_level": 6, "auto_execute": 1}
            cand = {"final_score": 90, "rule_score": 80, "risk_score": 10}
            gate = evaluate_gate(policy=pol, candidate=cand, stage="execute")
            self.assertFalse(gate["proceed"])
            self.assertIn("stale_data", gate["blocked_reason"])
        finally:
            automation_service.live_trading_allowed = orig

    def test_set_policy_forces_off_execute_when_guarded(self):
        orig = automation_service.live_trading_allowed
        automation_service.live_trading_allowed = lambda: False
        try:
            pol = set_policy("ai_stock_default_v1", "KR", {"automation_level": 6, "auto_execute": 1})
            # 환경 가드가 닫혀 있으면 auto_execute는 강제로 0
            self.assertEqual(pol["auto_execute"], 0)
        finally:
            automation_service.live_trading_allowed = orig

    def test_set_policy_normalizes_level_and_auto_flags(self):
        pol = set_policy("ai_stock_default_v1", "KR", {
            "automation_level": 4,
            "auto_approve": True,
            "auto_execute": True,
            "enabled": "true",
            "allow_stale_data_trade": "true",
        })
        self.assertEqual(pol["automation_level"], 4)
        self.assertEqual(pol["enabled"], 1)
        self.assertEqual(pol["auto_approve"], 0)
        self.assertEqual(pol["auto_execute"], 0)
        self.assertEqual(pol["allow_stale_data_trade"], 1)

        pol2 = set_policy("ai_stock_default_v1", "KR", {"automation_level": 99})
        self.assertEqual(pol2["automation_level"], 6)

    def test_run_strategy_level4_plans_but_no_order(self):
        set_policy("ai_stock_default_v1", "KR", {"automation_level": 4})
        result = run_strategy(market="KR", strategy_id="ai_stock_default_v1")
        self.assertIn("automation", result)
        # 실행 이력이 기록되고 주문은 발생하지 않는다(계획까지만).
        runs = repo.list_execution_runs(market="KR")
        self.assertTrue(runs)

    def test_run_strategy_level1_no_watchlist(self):
        set_policy("ai_stock_default_v1", "KR", {"automation_level": 1})
        result = run_strategy(market="KR", strategy_id="ai_stock_default_v1")
        self.assertEqual(result["automation"]["registered"], 0)

    def test_run_strategy_routes_enabled_autonomy_to_managed_approvals(self):
        autonomy_result = {
            "enabled": True,
            "cycle_key": "cycle-1",
            "managed_orders": [{"id": 11}],
            "approvals": [{"order_id": 11, "approval_id": 22}],
        }
        with patch(
            "src.ai_stock.discovery_service.run_scan",
            return_value={"scan_id": 999, "summary": {"status": "completed"}},
        ), patch.object(
            automation_service.repo,
            "get_policy",
            return_value={"enabled": 1, "automation_level": 6},
        ), patch.object(
            automation_service.repo, "log_execution_run"
        ) as log_run, patch(
            "src.config.config.autonomy_enabled", True
        ), patch(
            "src.strategy.autonomy.ai_stock_integration.run_ai_stock_autonomy_cycle",
            return_value=autonomy_result,
        ) as autonomy_run:
            result = run_strategy(
                market="KR",
                strategy_id="ai_stock_default_v1",
                run_type="scheduled",
            )

        self.assertEqual(result["autonomy"], autonomy_result)
        self.assertEqual(result["automation"]["planned"], 1)
        self.assertEqual(result["automation"]["approved"], 1)
        autonomy_run.assert_called_once_with(
            market="KR",
            strategy_id="ai_stock_default_v1",
            scan_id=999,
            run_type="scheduled",
        )
        self.assertEqual(
            log_run.call_args.args[0]["policy_snapshot"]["execution_path"],
            "autonomy",
        )

    def test_level5_updates_execution_plan_approval_fields(self):
        from src.ai_stock import discovery_service, watchlist_service, execution_plan_service

        orig_scan = discovery_service.run_scan
        orig_list_candidates = repo.list_candidates
        orig_register = watchlist_service.register
        orig_transition = watchlist_service.transition
        orig_plan = execution_plan_service.create_plan
        orig_queue = automation_service._queue_approval
        orig_update = repo.update_execution_plan_approval
        updates = []
        discovery_service.run_scan = lambda **kwargs: {"scan_id": 999, "summary": {"status": "completed"}}
        repo.list_candidates = lambda **kwargs: [{
            "candidate_id": 1, "market": "KR", "symbol": "005930", "name": "Samsung",
            "current_price": 100, "decision": "watch", "final_score": 90, "rule_score": 80,
            "risk_score": 10, "data_as_of": now().isoformat(),
        }]
        watchlist_service.register = lambda cand: {"ok": True}
        watchlist_service.transition = lambda *args, **kwargs: {"ok": True}
        execution_plan_service.create_plan = lambda *args, **kwargs: {"id": 7, "quantity": 1, "entry_price": 100}
        automation_service._queue_approval = lambda *args, **kwargs: 123
        repo.update_execution_plan_approval = lambda *args, **kwargs: updates.append((args, kwargs))
        try:
            set_policy("ai_stock_default_v1", "KR", {
                "automation_level": 5,
                "auto_approve": 1,
                "min_final_score": 0,
                "min_rule_score": 0,
                "max_risk_score": 100,
            })
            result = run_strategy(market="KR", strategy_id="ai_stock_default_v1")
            self.assertEqual(result["automation"]["approved"], 1)
            self.assertEqual(updates[0][0][0], 7)
            self.assertEqual(updates[0][1]["approval_id"], 123)
            self.assertEqual(updates[0][1]["approval_status"], "pending")
        finally:
            discovery_service.run_scan = orig_scan
            repo.list_candidates = orig_list_candidates
            watchlist_service.register = orig_register
            watchlist_service.transition = orig_transition
            execution_plan_service.create_plan = orig_plan
            automation_service._queue_approval = orig_queue
            repo.update_execution_plan_approval = orig_update

    def test_level6_legacy_path_cannot_execute_even_when_hook_claims_success(self):
        from src.ai_stock import discovery_service, watchlist_service, execution_plan_service

        orig_guard = automation_service.live_trading_allowed
        orig_execute = automation_service._execute_order
        orig_scan = discovery_service.run_scan
        orig_list_candidates = repo.list_candidates
        orig_register = watchlist_service.register
        orig_transition = watchlist_service.transition
        orig_plan = execution_plan_service.create_plan
        orig_update_status = repo.update_execution_plan_status
        automation_service.live_trading_allowed = lambda: True
        calls = []
        status_updates = []
        automation_service._execute_order = lambda *args, **kwargs: calls.append(args) or {"ok": True, "status": "submitted"}
        discovery_service.run_scan = lambda **kwargs: {"scan_id": 999, "summary": {"status": "completed"}}
        repo.list_candidates = lambda **kwargs: [{
            "candidate_id": 1, "market": "KR", "symbol": "005930", "name": "삼성",
            "current_price": 100, "decision": "watch", "final_score": 90, "rule_score": 80, "risk_score": 10,
            "data_as_of": now().isoformat(),
        }]
        watchlist_service.register = lambda cand: {"ok": True}
        watchlist_service.transition = lambda *args, **kwargs: {"ok": True}
        execution_plan_service.create_plan = lambda *args, **kwargs: {"id": 7, "quantity": 1, "entry_price": 100}
        repo.update_execution_plan_status = lambda *args, **kwargs: status_updates.append((args, kwargs))
        try:
            set_policy("ai_stock_default_v1", "KR", {
                "automation_level": 6,
                "auto_approve": 0,
                "auto_execute": 1,
                "min_final_score": 0,
                "min_rule_score": 0,
                "max_risk_score": 100,
            })
            result = run_strategy(market="KR", strategy_id="ai_stock_default_v1")
            self.assertEqual(result["automation"].get("executed", 0), 0)
            self.assertEqual(calls, [])
            self.assertEqual(status_updates[0][0][0], 7)
            self.assertEqual(status_updates[0][1]["status"], "blocked")
            self.assertTrue(any(
                "legacy_auto_execute_disabled_use_autonomy_runtime" in item
                for item in result["automation"]["blocked"]
            ))
        finally:
            automation_service.live_trading_allowed = orig_guard
            automation_service._execute_order = orig_execute
            discovery_service.run_scan = orig_scan
            repo.list_candidates = orig_list_candidates
            watchlist_service.register = orig_register
            watchlist_service.transition = orig_transition
            execution_plan_service.create_plan = orig_plan
            repo.update_execution_plan_status = orig_update_status

    def test_level6_does_not_count_failed_order_as_executed(self):
        from src.ai_stock import discovery_service, watchlist_service, execution_plan_service

        orig_guard = automation_service.live_trading_allowed
        orig_execute = automation_service._execute_order
        orig_scan = discovery_service.run_scan
        orig_list_candidates = repo.list_candidates
        orig_register = watchlist_service.register
        orig_transition = watchlist_service.transition
        orig_plan = execution_plan_service.create_plan
        automation_service.live_trading_allowed = lambda: True
        automation_service._execute_order = lambda *args, **kwargs: {"ok": False, "status": "failed"}
        discovery_service.run_scan = lambda **kwargs: {"scan_id": 999, "summary": {"status": "completed"}}
        repo.list_candidates = lambda **kwargs: [{
            "candidate_id": 1, "market": "KR", "symbol": "005930", "name": "삼성",
            "current_price": 100, "decision": "watch", "final_score": 90, "rule_score": 80, "risk_score": 10,
            "data_as_of": now().isoformat(),
        }]
        watchlist_service.register = lambda cand: {"ok": True}
        watchlist_service.transition = lambda *args, **kwargs: {"ok": True}
        execution_plan_service.create_plan = lambda *args, **kwargs: {"id": 7, "quantity": 1, "entry_price": 100}
        try:
            set_policy("ai_stock_default_v1", "KR", {
                "automation_level": 6,
                "auto_execute": 1,
                "min_final_score": 0,
                "min_rule_score": 0,
                "max_risk_score": 100,
            })
            result = run_strategy(market="KR", strategy_id="ai_stock_default_v1")
            self.assertEqual(result["automation"].get("executed", 0), 0)
            self.assertTrue(any(":execute:" in item for item in result["automation"]["blocked"]))
        finally:
            automation_service.live_trading_allowed = orig_guard
            automation_service._execute_order = orig_execute
            discovery_service.run_scan = orig_scan
            repo.list_candidates = orig_list_candidates
            watchlist_service.register = orig_register
            watchlist_service.transition = orig_transition
            execution_plan_service.create_plan = orig_plan

    def test_level6_does_not_count_paper_status_as_executed(self):
        from src.ai_stock import discovery_service, watchlist_service, execution_plan_service

        orig_guard = automation_service.live_trading_allowed
        orig_execute = automation_service._execute_order
        orig_scan = discovery_service.run_scan
        orig_list_candidates = repo.list_candidates
        orig_register = watchlist_service.register
        orig_transition = watchlist_service.transition
        orig_plan = execution_plan_service.create_plan
        automation_service.live_trading_allowed = lambda: True
        automation_service._execute_order = lambda *args, **kwargs: {"ok": False, "status": "paper"}
        discovery_service.run_scan = lambda **kwargs: {"scan_id": 999, "summary": {"status": "completed"}}
        repo.list_candidates = lambda **kwargs: [{
            "candidate_id": 1, "market": "KR", "symbol": "005930", "name": "Samsung",
            "current_price": 100, "decision": "watch", "final_score": 90, "rule_score": 80,
            "risk_score": 10, "data_as_of": now().isoformat(),
        }]
        watchlist_service.register = lambda cand: {"ok": True}
        watchlist_service.transition = lambda *args, **kwargs: {"ok": True}
        execution_plan_service.create_plan = lambda *args, **kwargs: {"id": 7, "quantity": 1, "entry_price": 100}
        try:
            set_policy("ai_stock_default_v1", "KR", {
                "automation_level": 6,
                "auto_execute": 1,
                "min_final_score": 0,
                "min_rule_score": 0,
                "max_risk_score": 100,
            })
            result = run_strategy(market="KR", strategy_id="ai_stock_default_v1")
            self.assertEqual(result["automation"].get("executed", 0), 0)
            self.assertTrue(any(":execute:" in item for item in result["automation"]["blocked"]))
        finally:
            automation_service.live_trading_allowed = orig_guard
            automation_service._execute_order = orig_execute
            discovery_service.run_scan = orig_scan
            repo.list_candidates = orig_list_candidates
            watchlist_service.register = orig_register
            watchlist_service.transition = orig_transition
            execution_plan_service.create_plan = orig_plan

    def test_extract_order_refs_from_broker_result_shapes(self):
        direct = automation_service._extract_order_refs({
            "order_id": 12,
            "broker_order_id": "BRK-1",
        })
        self.assertEqual(direct["order_id"], 12)
        self.assertEqual(direct["broker_order_id"], "BRK-1")

        nested = automation_service._extract_order_refs({
            "res": {"output": {"ODNO": "000123"}},
        })
        self.assertIsNone(nested["order_id"])
        self.assertEqual(nested["broker_order_id"], "000123")


if __name__ == "__main__":
    unittest.main()
