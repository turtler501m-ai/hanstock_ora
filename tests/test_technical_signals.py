import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from src.runtime_state import RuntimeStateStore
from src.strategy.condition_monitor import (
    get_fresh_condition_symbols,
    run_condition_monitor_cycle,
    save_condition_symbols,
)
from src.strategy.position_tracker import update_position_peak
from src.strategy.technical_backtest import run_technical_walk_forward
from src.strategy.technical_readiness import build_technical_strategy_readiness
from src.strategy.technical_signals import (
    first_wave_pullback,
    moving_average_cross,
    trade_value_surge,
    trailing_stop_signal,
)


class TechnicalSignalsTests(unittest.TestCase):
    def test_technical_strategy_readiness_reaches_defined_target(self):
        readiness = build_technical_strategy_readiness()

        self.assertEqual(readiness["target_pct"], 100)
        self.assertEqual(readiness["current_pct"], 100)
        self.assertTrue(readiness["complete"])
        self.assertTrue(readiness["items"])
        self.assertTrue(all(item["complete"] for item in readiness["items"]))

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

    def test_trade_value_surge_uses_previous_twenty_days(self):
        result = trade_value_surge([100.0] * 21, [100.0] * 20 + [200.0])

        self.assertTrue(result["matched"])
        self.assertEqual(result["ratio"], 2.0)

    def test_first_wave_pullback_requires_impulse_contraction_and_rebound(self):
        prices = [100.0] * 10 + [102, 105, 108, 112, 116, 120]
        prices += [118, 116, 114, 112, 110, 108, 110, 112]
        prices = [100.0] * (41 - len(prices)) + prices
        volumes = [1000.0] * len(prices)
        peak_index = prices.index(120)
        for index in range(max(0, peak_index - 5), peak_index + 1):
            volumes[index] = 2000.0
        for index in range(peak_index + 1, len(volumes)):
            volumes[index] = 800.0

        result = first_wave_pullback(prices, volumes)

        self.assertTrue(result["matched"])
        self.assertGreaterEqual(result["wave_pct"], 12)
        self.assertGreaterEqual(result["pullback_pct"], 3)

    def test_position_peak_persists_and_resets_when_position_increases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "runtime.sqlite")
            with patch("src.strategy.position_tracker.runtime_state_store", store):
                first = update_position_peak(
                    "KR", "005930", current_price=100, entry_price=90, quantity=2
                )
                second = update_position_peak(
                    "KR", "005930", current_price=120, entry_price=90, quantity=2
                )
                restarted = update_position_peak(
                    "KR", "005930", current_price=110, entry_price=90, quantity=2
                )
                increased = update_position_peak(
                    "KR", "005930", current_price=105, entry_price=95, quantity=3
                )

        self.assertEqual(first["peak_price"], 100)
        self.assertEqual(second["peak_price"], 120)
        self.assertEqual(restarted["peak_price"], 120)
        self.assertEqual(increased["peak_price"], 105)

    def test_condition_monitor_cache_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "runtime.sqlite")
            with (
                patch("src.strategy.condition_monitor.runtime_state_store", store),
                patch("src.strategy.condition_monitor.time.time", return_value=1000),
            ):
                save_condition_symbols("KR", ["005930"], source="test")
            with patch("src.strategy.condition_monitor.runtime_state_store", store):
                self.assertEqual(
                    get_fresh_condition_symbols("KR", now=1100, max_age_seconds=180),
                    ["005930"],
                )
                self.assertEqual(
                    get_fresh_condition_symbols("KR", now=1300, max_age_seconds=180),
                    [],
                )

    def test_condition_monitor_cycle_updates_both_markets(self):
        kr_api = Mock()
        us_api = Mock()
        us_api.get_overseas_volume_rank.side_effect = [["AAPL"], ["MSFT", "AAPL"]]
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "runtime.sqlite")
            with (
                patch("src.strategy.condition_monitor.runtime_state_store", store),
                patch("src.trader.KIStockAPI", return_value=kr_api),
                patch(
                    "src.strategy.seven_split._condition_search_universe",
                    return_value=["005930"],
                ),
                patch("src.mistock.trader._get_kis_client", return_value=us_api),
            ):
                result = run_condition_monitor_cycle()
                kr_symbols = get_fresh_condition_symbols("KR")
                us_symbols = get_fresh_condition_symbols("US")

        self.assertTrue(result["ok"])
        self.assertEqual(kr_symbols, ["005930"])
        self.assertEqual(us_symbols, ["AAPL", "MSFT"])

    def test_condition_monitor_cycle_queries_only_open_market_selection(self):
        kr_api = Mock()
        kr_api.get_volume_rank.return_value = ["005930"]
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "runtime.sqlite")
            with (
                patch("src.strategy.condition_monitor.runtime_state_store", store),
                patch("src.trader.KIStockAPI", return_value=kr_api),
                patch(
                    "src.strategy.seven_split._condition_search_universe",
                    return_value=[],
                ),
                patch("src.mistock.trader._get_kis_client") as us_client,
            ):
                result = run_condition_monitor_cycle({"KR"})

        self.assertTrue(result["ok"])
        kr_api.get_volume_rank.assert_called_once_with(top_n=50)
        us_client.assert_not_called()

    def test_walk_forward_models_costs_and_multiple_folds(self):
        prices = [100 + index * 0.2 + (index % 10) for index in range(140)]
        highs = [price * 1.01 for price in prices]
        volumes = [1000.0] * len(prices)

        def profile(p, _h, _v):
            return {
                "score": 5 if len(p) % 10 == 0 else 0,
                "sma_dead_cross": len(p) % 10 == 5,
            }

        result = run_technical_walk_forward(
            prices, highs, volumes, profile_builder=profile, folds=3
        )

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["folds"]), 3)
        self.assertTrue(result["costs"]["modeled"])
        self.assertGreaterEqual(result["metrics"]["trade_count"], 3)


if __name__ == "__main__":
    unittest.main()
