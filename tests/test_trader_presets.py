import unittest
from unittest.mock import Mock, patch
from src import trader

class TraderPresetsTests(unittest.TestCase):
    def test_build_runtime_plan_rule_only_preset_uses_rule_only_ranker(self):
        api = Mock()
        api.get_daily.return_value = []
        api.get_quote.return_value = {"current": 70000, "ask1": 70000, "bid1": 69900}
        balance = {
            "output1": [],
            "output2": [
                {
                    "dnca_tot_amt": "1000000",
                    "tot_evlu_amt": "1000000",
                    "evlu_pfls_smtl_amt": "0",
                }
            ],
        }

        preset_strategy = {
            "id": "easy_safe_preset",
            "name": "쉬운 안정형 전략",
            "provider": "none",
            "model": "none",
            "weight": 0.0,
            "selected": True,
            "profile": {
                "model": "none",
                "ai_weight": 0.0,
                "risk": {
                    "max_risk_per_trade_pct": 0.5,
                }
            }
        }

        with (
            patch("src.db.repository.load_ai_strategies", return_value=[preset_strategy]),
            patch("src.db.repository.load_watchlist_data", return_value={"symbols": ["000660"]}),
            patch("src.trader.find_candidates", return_value={"candidates": []}) as mock_find_candidates,
            patch("src.trader.build_orders", return_value=[]),
        ):
            trader.build_runtime_plan(api, balance)

            mock_find_candidates.assert_called_once_with(
                set(),
                universe=["000660"],
                ranker="rule_only",
                strategy_model="none",
                strategy_profile={
                    "model": "none",
                    "ai_weight": 0.0,
                    "risk": {
                        "max_risk_per_trade_pct": 0.5,
                    },
                },
                strategy_description="",
                api=api,
            )

    def test_build_runtime_plan_ai_preset_uses_model_ranker(self):
        api = Mock()
        api.get_daily.return_value = []
        api.get_quote.return_value = {"current": 70000, "ask1": 70000, "bid1": 69900}
        balance = {
            "output1": [],
            "output2": [
                {
                    "dnca_tot_amt": "1000000",
                    "tot_evlu_amt": "1000000",
                    "evlu_pfls_smtl_amt": "0",
                }
            ],
        }

        ai_strategy = {
            "id": "gpt_5_mini_default",
            "name": "🤖 GPT-5-mini 기본 추론 랭커",
            "provider": "openai",
            "model": "gpt-5-mini",
            "weight": 0.4,
            "selected": True,
            "profile": {
                "model": "gpt-5-mini",
                "ai_weight": 0.4,
            }
        }

        with (
            patch("src.db.repository.load_ai_strategies", return_value=[ai_strategy]),
            patch("src.db.repository.load_watchlist_data", return_value={"symbols": ["000660"]}),
            patch("src.trader.find_candidates", return_value={"candidates": []}) as mock_find_candidates,
            patch("src.trader.build_orders", return_value=[]),
        ):
            trader.build_runtime_plan(api, balance)

            mock_find_candidates.assert_called_once_with(
                set(),
                universe=["000660"],
                ranker="gpt-5-mini",
                strategy_model="gpt-5-mini",
                strategy_profile={
                    "model": "gpt-5-mini",
                    "ai_weight": 0.4,
                },
                strategy_description="",
                api=api,
            )

if __name__ == "__main__":
    unittest.main()
