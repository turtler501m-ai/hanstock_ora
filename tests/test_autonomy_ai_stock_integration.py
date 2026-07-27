from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.strategy.autonomy.approval_bridge import ApprovalPlanResult
from src.strategy.autonomy.ai_stock_integration import (
    _autonomy_execution_enabled,
    approve_managed_ai_stock_order,
    run_ai_stock_autonomy_cycle,
)
from src.strategy.autonomy.runtime import RuntimeConfigurationError


class AIStockAutonomyIntegrationTests(unittest.TestCase):
    def test_real_execution_is_enabled_with_all_explicit_live_flags(self):
        with patch(
            "src.strategy.autonomy.ai_stock_integration.config.autonomy_trading_env",
            "real",
        ), patch(
            "src.strategy.autonomy.ai_stock_integration.config.trading_env", "real"
        ), patch(
            "src.strategy.autonomy.ai_stock_integration.config.enable_live_trading",
            True,
        ), patch(
            "src.strategy.autonomy.ai_stock_integration.config.autonomy_enable_live_trading",
            True,
        ), patch(
            "src.strategy.autonomy.ai_stock_integration.config.autonomy_live_opt_in",
            True,
        ):
            self.assertTrue(_autonomy_execution_enabled())

    def _runtime(self):
        runtime = Mock()
        runtime.order_service = Mock()
        runtime.run.return_value = SimpleNamespace(
            cycle=SimpleNamespace(
                cycle_key="cycle-1",
                scanned_intents=1,
                managed_positions=0,
                results=(SimpleNamespace(status="managed_order_created"),),
            ),
            managed_orders=({"id": 11, "status": "risk_approved"},),
        )
        return runtime

    def _snapshots(self):
        provider = Mock()
        provider.snapshot.return_value = SimpleNamespace(
            account={"snapshot_id": "account-1"},
            market={"snapshot_id": "market-1"},
        )
        return provider

    @patch(
        "src.strategy.autonomy.ai_stock_integration.ai_stock_repository.get_policy",
        return_value={
            "enabled": 1,
            "automation_level": 5,
            "auto_approve": 1,
        },
    )
    @patch(
        "src.strategy.autonomy.ai_stock_integration.build_managed_approval_bridge"
    )
    @patch(
        "src.strategy.autonomy.ai_stock_integration.ManagedApprovalPlanService"
    )
    def test_level5_queues_canonical_managed_approval(
        self, plan_type, build_bridge, policy
    ):
        runtime = self._runtime()
        snapshots = self._snapshots()
        bridge, orders = Mock(), runtime.order_service
        build_bridge.return_value = (bridge, orders)
        plan_type.return_value.queue_runtime_result.return_value = (
            ApprovalPlanResult(order_id=11, approval_id=22),
        )

        result = run_ai_stock_autonomy_cycle(
            market="KR",
            strategy_id="s1",
            scan_id=7,
            run_type="scheduled",
            snapshots=snapshots,
            runtime=runtime,
        )

        self.assertEqual(result["approvals"][0]["approval_id"], 22)
        plan_type.return_value.queue_runtime_result.assert_called_once_with(
            runtime.run.return_value
        )

    @patch(
        "src.strategy.autonomy.ai_stock_integration.ai_stock_repository.get_policy",
        return_value={
            "enabled": 1,
            "automation_level": 4,
            "auto_approve": 0,
        },
    )
    @patch(
        "src.strategy.autonomy.ai_stock_integration.build_managed_approval_bridge"
    )
    def test_level4_creates_managed_plan_without_approval(
        self, build_bridge, policy
    ):
        result = run_ai_stock_autonomy_cycle(
            market="KR",
            strategy_id="s1",
            scan_id=7,
            run_type="scheduled",
            snapshots=self._snapshots(),
            runtime=self._runtime(),
        )

        self.assertEqual(result["approvals"], [])
        build_bridge.assert_not_called()

    @patch(
        "src.strategy.autonomy.ai_stock_integration.config.autonomy_require_approval",
        False,
    )
    @patch(
        "src.strategy.autonomy.ai_stock_integration.config.autonomy_trading_env",
        "real",
    )
    def test_approval_free_integration_is_rejected_outside_demo(self):
        with self.assertRaisesRegex(
            RuntimeConfigurationError, "enabled KR autonomy environment"
        ):
            run_ai_stock_autonomy_cycle(
                market="KR",
                strategy_id="s1",
                scan_id=7,
                run_type="scheduled",
                snapshots=self._snapshots(),
                runtime=self._runtime(),
            )

    @patch(
        "src.strategy.autonomy.ai_stock_integration.approve_managed_ai_stock_order",
        return_value={"id": 22, "status": "submitted"},
    )
    @patch(
        "src.strategy.autonomy.ai_stock_integration.ai_stock_repository.get_policy",
        return_value={
            "enabled": 1,
            "automation_level": 5,
            "auto_approve": 1,
        },
    )
    @patch(
        "src.strategy.autonomy.ai_stock_integration.build_managed_approval_bridge"
    )
    @patch(
        "src.strategy.autonomy.ai_stock_integration.ManagedApprovalPlanService"
    )
    @patch(
        "src.strategy.autonomy.ai_stock_integration.config.autonomy_require_approval",
        False,
    )
    def test_guarded_demo_automatically_approves_and_executes(
        self, plan_type, build_bridge, policy, approve
    ):
        runtime = self._runtime()
        build_bridge.return_value = (Mock(), runtime.order_service)
        plan_type.return_value.queue_runtime_result.return_value = (
            ApprovalPlanResult(order_id=11, approval_id=22),
        )

        result = run_ai_stock_autonomy_cycle(
            market="KR",
            strategy_id="s1",
            scan_id=7,
            run_type="dashboard_manual",
            snapshots=self._snapshots(),
            runtime=runtime,
        )

        self.assertEqual(result["executions"][0]["status"], "submitted")
        approve.assert_called_once_with(22)

    @patch(
        "src.strategy.autonomy.ai_stock_integration.ManagedExecutionCoordinator"
    )
    @patch("src.strategy.autonomy.ai_stock_integration.KRBrokerGateway")
    @patch("src.api.kis_api.KIStockAPI")
    @patch(
        "src.strategy.autonomy.ai_stock_integration.build_managed_approval_bridge"
    )
    @patch(
        "src.strategy.autonomy.ai_stock_integration.ai_stock_repository.get_managed_order",
        return_value={
            "id": 11,
            "market": "KR",
            "strategy_id": "s1",
            "status": "approval_queued",
        },
    )
    @patch("src.strategy.autonomy.ai_stock_integration.ApprovalService")
    def test_demo_approval_submits_canonical_order_to_kis_demo(
        self,
        approval_service,
        get_order,
        build_bridge,
        kis_type,
        gateway_type,
        coordinator_type,
    ):
        approval_service.return_value.get_pending_approval.return_value = (
            SimpleNamespace(managed_order_id=11)
        )
        bridge, orders = Mock(), Mock()
        bridge.approvals = Mock()
        bridge.approve.return_value = {"status": "approved"}
        build_bridge.return_value = (bridge, orders)
        coordinator_type.return_value.execute.return_value = {
            "status": "submitted",
            "broker_order_id": "DEMO-1",
        }

        with patch(
            "src.strategy.autonomy.ai_stock_integration.config.autonomy_trading_env",
            "demo",
        ), patch(
            "src.strategy.autonomy.ai_stock_integration.config.trading_env",
            "demo",
        ), patch(
            "src.strategy.autonomy.ai_stock_integration.config.enable_live_trading",
            False,
        ):
            result = approve_managed_ai_stock_order(7)

        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["broker_order_id"], "DEMO-1")
        coordinator_type.return_value.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
