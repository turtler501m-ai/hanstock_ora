import unittest
from datetime import datetime, timedelta, timezone

from src.strategy.autonomy.models import (
    EntryPlan,
    ExitPlan,
    ExitTarget,
    InvalidationPlan,
    OrderPlan,
    OrderType,
    TradeAction,
    TradeIntent,
)
from src.strategy.autonomy.orchestrator import (
    AutonomousStrategyOrchestrator,
    MarketContext,
    PortfolioContext,
)
from src.strategy.autonomy.risk_envelope import (
    RiskEnvelope,
    RiskLimits,
    RiskSnapshot,
)
from src.strategy.autonomy.lifecycle import LifecycleDecision


NOW = datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc)


class FakePersistence:
    def __init__(self):
        self.positions = []
        self.decisions = {}
        self.orders = {}
        self.reservations = {}
        self.next_id = 1
        self.fail_reservation = False
        self.fail_order = False
        self.reservation_requests = []

    def list_active_positions(self, **_):
        return list(self.positions)

    def get_decision(self, key):
        return self.decisions.get(key)

    def save_decision(self, data):
        row = dict(data, id=self.next_id)
        self.next_id += 1
        self.decisions[data["decision_key"]] = row
        return row["id"]

    def update_decision(self, decision_id, **values):
        row = next(row for row in self.decisions.values() if row["id"] == decision_id)
        row.update(values)
        return True

    def create_position(self, data):
        row = dict(data, id=self.next_id)
        self.next_id += 1
        self.positions.append(row)
        return row["id"]

    def get_order(self, key):
        return self.orders.get(key)

    def create_order(self, data):
        if self.fail_order:
            raise ValueError("order insert failed")
        row = dict(data, id=self.next_id)
        self.next_id += 1
        self.orders[data["client_order_key"]] = row
        return row["id"]

    def reserve_risk(self, data, *, available_cash, risk_budget_limit):
        if self.fail_reservation:
            raise ValueError("reservation denied")
        self.reservation_requests.append(
            (dict(data), available_cash, risk_budget_limit)
        )
        row = dict(data, id=self.next_id, status="active")
        self.next_id += 1
        if row["cash_amount"] > available_cash or row["risk_amount"] > risk_budget_limit:
            raise ValueError("reservation limit")
        self.reservations[row["id"]] = row
        return row

    def release_risk(self, reservation_id, *, reason):
        self.reservations[reservation_id]["status"] = "released"
        return self.reservations[reservation_id]

    def abandon_position(self, position_id, *, reason):
        row = next(row for row in self.positions if row["id"] == position_id)
        row["status"] = "closed"
        return True


def entry_intent():
    return TradeIntent(
        intent_id="entry-1",
        strategy_id="s1",
        strategy_version=1,
        profile_hash="hash-1",
        symbol="005930",
        market="KR",
        action=TradeAction.ENTER_LONG,
        confidence=0.8,
        thesis="pullback",
        created_at=NOW,
        data_as_of=NOW,
        valid_until=NOW + timedelta(minutes=10),
        entry=EntryPlan(
            OrderPlan(OrderType.LIMIT, limit_price=1000), 990, 1010
        ),
        invalidation=InvalidationPlan(900),
        exit_plan=ExitPlan(targets=(ExitTarget(1200, 100),)),
    )


class Adapter:
    strategy_id = "s1"

    def __init__(self, intents):
        self.intents = intents
        self.managed = []

    def scan(self, market, portfolio):
        return self.intents

    def manage_position(self, position, market, portfolio):
        self.managed.append(position["id"])
        return TradeIntent(
            intent_id=f"hold-{position['id']}",
            strategy_id="s1",
            strategy_version=1,
            profile_hash="hash-1",
            symbol=position["symbol"],
            market="KR",
            action=TradeAction.HOLD,
            confidence=0.7,
            thesis="still valid",
            created_at=NOW,
            data_as_of=NOW,
            valid_until=NOW + timedelta(minutes=10),
            position_id=str(position["id"]),
        )


class AllowLifecycle:
    def evaluate(self, intent, health=None):
        return LifecycleDecision(True, "approved")


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        limits = RiskLimits(
            max_risk_per_trade_pct=1,
            max_daily_loss_pct=3,
            max_position_pct=10,
            max_market_exposure_pct=50,
            max_sector_exposure_pct=20,
            max_liquidity_participation_pct=1,
            max_strategy_exposure_pct=25,
            max_total_open_risk_pct=3,
            max_data_age_seconds=60,
            allowed_regimes=frozenset({"bull"}),
        )
        self.db = FakePersistence()
        self.service = AutonomousStrategyOrchestrator(
            RiskEnvelope(limits), self.db, AllowLifecycle()
        )
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
        self.market = MarketContext("KR", "bull", NOW, NOW, "market-1")
        self.portfolio = PortfolioContext("account-1", "portfolio-1", {"005930": snapshot})

    def test_creates_owned_position_and_managed_order_without_broker(self):
        result = self.service.run_cycle(
            cycle_key="cycle-1",
            adapter=Adapter([entry_intent()]),
            market=self.market,
            portfolio=self.portfolio,
        )

        self.assertEqual(result.results[0].status, "managed_order_created")
        self.assertEqual(len(self.db.positions), 1)
        self.assertEqual(len(self.db.orders), 1)
        order = next(iter(self.db.orders.values()))
        self.assertEqual(order["status"], "intent_created")
        self.assertNotIn("broker_order_id", order)

    def test_same_cycle_and_intent_are_idempotent(self):
        adapter = Adapter([entry_intent()])
        first = self.service.run_cycle(
            cycle_key="cycle-1", adapter=adapter,
            market=self.market, portfolio=self.portfolio
        )
        second = self.service.run_cycle(
            cycle_key="cycle-1", adapter=adapter,
            market=self.market, portfolio=self.portfolio
        )

        self.assertEqual(first.results[0].status, "managed_order_created")
        self.assertTrue(any(row.status == "duplicate" for row in second.results))
        self.assertEqual(len(self.db.orders), 1)

    def test_missing_risk_snapshot_fails_closed(self):
        result = self.service.run_cycle(
            cycle_key="cycle-1",
            adapter=Adapter([entry_intent()]),
            market=self.market,
            portfolio=PortfolioContext("account-1", "portfolio-1", {}),
        )

        self.assertEqual(result.results[0].status, "rejected")
        self.assertEqual(len(self.db.orders), 0)

    def test_active_positions_are_reassessed_each_cycle(self):
        self.db.positions.append(
            {"id": 7, "symbol": "005930", "strategy_id": "s1", "status": "open"}
        )
        adapter = Adapter([])

        result = self.service.run_cycle(
            cycle_key="cycle-2", adapter=adapter,
            market=self.market, portfolio=self.portfolio
        )

        self.assertEqual(adapter.managed, [7])
        self.assertEqual(result.managed_positions, 1)
        self.assertEqual(result.results[0].status, "decision_recorded")

    def test_reservation_failure_rejects_and_closes_new_position_shell(self):
        self.db.fail_reservation = True
        result = self.service.run_cycle(
            cycle_key="reservation-failure",
            adapter=Adapter([entry_intent()]),
            market=self.market,
            portfolio=self.portfolio,
        )
        self.assertEqual(result.results[0].status, "rejected")
        self.assertEqual(self.db.positions[0]["status"], "closed")
        self.assertEqual(self.db.orders, {})

    def test_order_failure_releases_reservation_and_closes_position_shell(self):
        self.db.fail_order = True
        result = self.service.run_cycle(
            cycle_key="order-failure",
            adapter=Adapter([entry_intent()]),
            market=self.market,
            portfolio=self.portfolio,
        )
        self.assertEqual(result.results[0].status, "rejected")
        self.assertEqual(self.db.positions[0]["status"], "closed")
        self.assertEqual(next(iter(self.db.reservations.values()))["status"], "released")

    def test_reservation_uses_actual_trade_risk_and_total_account_limit(self):
        self.service.run_cycle(
            cycle_key="risk-authority",
            adapter=Adapter([entry_intent()]),
            market=self.market,
            portfolio=self.portfolio,
        )
        data, available_cash, total_limit = self.db.reservation_requests[0]
        self.assertEqual(available_cash, 500_000)
        self.assertEqual(total_limit, 30_000)
        self.assertLess(data["risk_amount"], total_limit)
        self.assertEqual(data["risk_amount"], 9_900)


if __name__ == "__main__":
    unittest.main()
