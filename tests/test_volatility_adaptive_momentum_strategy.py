import unittest

from src.strategy.custom_rules.volatility_adaptive_momentum_strategy import (
    STRATEGY_PROFILE,
    VolatilityAdaptiveMomentumStrategy,
)


class VolatilityAdaptiveMomentumStrategyTests(unittest.TestCase):
    def test_profile_is_explicitly_scoped_to_high_volatility(self):
        self.assertEqual(STRATEGY_PROFILE["market_regime_filter"], ["high_volatility"])
        self.assertEqual(STRATEGY_PROFILE["strategy_type"], "momentum")

    def test_scores_liquid_high_volatility_trend_with_volume(self):
        strategy = VolatilityAdaptiveMomentumStrategy()
        prices = [10_000 + index * 80 + (200 if index % 2 else -200) for index in range(60)]
        volumes = [1_000_000] * 59 + [1_800_000]
        indicators = {
            "sma20": sum(prices[-20:]) / 20,
            "sma60": sum(prices) / 60,
            "rsi": 58.0,
            "macd_hist": 1.5,
            "volumes": volumes,
        }

        score = strategy.calculate_score(prices, indicators)

        self.assertGreaterEqual(score, 2.0)
        self.assertIn("volatility_adaptive_momentum", indicators)
        self.assertTrue(indicators["custom_reasons"])

    def test_rejects_low_liquidity_setup(self):
        strategy = VolatilityAdaptiveMomentumStrategy()
        prices = [10_000 + index * 50 + (200 if index % 2 else -200) for index in range(60)]
        indicators = {
            "sma20": sum(prices[-20:]) / 20,
            "sma60": sum(prices) / 60,
            "rsi": 55.0,
            "macd_hist": 1.0,
            "volumes": [10] * 60,
        }

        self.assertEqual(strategy.calculate_score(prices, indicators), 0.0)

    def test_rejects_extreme_one_day_chase(self):
        strategy = VolatilityAdaptiveMomentumStrategy()
        prices = [10_000 + index * 30 for index in range(59)]
        prices.append(prices[-1] * 1.2)
        indicators = {
            "sma20": sum(prices[-20:]) / 20,
            "sma60": sum(prices) / 60,
            "rsi": 70.0,
            "macd_hist": 2.0,
            "volumes": [100_000] * 59 + [300_000],
        }

        self.assertEqual(strategy.calculate_score(prices, indicators), 0.0)


if __name__ == "__main__":
    unittest.main()
