import unittest

from src.strategy.custom_rules.issue_sector_rotation_strategy import IssueSectorRotationStrategy


class IssueSectorRotationStrategyTests(unittest.TestCase):
    def test_scores_sector_rotation_candidate(self):
        strategy = IssueSectorRotationStrategy()
        prices = [100.0 + i * 0.15 for i in range(40)]
        prices += [
            106.0, 106.5, 107.0, 107.8, 108.5, 109.2, 110.0, 111.0, 112.0, 113.0,
            112.5, 115.0, 113.0, 116.0, 114.5, 118.0, 116.5, 120.0, 118.0, 124.0,
        ]
        highs = [p * 1.01 for p in prices]
        highs[-2] = 124.5
        highs[-1] = 125.0
        volumes = [10_000_000.0] * 59 + [26_000_000.0]
        indicators = {
            "sma20": sum(prices[-20:]) / 20,
            "sma60": sum(prices[-60:]) / 60,
            "rsi": 68.0,
            "macd_hist": 1.2,
            "highs": highs,
            "volumes": volumes,
            "symbol": "123456",
        }

        score = strategy.calculate_score(prices, indicators)

        self.assertGreaterEqual(score, 4.0)
        self.assertIn("issue_sector_rotation", indicators)
        self.assertGreaterEqual(indicators["issue_sector_rotation"]["volume_ratio"], 2.0)
        self.assertGreaterEqual(indicators["issue_sector_rotation"]["trade_value"], 3_000_000_000)

    def test_caps_score_for_overheated_chase(self):
        strategy = IssueSectorRotationStrategy()
        prices = [100.0] * 56 + [110.0, 125.0, 150.0, 190.0]
        highs = [p * 1.01 for p in prices]
        volumes = [1_000_000.0] * 59 + [7_000_000.0]
        indicators = {
            "sma20": sum(prices[-20:]) / 20,
            "sma60": sum(prices[-60:]) / 60,
            "rsi": 88.0,
            "macd_hist": 3.0,
            "highs": highs,
            "volumes": volumes,
            "symbol": "123456",
        }

        score = strategy.calculate_score(prices, indicators)

        self.assertLessEqual(score, 2.0)
        self.assertGreater(indicators["issue_sector_rotation"]["return_3d"], 25.0)


if __name__ == "__main__":
    unittest.main()
