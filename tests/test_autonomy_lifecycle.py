import unittest
from dataclasses import replace
from unittest.mock import patch

from src.strategy.autonomy.lifecycle import (
    LifecyclePolicy,
    StrategyHealth,
    StrategyLifecycleGate,
)
from src.strategy.autonomy.models import TradeAction
from tests.test_autonomy_trade_intent import valid_intent


class Catalog:
    def __init__(self, strategy):
        self.strategy = strategy

    def get(self, strategy_id):
        if self.strategy and self.strategy["id"] == strategy_id:
            return self.strategy
        return None


def strategy(**changes):
    row = {
        "id": "ai-swing",
        "status": "approved",
        "strategy_version": 2,
        "profile_hash": "profile-hash-2",
        "profile": {},
    }
    row.update(changes)
    return row


class StrategyLifecycleGateTests(unittest.TestCase):
    def test_approved_exact_version_and_hash_is_allowed(self):
        decision = StrategyLifecycleGate(Catalog(strategy())).evaluate(valid_intent())

        self.assertTrue(decision.allowed)
        self.assertFalse(decision.halt_required)

    def test_unapproved_and_terminal_states_are_blocked(self):
        for status in ("draft", "review_required", "suspended", "retired"):
            with self.subTest(status=status):
                decision = StrategyLifecycleGate(
                    Catalog(strategy(status=status))
                ).evaluate(valid_intent())
                self.assertFalse(decision.allowed)

    def test_verified_strategy_is_runnable_when_approval_is_disabled(self):
        gate = StrategyLifecycleGate(Catalog(strategy(status="verified")))
        with patch(
            "src.strategy.autonomy.lifecycle.config.autonomy_require_approval",
            False,
        ):
            self.assertTrue(gate.evaluate(valid_intent()).allowed)

        with patch(
            "src.strategy.autonomy.lifecycle.config.autonomy_require_approval",
            True,
        ):
            self.assertFalse(gate.evaluate(valid_intent()).allowed)

    def test_version_or_profile_mismatch_requires_halt(self):
        for changed in (
            replace(valid_intent(), strategy_version=3),
            replace(valid_intent(), profile_hash="different"),
        ):
            with self.subTest(intent=changed):
                decision = StrategyLifecycleGate(Catalog(strategy())).evaluate(changed)
                self.assertFalse(decision.allowed)
                self.assertTrue(decision.halt_required)

    def test_fallback_requires_operator_profile_approval(self):
        intent = replace(valid_intent(), metadata={"fallback_used": True})
        denied = StrategyLifecycleGate(Catalog(strategy())).evaluate(intent)
        approved = StrategyLifecycleGate(
            Catalog(strategy(profile={"allow_fallback_trade": True}))
        ).evaluate(intent)

        self.assertFalse(denied.allowed)
        self.assertTrue(approved.allowed)

    def test_error_rate_and_state_mismatch_require_halt(self):
        gate = StrategyLifecycleGate(
            Catalog(strategy()),
            LifecyclePolicy(
                max_error_rate=0.2,
                min_error_rate_observations=5,
                max_state_mismatches=0,
            ),
        )

        high_errors = gate.evaluate(
            valid_intent(), StrategyHealth(observations=10, errors=3)
        )
        mismatch = gate.evaluate(
            valid_intent(), StrategyHealth(observations=10, state_mismatches=1)
        )

        self.assertFalse(high_errors.allowed)
        self.assertTrue(high_errors.halt_required)
        self.assertFalse(mismatch.allowed)
        self.assertTrue(mismatch.halt_required)

    def test_suspended_or_unhealthy_strategy_can_still_exit(self):
        exit_intent = replace(
            valid_intent(),
            action=TradeAction.EXIT,
            position_id="17",
            entry=None,
            invalidation=None,
        )
        decision = StrategyLifecycleGate(
            Catalog(strategy(status="suspended"))
        ).evaluate(
            exit_intent,
            StrategyHealth(observations=10, errors=10, halt_required=True),
        )
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.halt_required)


if __name__ == "__main__":
    unittest.main()
