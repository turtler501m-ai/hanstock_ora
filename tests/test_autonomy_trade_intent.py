import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.strategy.autonomy import (
    EntryPlan,
    ExitPlan,
    ExitTarget,
    IntentValidationError,
    InvalidationPlan,
    OrderPlan,
    OrderType,
    TradeAction,
    TradeIntent,
    validate_trade_intent,
)


NOW = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)


def valid_intent(**overrides):
    values = {
        "intent_id": "intent-1",
        "strategy_id": "ai-swing",
        "strategy_version": 2,
        "profile_hash": "profile-hash-2",
        "symbol": "005930",
        "market": "KR",
        "action": TradeAction.ENTER_LONG,
        "confidence": 0.78,
        "thesis": "trend pullback",
        "created_at": NOW,
        "data_as_of": NOW - timedelta(seconds=5),
        "valid_until": NOW + timedelta(minutes=30),
        "entry": EntryPlan(
            order=OrderPlan(OrderType.LIMIT, limit_price=70000),
            price_min=69500,
            price_max=70500,
        ),
        "invalidation": InvalidationPlan(68000, ("close_below_sma60",)),
        "exit_plan": ExitPlan(targets=(ExitTarget(73500, 50), ExitTarget(77000, 50))),
    }
    values.update(overrides)
    return TradeIntent(**values)


class TradeIntentValidationTests(unittest.TestCase):
    def test_valid_complete_entry_intent(self):
        intent = valid_intent()

        self.assertIs(validate_trade_intent(intent, now=NOW), intent)
        self.assertEqual(intent.to_dict()["action"], "enter_long")
        self.assertEqual(intent.to_dict()["entry"]["order"]["order_type"], "limit")

    def test_entry_requires_invalidation_and_exit(self):
        with self.assertRaises(IntentValidationError) as caught:
            validate_trade_intent(valid_intent(invalidation=None, exit_plan=None))

        self.assertIn("invalidation plan is required", str(caught.exception))
        self.assertIn("exit plan is required", str(caught.exception))

    def test_long_stop_cannot_be_at_or_above_entry(self):
        with self.assertRaises(IntentValidationError):
            validate_trade_intent(
                valid_intent(invalidation=InvalidationPlan(70000)),
            )

    def test_exit_percent_cannot_exceed_one_hundred(self):
        with self.assertRaises(IntentValidationError):
            validate_trade_intent(
                valid_intent(
                    exit_plan=ExitPlan(
                        targets=(ExitTarget(73500, 60), ExitTarget(77000, 50)),
                    ),
                ),
            )

    def test_position_action_requires_position_id(self):
        with self.assertRaises(IntentValidationError):
            validate_trade_intent(
                valid_intent(
                    action=TradeAction.HOLD,
                    entry=None,
                    invalidation=None,
                    exit_plan=None,
                ),
            )

    def test_expired_intent_is_rejected(self):
        with self.assertRaises(IntentValidationError):
            validate_trade_intent(valid_intent(), now=NOW + timedelta(hours=1))

    def test_quantity_hidden_in_metadata_is_rejected(self):
        with self.assertRaises(IntentValidationError) as caught:
            validate_trade_intent(valid_intent(metadata={"quantity": 100}))

        self.assertIn("cannot prescribe executable quantity", str(caught.exception))

    def test_reduce_requires_percentage_and_exit_requires_one_hundred(self):
        base = valid_intent(
            action=TradeAction.REDUCE,
            entry=None,
            invalidation=None,
            position_id="10",
        )
        with self.assertRaises(IntentValidationError):
            validate_trade_intent(base)
        reduction = replace(base, reduce_pct=25)
        self.assertIs(validate_trade_intent(reduction), reduction)

        with self.assertRaises(IntentValidationError):
            validate_trade_intent(
                replace(
                    base,
                    action=TradeAction.EXIT,
                    exit_plan=None,
                    reduce_pct=50,
                )
            )
        validate_trade_intent(
            replace(base, action=TradeAction.EXIT, exit_plan=None, reduce_pct=100)
        )


if __name__ == "__main__":
    unittest.main()
