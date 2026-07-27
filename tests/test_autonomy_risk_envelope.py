from datetime import datetime, timedelta, timezone
from dataclasses import replace
import unittest

from src.strategy.autonomy.risk_envelope import (
    RiskEnvelope,
    RiskLimits,
    RiskSnapshot,
)
from src.strategy.autonomy.models import TradeAction


class RiskEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 23, tzinfo=timezone.utc)
        self.gate = RiskEnvelope(
            RiskLimits(
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
                min_cash_reserve_pct=10,
            )
        )

    def snapshot(self, **changes):
        values = {
            "total_equity": 1_000_000,
            "available_cash": 500_000,
            "daily_pnl": 0,
            "position_value": 0,
            "market_exposure_value": 100_000,
            "sector_exposure_value": 50_000,
            "strategy_exposure_value": 40_000,
            "reserved_symbol_exposure_value": 0,
            "reserved_market_exposure_value": 0,
            "reserved_sector_exposure_value": 0,
            "reserved_strategy_exposure_value": 0,
            "sector_key": "semiconductor",
            "average_daily_trading_value": 10_000_000,
            "open_position_risk_amount_excluding_reservations": 0,
            "current_position_qty": 0,
            "market_regime": "bull",
            "data_as_of": self.now,
            "evaluated_at": self.now,
            "kill_switch_active": False,
        }
        values.update(changes)
        return RiskSnapshot(**values)

    def test_quantity_is_minimum_of_all_caps(self):
        decision = self.gate.evaluate(
            {"action": "enter_long", "entry_price": 1000, "stop_price": 900, "quantity": 999},
            self.snapshot(),
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.quantity, 100)
        self.assertEqual(decision.binding_cap, "risk")

    def test_trade_action_enum_is_supported(self):
        decision = self.gate.evaluate(
            {
                "action": TradeAction.ENTER_LONG,
                "entry": {"price_max": 1000},
                "invalidation": {"hard_stop_price": 900},
            },
            self.snapshot(),
        )
        self.assertTrue(decision.approved)

    def test_missing_stop_is_denied(self):
        decision = self.gate.evaluate(
            {"action": "buy", "entry_price": 1000, "quantity": 1}, self.snapshot()
        )
        self.assertFalse(decision.approved)
        self.assertIn("stop_price_required", decision.reasons)

    def test_all_safety_halts_fail_closed(self):
        cases = (
            ({"kill_switch_active": True}, "kill_switch_off"),
            ({"daily_pnl": -30_000}, "daily_loss_limit"),
            ({"market_regime": "crash"}, "allowed_market_regime"),
            (
                {"data_as_of": self.now - timedelta(seconds=61)},
                "fresh_data",
            ),
            ({"account_snapshot_available": False}, "account_snapshot_available"),
        )
        intent = {"action": "add", "entry_price": 1000, "stop_price": 900, "quantity": 1}
        for changes, reason in cases:
            with self.subTest(reason=reason):
                decision = self.gate.evaluate(intent, self.snapshot(**changes))
                self.assertFalse(decision.approved)
                self.assertIn(reason, decision.reasons)

    def test_exit_remains_available_during_kill_switch(self):
        decision = self.gate.evaluate(
            {"action": "exit", "entry_price": 950, "reduce_pct": 100},
            self.snapshot(kill_switch_active=True, current_position_qty=12),
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.quantity, 12)

    def test_reduce_uses_percentage_not_strategy_share_quantity(self):
        decision = self.gate.evaluate(
            {"action": "reduce", "entry_price": 950, "reduce_pct": 25},
            self.snapshot(current_position_qty=10),
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.quantity, 2)

    def test_reduce_below_one_share_and_invalid_exit_percent_are_denied(self):
        too_small = self.gate.evaluate(
            {"action": "reduce", "entry_price": 950, "reduce_pct": 10},
            self.snapshot(current_position_qty=5),
        )
        partial_exit = self.gate.evaluate(
            {"action": "exit", "entry_price": 950, "reduce_pct": 50},
            self.snapshot(current_position_qty=5),
        )

        self.assertFalse(too_small.approved)
        self.assertIn("reduction_below_one_share", too_small.reasons)
        self.assertFalse(partial_exit.approved)
        self.assertIn("exit_requires_100_pct", partial_exit.reasons)

    def test_nan_exposure_is_denied(self):
        decision = self.gate.evaluate(
            {"action": "buy", "entry_price": 1000, "stop_price": 900, "quantity": 1},
            self.snapshot(sector_exposure_value=float("nan")),
        )
        self.assertFalse(decision.approved)
        self.assertIn("sector_exposure_valid", decision.reasons)

    def test_open_position_risk_reduces_account_risk_capacity(self):
        decision = self.gate.evaluate(
            {"action": "buy", "entry_price": 1000, "stop_price": 900},
            self.snapshot(open_position_risk_amount_excluding_reservations=25_000),
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.account_risk_reservation_limit, 5_000)
        self.assertEqual(decision.quantity, 50)
        self.assertEqual(decision.binding_cap, "account_risk")

    def test_pending_reserved_exposure_is_included_in_every_weight_cap(self):
        decision = self.gate.evaluate(
            {"action": "buy", "entry_price": 1000, "stop_price": 900},
            self.snapshot(
                position_value=80_000,
                reserved_symbol_exposure_value=15_000,
                reserved_market_exposure_value=100_000,
                reserved_sector_exposure_value=140_000,
                reserved_strategy_exposure_value=205_000,
            ),
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.quantity, 5)
        self.assertIn(
            decision.binding_cap,
            {"position", "sector", "strategy"},
        )

    def test_reduction_ignores_exposure_caps(self):
        decision = self.gate.evaluate(
            {"action": "exit", "entry_price": 1000},
            self.snapshot(
                current_position_qty=3,
                strategy_exposure_value=999_999,
                reserved_market_exposure_value=999_999,
            ),
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.quantity, 3)

    def test_unprotected_position_global_block_denies_new_risk(self):
        decision = self.gate.evaluate(
            {"action": "add", "entry_price": 1000, "stop_price": 900, "quantity": 1},
            self.snapshot(protection_global_block=True),
        )
        self.assertFalse(decision.approved)
        self.assertIn("all_open_positions_protected", decision.reasons)

    def test_authoritative_daily_order_limit_denies_new_risk(self):
        gate = RiskEnvelope(replace(self.gate.limits, max_daily_orders=2))
        decision = gate.evaluate(
            {"action": "add", "entry_price": 1000, "stop_price": 900, "quantity": 1},
            self.snapshot(daily_new_risk_orders=2),
        )
        self.assertFalse(decision.approved)
        self.assertIn("daily_order_limit", decision.reasons)

    def test_invalid_daily_order_count_fails_closed(self):
        decision = self.gate.evaluate(
            {"action": "buy", "entry_price": 1000, "stop_price": 900, "quantity": 1},
            self.snapshot(daily_new_risk_orders=float("nan")),
        )
        self.assertFalse(decision.approved)
        self.assertIn("daily_order_count_valid", decision.reasons)


if __name__ == "__main__":
    unittest.main()
