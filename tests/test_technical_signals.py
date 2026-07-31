import unittest

from src.strategy.technical_signals import moving_average_cross, trailing_stop_signal


class TechnicalSignalsTests(unittest.TestCase):
    def test_detects_fresh_golden_cross(self):
        prices = [100.0] * 40 + [90.0] * 20 + [400.0]

        signal = moving_average_cross(prices)

        self.assertTrue(signal["golden_cross"])
        self.assertFalse(signal["dead_cross"])

    def test_detects_fresh_dead_cross(self):
        prices = [100.0] * 40 + [101.0] * 20 + [80.0]

        signal = moving_average_cross(prices)

        self.assertFalse(signal["golden_cross"])
        self.assertTrue(signal["dead_cross"])

    def test_trailing_stop_triggers_after_activation_and_drawdown(self):
        signal = trailing_stop_signal(
            current_price=110,
            return_pct=10,
            recent_highs=[100, 115, 125, 120, 110],
            activation_pct=10,
            trail_pct=8,
            lookback=20,
        )

        self.assertTrue(signal["triggered"])
        self.assertEqual(signal["peak_price"], 125.0)
        self.assertEqual(signal["drawdown_pct"], -12.0)

    def test_trailing_stop_does_not_replace_fixed_loss_stop(self):
        signal = trailing_stop_signal(
            current_price=90,
            return_pct=-10,
            recent_highs=[120, 110, 100, 90],
            activation_pct=10,
            trail_pct=7,
            lookback=20,
        )

        self.assertFalse(signal["triggered"])

    def test_trailing_stop_waits_for_activation(self):
        signal = trailing_stop_signal(
            current_price=102,
            return_pct=2,
            recent_highs=[108, 106, 102],
            activation_pct=10,
            trail_pct=5,
            lookback=20,
        )

        self.assertFalse(signal["triggered"])


if __name__ == "__main__":
    unittest.main()
