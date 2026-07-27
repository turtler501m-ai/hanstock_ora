from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from src.strategy.autonomy.continuous_service import ContinuousStrategyService
from src.strategy.autonomy.orchestrator import (
    CycleResult,
    MarketContext,
    PortfolioContext,
)
from src.strategy.autonomy.protection import ProtectionGateSignal
from src.strategy.autonomy.risk_envelope import RiskSnapshot
from src.strategy.autonomy.lifecycle import StrategyHealth


NOW = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)


class Adapter:
    def __init__(self, strategy_id):
        self.strategy_id = strategy_id


class Contexts:
    def __init__(self, fail_market=None):
        self.fail_market = fail_market

    def market_context(self, market):
        if market == self.fail_market:
            raise RuntimeError("snapshot unavailable")
        return MarketContext(market, "bull", NOW, NOW, f"market-{market}")

    def portfolio_context(self, market):
        snapshot = RiskSnapshot(
            total_equity=1_000_000,
            available_cash=500_000,
            daily_pnl=0,
            position_value=0,
            market_exposure_value=0,
            sector_exposure_value=0,
            strategy_exposure_value=0,
            reserved_symbol_exposure_value=0,
            reserved_market_exposure_value=0,
            reserved_sector_exposure_value=0,
            reserved_strategy_exposure_value=0,
            sector_key="semiconductor",
            average_daily_trading_value=10_000_000,
            open_position_risk_amount_excluding_reservations=0,
            current_position_qty=0,
            market_regime="bull",
            data_as_of=NOW,
            evaluated_at=NOW,
            kill_switch_active=False,
        )
        return PortfolioContext("A1", f"portfolio-{market}", {"005930": snapshot})


class Recovery:
    def __init__(self, blocked=(), fail_reconcile=()):
        self.blocked = set(blocked)
        self.fail_reconcile = set(fail_reconcile)
        self.reconciled = []
        self.audited = []

    def reconcile_open_orders(self, market):
        self.reconciled.append(market)
        if market in self.fail_reconcile:
            raise RuntimeError("reconcile failed")

    def audit_unprotected_positions(self, market):
        self.audited.append(market)
        return ProtectionGateSignal(
            market in self.blocked,
            "unprotected_open_quantity" if market in self.blocked else "clear",
        )


class Orchestrator:
    def __init__(self, fail_strategy=None):
        self.fail_strategy = fail_strategy
        self.calls = []

    def run_cycle(self, **kwargs):
        adapter = kwargs["adapter"]
        self.calls.append(kwargs)
        if adapter.strategy_id == self.fail_strategy:
            raise RuntimeError("strategy failed")
        return CycleResult(kwargs["cycle_key"], 1, 1, ())


class HealthReport:
    def lifecycle_health(self):
        return StrategyHealth(observations=10, errors=3, halt_required=True)


class HealthService:
    def __init__(self):
        self.strategies = []

    def evaluate_and_enforce(self, strategy_id):
        self.strategies.append(strategy_id)
        return HealthReport()


class ApprovalPlanner:
    def __init__(self):
        self.cycles = []

    def queue_cycle(self, cycle):
        self.cycles.append(cycle)
        return (SimpleNamespace(approval_id=100 + len(self.cycles)),)


class ContinuousStrategyServiceTest(unittest.TestCase):
    def service(
        self, *, contexts=None, recovery=None, orchestrator=None,
        approval_planner=None
    ):
        return ContinuousStrategyService(
            markets=("KR", "US"),
            adapters={
                "KR": (Adapter("alpha"), Adapter("beta")),
                "US": (Adapter("gamma"),),
            },
            orchestrator=orchestrator or Orchestrator(),
            contexts=contexts or Contexts(),
            recovery=recovery or Recovery(),
            approval_planner=approval_planner or ApprovalPlanner(),
            interval_seconds=1,
            clock=lambda: NOW,
        )

    def test_each_strategy_cycle_is_advanced_to_approval_queue(self):
        planner = ApprovalPlanner()
        service = self.service(approval_planner=planner)

        result = service.run_market_once("KR")

        self.assertEqual(len(planner.cycles), 2)
        self.assertEqual(result.approval_ids, (101, 102))

    def test_startup_reconciles_and_audits_each_market(self):
        recovery = Recovery(blocked=("US",))
        service = self.service(recovery=recovery)
        result = service.startup_recovery()
        self.assertEqual(recovery.reconciled, ["KR", "US"])
        self.assertEqual(recovery.audited, ["KR", "US"])
        self.assertEqual(result.blocked_markets, frozenset({"US"}))

    def test_protection_block_is_injected_into_every_trusted_risk_snapshot(self):
        orchestrator = Orchestrator()
        service = self.service(
            recovery=Recovery(blocked=("KR",)),
            orchestrator=orchestrator,
        )
        result = service.run_market_once("KR")
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.protection_block)
        for call in orchestrator.calls:
            snapshot = call["portfolio"].risk_snapshot_for("005930")
            self.assertTrue(snapshot.protection_global_block)

    def test_snapshot_failure_creates_no_strategy_cycle(self):
        orchestrator = Orchestrator()
        service = self.service(
            contexts=Contexts(fail_market="KR"),
            orchestrator=orchestrator,
        )
        result = service.run_market_once("KR")
        self.assertEqual(result.status, "safety_snapshot_blocked")
        self.assertTrue(result.protection_block)
        self.assertEqual(orchestrator.calls, [])

    def test_strategy_error_does_not_skip_next_strategy(self):
        orchestrator = Orchestrator(fail_strategy="alpha")
        service = self.service(orchestrator=orchestrator)
        result = service.run_market_once("KR")
        self.assertEqual(result.status, "completed_with_errors")
        self.assertEqual(
            [call["adapter"].strategy_id for call in orchestrator.calls],
            ["alpha", "beta"],
        )

    def test_market_reentry_is_skipped(self):
        service = self.service()
        service._locks["KR"].acquire()
        try:
            result = service.run_market_once("KR")
        finally:
            service._locks["KR"].release()
        self.assertEqual(result.status, "reentry_skipped")

    def test_stop_event_prevents_further_market_cycles(self):
        orchestrator = Orchestrator()
        service = self.service(orchestrator=orchestrator)
        service.stop()
        self.assertEqual(service.run_iteration(), ())
        self.assertEqual(orchestrator.calls, [])

    def test_health_is_evaluated_before_each_strategy_cycle(self):
        orchestrator = Orchestrator()
        health = HealthService()
        service = self.service(orchestrator=orchestrator)
        service.health_service = health
        service.run_market_once("KR")
        self.assertEqual(health.strategies, ["alpha", "beta"])
        for call in orchestrator.calls:
            self.assertTrue(call["strategy_health"].halt_required)


if __name__ == "__main__":
    unittest.main()
