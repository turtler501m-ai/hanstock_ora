# -*- coding: utf-8 -*-
"""AI스톡 핵심 로직 테스트 (DB 불필요): 점수·시장·신선도·유니버스 (§17)."""
import unittest
from unittest.mock import patch

from src.ai_stock import scoring, markets, freshness, universe, ai_evaluator, market_data
from src.ai_stock.markets import MarketError
from src.ai_stock.scoring import ScoreProfile, score_candidate


class MarketTests(unittest.TestCase):
    def test_all_rejected_for_storage(self):
        with self.assertRaises(MarketError):
            markets.require_storable_market("ALL")

    def test_currency_separated(self):
        self.assertEqual(markets.currency_of("KR"), "KRW")
        self.assertEqual(markets.currency_of("US"), "USD")

    def test_markets_for_query_all(self):
        self.assertEqual(markets.markets_for_query("ALL"), ("KR", "US"))


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.profile = ScoreProfile()

    def test_scores_in_range(self):
        out = score_candidate(
            components={"rule": 200, "technical": -5, "momentum": 50, "narrative": 50, "ai": 50},
            profile=self.profile, regime_multiplier=1.0, risk_penalty=0,
        )
        for k in ("rule_score", "technical_score", "final_score"):
            self.assertGreaterEqual(out[k], 0.0)
            self.assertLessEqual(out[k], 100.0)

    def test_risk_lowers_final(self):
        comps = {"rule": 70, "technical": 70, "momentum": 70, "narrative": 70, "ai": 70}
        no_risk = score_candidate(components=comps, profile=self.profile, risk_penalty=0)
        with_risk = score_candidate(components=comps, profile=self.profile, risk_penalty=30)
        self.assertLess(with_risk["final_score"], no_risk["final_score"])

    def test_regime_multiplier_affects_final(self):
        comps = {"rule": 70, "technical": 70, "momentum": 70, "narrative": 70, "ai": 70}
        bull = score_candidate(components=comps, profile=self.profile, regime_multiplier=1.1)
        bear = score_candidate(components=comps, profile=self.profile, regime_multiplier=0.9)
        self.assertGreater(bull["final_score"], bear["final_score"])

    def test_rule_below_min_blocks_promotion(self):
        # 룰 점수 0이면 final이 높아도 watch 이상 승격 불가
        out = score_candidate(
            components={"rule": 0, "technical": 100, "momentum": 100, "narrative": 100, "ai": 100},
            profile=ScoreProfile(min_rule_score=40.0),
        )
        self.assertNotIn(out["decision"], ("strong_watch", "watch"))

    def test_ai_fallback_zeroes_ai_weight(self):
        out = score_candidate(
            components={"rule": 60, "technical": 60, "momentum": 60, "narrative": 60, "ai": 100},
            profile=self.profile, ai_fallback=True,
        )
        self.assertEqual(out["ai_weight_applied"], 0.0)
        self.assertTrue(out["fallback_used"])

    def test_low_confidence_drops_ai(self):
        out = score_candidate(
            components={"rule": 60, "technical": 60, "momentum": 60, "narrative": 60, "ai": 100},
            profile=ScoreProfile(min_ai_confidence=0.6), ai_confidence=0.3,
        )
        self.assertEqual(out["ai_weight_applied"], 0.0)


class AiEvaluatorTests(unittest.TestCase):
    def test_probability_is_not_exposed_as_confidence(self):
        class _Predictor:
            model_name = "test-model"

            def predict(self, features):
                return {"model_status": "ready", "ml_score": 0.91}

        orig = ai_evaluator._profile_predictor
        ai_evaluator._profile_predictor = lambda profile: _Predictor()
        try:
            out = ai_evaluator.evaluate({
                "rule_score": 1,
                "technical_score": 2,
                "momentum_score": 3,
                "narrative_score": 4,
                "risk_score": 5,
            })
            self.assertEqual(out["ai_score"], 91.0)
            self.assertNotEqual(out["confidence"], 0.91)
            self.assertEqual(out["model_status"], "ready")
        finally:
            ai_evaluator._profile_predictor = orig

    def test_confidence_none_when_feature_quality_unknown(self):
        class _Predictor:
            model_name = "test-model"

            def predict(self, features):
                return {"model_status": "ready", "ml_score": 0.5}

        orig = ai_evaluator._profile_predictor
        ai_evaluator._profile_predictor = lambda profile: _Predictor()
        try:
            out = ai_evaluator.evaluate({})
            self.assertEqual(out["ai_score"], 50.0)
            self.assertIsNone(out["confidence"])
        finally:
            ai_evaluator._profile_predictor = orig


class FreshnessTests(unittest.TestCase):
    def test_missing_is_stale(self):
        self.assertTrue(freshness.is_stale(None, "ai_eval"))

    def test_recent_not_stale(self):
        self.assertFalse(freshness.is_stale(freshness.now().isoformat(), "ai_eval"))


class MarketDataTests(unittest.TestCase):
    def test_index_history_covers_autonomy_regime_requirement(self):
        with patch(
            "src.mistock.strategy.fetch_history",
            return_value={"close": [100.0] * 220},
        ) as fetch:
            result = market_data.DefaultProvider().index_series("KR")

        self.assertEqual(len(result["KOSPI"]), 220)
        fetch.assert_called_once_with("^KS11", period="1y")


class UniverseTests(unittest.TestCase):
    def test_excludes_small_cap_and_keeps_etf(self):
        items = [
            {"symbol": "BIG", "instrument_type": "stock", "market_cap": 5e11, "avg_trading_value": 1e10, "price": 50000},
            {"symbol": "SMALL", "instrument_type": "stock", "market_cap": 1e10, "avg_trading_value": 1e8, "price": 1000},
            {"symbol": "ETF1", "instrument_type": "etf", "market_cap": None, "avg_trading_value": None, "price": 30000},
        ]
        result = universe.build("KR", items)
        passed = {p["symbol"] for p in result["passed"]}
        excluded = {e["symbol"] for e in result["excluded"]}
        self.assertIn("BIG", passed)
        self.assertIn("ETF1", passed)  # ETF 포함
        self.assertIn("SMALL", excluded)  # 소형주 제외

    def test_etf_disabled(self):
        items = [{"symbol": "ETF1", "instrument_type": "etf", "price": 30000}]
        result = universe.build("KR", items, {"include_etf": False})
        self.assertEqual(result["passed_count"], 0)


if __name__ == "__main__":
    unittest.main()
