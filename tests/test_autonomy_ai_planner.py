import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from src.strategy.autonomy.ai_planner import (
    AutonomousAIAdapter,
    DemoRulePlanner,
    OpenAIResponsesPlanner,
    PlannerError,
    PlannerResponse,
)
from src.strategy.autonomy.orchestrator import MarketContext, PortfolioContext
from src.strategy.autonomy.risk_envelope import RiskSnapshot


NOW = datetime(2026, 7, 23, tzinfo=timezone.utc)


def payload(**changes):
    row = {
        "intent_id": "intent-1",
        "strategy_id": "s1",
        "strategy_version": 7,
        "profile_hash": "hash-7",
        "symbol": "005930",
        "market": "KR",
        "action": "enter_long",
        "confidence": 0.8,
        "thesis": "bull pullback with volume recovery",
        "created_at": NOW.isoformat(),
        "data_as_of": NOW.isoformat(),
        "valid_until": (NOW + timedelta(minutes=10)).isoformat(),
        "entry": {
            "order": {
                "order_type": "limit",
                "time_in_force": "day",
                "limit_price": 1000,
                "stop_price": None,
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            },
            "price_min": 990,
            "price_max": 1010,
        },
        "invalidation": {
            "hard_stop_price": 900,
            "conditions": ["close_below_support"],
        },
        "exit_plan": {
            "targets": [{"price": 1200, "reduce_pct": 100}],
            "trailing_stop": None,
            "max_holding_until": None,
        },
        "position_id": None,
        "reduce_pct": None,
        "reasons": ["trend", "volume"],
        "evidence": {"signals": ["rsi_recovery"], "data_quality": "good"},
        "metadata": {"fallback_used": False, "planner_mode": "candidate_scan"},
    }
    row.update(changes)
    return row


class FakeProvider:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.contexts = []

    def plan(self, *, instructions, context, schema):
        self.contexts.append(context)
        return PlannerResponse(self.outputs.pop(0), "mock-model")


def contexts():
    risk = RiskSnapshot(
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
    market = MarketContext(
        "KR", "bull", NOW, NOW, "market-1",
        {"candidates": [{"symbol": "005930", "price": 1000}]},
    )
    portfolio = PortfolioContext("account-1", "portfolio-1", {"005930": risk})
    return market, portfolio


class AutonomousAIAdapterTests(unittest.TestCase):
    def test_candidate_scan_produces_valid_complete_intent(self):
        provider = FakeProvider([payload()])
        adapter = AutonomousAIAdapter(
            strategy_id="s1", strategy_version=7, profile_hash="hash-7",
            provider=provider,
        )
        market, portfolio = contexts()

        intents = adapter.scan(market, portfolio)

        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].action.value, "enter_long")
        self.assertEqual(intents[0].invalidation.hard_stop_price, 900)
        self.assertIn("market_snapshot", provider.contexts[0])
        self.assertIn("portfolio_snapshot", provider.contexts[0])

    def test_position_management_includes_previous_thesis_and_identity(self):
        managed_payload = payload(
            intent_id="manage-10",
            action="hold",
            entry=None,
            invalidation=None,
            exit_plan=None,
            position_id="10",
            metadata={"fallback_used": False, "planner_mode": "position_management"},
        )
        provider = FakeProvider([managed_payload])
        adapter = AutonomousAIAdapter(
            strategy_id="s1", strategy_version=7, profile_hash="hash-7",
            provider=provider,
        )
        market, portfolio = contexts()

        intent = adapter.manage_position(
            {
                "id": 10,
                "strategy_id": "s1",
                "symbol": "005930",
                "entry_thesis": "original thesis",
                "last_decision_id": 9,
            },
            market,
            portfolio,
        )

        self.assertEqual(intent.position_id, "10")
        self.assertEqual(provider.contexts[0]["previous_thesis"], "original thesis")

    def test_identity_mismatch_is_not_repaired_or_fallbacked(self):
        adapter = AutonomousAIAdapter(
            strategy_id="s1", strategy_version=7, profile_hash="hash-7",
            provider=FakeProvider([payload(strategy_id="other")]),
        )
        market, portfolio = contexts()

        with self.assertRaises(PlannerError):
            adapter.scan(market, portfolio)


class DemoRulePlannerTests(unittest.TestCase):
    def test_demo_rule_candidate_builds_complete_entry_intent(self):
        planner = DemoRulePlanner()
        context = {
            "mode": "candidate_scan",
            "strategy": {
                "strategy_id": "s1",
                "strategy_version": 7,
                "profile_hash": "hash-7",
            },
            "market_snapshot": {
                "market": "KR",
                "evaluated_at": NOW.isoformat(),
                "data_as_of": NOW.isoformat(),
            },
            "candidate": {"symbol": "005930", "current_price": 1000},
        }
        with patch(
            "src.strategy.autonomy.ai_planner.config.autonomy_trading_env", "demo"
        ), patch(
            "src.strategy.autonomy.ai_planner.config.trading_env", "demo"
        ), patch(
            "src.strategy.autonomy.ai_planner.config.enable_live_trading", False
        ):
            result = planner.plan(instructions="", context=context, schema={})

        self.assertEqual(result.model, "demo-rule-v1")
        self.assertEqual(result.payload["action"], "enter_long")
        self.assertEqual(
            result.payload["invalidation"]["hard_stop_price"], 950
        )

    def test_demo_rule_position_exits_at_hard_stop(self):
        planner = DemoRulePlanner()
        context = {
            "mode": "position_management",
            "strategy": {
                "strategy_id": "s1",
                "strategy_version": 7,
                "profile_hash": "hash-7",
            },
            "market_snapshot": {
                "market": "KR",
                "evaluated_at": NOW.isoformat(),
                "data_as_of": NOW.isoformat(),
            },
            "portfolio_snapshot": {
                "risk_snapshots": {"005930": {"current_price": 940}}
            },
            "position": {
                "id": 9,
                "symbol": "005930",
                "current_stop_price": 950,
                "target_plan": {"targets": [{"price": 1100, "reduce_pct": 100}]},
                "max_holding_until": None,
            },
        }
        with patch(
            "src.strategy.autonomy.ai_planner.config.autonomy_trading_env", "demo"
        ), patch(
            "src.strategy.autonomy.ai_planner.config.trading_env", "demo"
        ), patch(
            "src.strategy.autonomy.ai_planner.config.enable_live_trading", False
        ):
            result = planner.plan(instructions="", context=context, schema={})

        self.assertEqual(result.payload["action"], "exit")
        self.assertEqual(result.payload["reduce_pct"], 100.0)
        self.assertEqual(result.payload["reasons"], ["hard_stop_reached"])

    def test_demo_rule_planner_is_blocked_outside_demo(self):
        with patch(
            "src.strategy.autonomy.ai_planner.config.autonomy_trading_env", "real"
        ):
            with self.assertRaises(PlannerError):
                DemoRulePlanner().plan(
                    instructions="",
                    context={},
                    schema={},
                )

    def test_rule_planner_allows_real_with_explicit_live_opt_ins(self):
        planner = DemoRulePlanner()
        context = {
            "mode": "candidate_scan",
            "strategy": {
                "strategy_id": "s1",
                "strategy_version": 7,
                "profile_hash": "hash-7",
            },
            "market_snapshot": {
                "market": "KR",
                "evaluated_at": NOW.isoformat(),
                "data_as_of": NOW.isoformat(),
            },
            "candidate": {"symbol": "005930", "current_price": 1000},
        }
        with patch(
            "src.strategy.autonomy.ai_planner.config.autonomy_trading_env", "real"
        ), patch(
            "src.strategy.autonomy.ai_planner.config.trading_env", "real"
        ), patch(
            "src.strategy.autonomy.ai_planner.config.enable_live_trading", True
        ), patch(
            "src.strategy.autonomy.ai_planner.config.autonomy_enable_live_trading",
            True,
        ), patch(
            "src.strategy.autonomy.ai_planner.config.autonomy_live_opt_in", True
        ):
            result = planner.plan(instructions="", context=context, schema={})

        self.assertEqual(result.payload["action"], "enter_long")
        self.assertIn("real", result.payload["reasons"])


class OpenAIResponsesPlannerTests(unittest.TestCase):
    @patch("src.strategy.autonomy.ai_planner.update_token_usage")
    @patch("src.strategy.autonomy.ai_planner.require_online_access")
    def test_responses_request_uses_strict_schema_and_tracks_exact_usage(
        self, online, usage
    ):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "id": "resp-1",
            "status": "completed",
            "model": "mock-model",
            "output_text": json.dumps(payload()),
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        }
        session = Mock()
        session.post.return_value = response
        provider = OpenAIResponsesPlanner(
            api_key="test-key", model="mock-model", session=session
        )

        result = provider.plan(
            instructions="plan",
            context={"candidate": {"symbol": "005930"}},
            schema={"type": "object"},
        )

        sent = session.post.call_args.kwargs["json"]
        self.assertTrue(sent["text"]["format"]["strict"])
        self.assertFalse(sent["store"])
        self.assertEqual(result.response_id, "resp-1")
        online.assert_called_once()
        usage.assert_called_once_with(100, 50, 150)

    @patch("src.strategy.autonomy.ai_planner.require_online_access")
    def test_refusal_raises_without_fallback(self, online):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "completed",
            "output": [{
                "type": "message",
                "content": [{"type": "refusal", "refusal": "cannot plan"}],
            }],
        }
        session = Mock()
        session.post.return_value = response
        provider = OpenAIResponsesPlanner(
            api_key="test-key", model="mock-model", session=session
        )

        with self.assertRaises(PlannerError):
            provider.plan(
                instructions="plan", context={}, schema={"type": "object"}
            )


if __name__ == "__main__":
    unittest.main()
