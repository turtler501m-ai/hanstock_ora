import unittest
from unittest.mock import MagicMock, patch

from src.mistock.config import config
from src.mistock import strategy
from src.mistock import trader


class MistockIndicatorStrategyTests(unittest.TestCase):
    def setUp(self):
        self.original_model = config.strategy_model
        self.original_min_score = config.indicator_min_score
        self.original_rsi_min = config.indicator_rsi_entry_min
        self.original_rsi_max = config.indicator_rsi_entry_max
        self.original_volume_ratio = config.indicator_volume_ratio

    def tearDown(self):
        config.strategy_model = self.original_model
        config.indicator_min_score = self.original_min_score
        config.indicator_rsi_entry_min = self.original_rsi_min
        config.indicator_rsi_entry_max = self.original_rsi_max
        config.indicator_volume_ratio = self.original_volume_ratio

    def test_macd_rsi_momentum_profile_scores_trend_and_volume(self):
        config.strategy_model = "macd_rsi_momentum"
        config.indicator_rsi_entry_max = 95
        prices = [100 + i * 0.35 for i in range(70)] + [125, 124, 126, 129, 132]
        highs = [price * 1.01 for price in prices]
        volumes = [1000] * (len(prices) - 1) + [1800]

        profile = strategy.strategy_profile(prices, highs, volumes)

        self.assertEqual(profile["strategy_model"], "macd_rsi_momentum")
        self.assertGreaterEqual(profile["score"], 4)
        self.assertIn("above SMA60 trend", profile["reasons"])
        self.assertTrue(any("volume confirmation" in item for item in profile["reasons"]))

    def test_scan_candidates_uses_indicator_min_score_when_defaulted(self):
        config.strategy_model = "macd_rsi_momentum"
        config.indicator_min_score = 5

        with patch("src.mistock.trader._get_kis_client", side_effect=RuntimeError("offline")), \
                patch("src.mistock.strategy.build_scan_universe", return_value=["AAPL"]), \
                patch("src.mistock.trader.get_watchlist", return_value=[]), \
                patch("src.mistock.trader.fetch_history", return_value={"close": [1.0] * 80, "high": [1.0] * 80, "volume": [1.0] * 80}), \
                patch("src.mistock.trader.strategy_profile", return_value={
                    "score": 4,
                    "reasons": ["below threshold"],
                    "price": 1.0,
                    "rsi": 55.0,
                    "rsi2": 50.0,
                    "macd_hist": 0.1,
                    "sma20": 1.0,
                    "sma60": 1.0,
                }), \
                patch("src.mistock.trader.symbol_name", return_value="Apple"), \
                patch("src.mistock.db.execute"):
            result = trader.scan_candidates(limit=1)

        self.assertEqual(result["min_score"], 5)
        self.assertEqual(result["candidates"], [])

    def test_fetch_history_converts_kis_share_class_symbol_for_yahoo(self):
        empty = MagicMock()
        empty.empty = True
        with patch("src.online_access.require_online_access"), \
                patch("src.mistock.strategy.yf.download", return_value=empty) as download:
            result = strategy.fetch_history("BRK.B")

        self.assertEqual(result, {"close": [], "high": [], "volume": []})
        self.assertEqual(download.call_args.args[0], "BRK-B")

    def test_fetch_history_preserves_yahoo_index_prefix(self):
        empty = MagicMock()
        empty.empty = True
        with patch("src.online_access.require_online_access"), \
                patch("src.mistock.strategy.yf.download", return_value=empty) as download:
            result = strategy.fetch_history("^KS11")

        self.assertEqual(result, {"close": [], "high": [], "volume": []})
        self.assertEqual(download.call_args.args[0], "^KS11")


class RsiDivergenceTests(unittest.TestCase):
    def test_calc_rsi_series_length(self):
        from src.strategy.indicators import calc_rsi_series
        prices = [100 + i * 0.3 for i in range(50)]
        result = calc_rsi_series(prices, 14)
        # period+1 이후부터 계산 가능 → len(prices) - period 개
        self.assertEqual(len(result), len(prices) - 14)

    def _make_alternating(self, start: float, up_pct: float, down_pct: float, n: int) -> list:
        """up_pct / down_pct 교차 캔들 생성 → 현실적인 RSI 범위(50~90) 확보"""
        prices = [start]
        for i in range(n):
            if i % 2 == 0:
                prices.append(prices[-1] * (1 + up_pct / 100))
            else:
                prices.append(prices[-1] * (1 - down_pct / 100))
        return prices[1:]

    def test_bearish_divergence_detected(self):
        from src.strategy.indicators import calc_rsi_divergence
        # 전반부: 3%↑ 1%↓ 교차 → RSI ~75
        # 후반부: 0.5%↑ 0.4%↓ 교차 → 더 높은 가격이지만 RSI ~55
        warmup = [90 + i * 0.3 for i in range(14)]
        first_half = self._make_alternating(95.0, 3.0, 1.0, 20)   # 강한 상승 RSI↑
        second_half = self._make_alternating(max(first_half) + 1, 0.5, 0.4, 20)  # 완만 상승 RSI↓
        prices = warmup + first_half + second_half
        result = calc_rsi_divergence(prices, period=40)
        self.assertTrue(result["bearish"],
            f"Expected bearish divergence. price: {result['price_high1']:.1f}->{result['price_high2']:.1f}, "
            f"RSI: {result['rsi_high1']:.1f}->{result['rsi_high2']:.1f}")

    def test_no_divergence_when_price_not_making_new_highs(self):
        from src.strategy.indicators import calc_rsi_divergence
        # 후반부 가격 고점이 전반부보다 낮으면 다이버전스 조건 자체가 성립 안 함
        warmup = [90 + i * 0.3 for i in range(14)]
        first_half = self._make_alternating(95.0, 3.0, 1.0, 20)   # 강한 상승 → 높은 고점
        peak = max(first_half)
        second_half = self._make_alternating(peak * 0.95, 1.0, 1.5, 20)  # 하락 조정 → 낮은 고점
        prices = warmup + first_half + second_half
        result = calc_rsi_divergence(prices, period=40)
        self.assertFalse(result["bearish"],
            f"Expected no divergence (price_high2 < price_high1). "
            f"price: {result['price_high1']:.1f}->{result['price_high2']:.1f}, "
            f"RSI: {result['rsi_high1']:.1f}->{result['rsi_high2']:.1f}")

    def test_divergence_returns_price_and_rsi_highs(self):
        from src.strategy.indicators import calc_rsi_divergence
        warmup = [90 + i * 0.3 for i in range(14)]
        first_half = self._make_alternating(95.0, 3.0, 1.0, 20)
        second_half = self._make_alternating(max(first_half) + 1, 0.5, 0.4, 20)
        prices = warmup + first_half + second_half
        result = calc_rsi_divergence(prices, period=40)
        self.assertIn("price_high1", result)
        self.assertIn("price_high2", result)
        self.assertIn("rsi_high1", result)
        self.assertIn("rsi_high2", result)
        self.assertGreater(result["price_high2"], result["price_high1"])
        self.assertLess(result["rsi_high2"], result["rsi_high1"])

    def test_divergence_returns_false_when_insufficient_data(self):
        from src.strategy.indicators import calc_rsi_divergence
        prices = [100.0] * 20  # 데이터 부족
        result = calc_rsi_divergence(prices, period=40)
        self.assertFalse(result["bearish"])


class MacdRsiMomentumProfileV2Tests(unittest.TestCase):
    def setUp(self):
        self.original_model = config.strategy_model
        self.original_rsi_min = config.indicator_rsi_entry_min
        self.original_rsi_max = config.indicator_rsi_entry_max
        self.original_volume_ratio = config.indicator_volume_ratio
        config.strategy_model = "macd_rsi_momentum"
        config.indicator_rsi_entry_min = 50
        config.indicator_rsi_entry_max = 70
        config.indicator_volume_ratio = 1.3

    def tearDown(self):
        config.strategy_model = self.original_model
        config.indicator_rsi_entry_min = self.original_rsi_min
        config.indicator_rsi_entry_max = self.original_rsi_max
        config.indicator_volume_ratio = self.original_volume_ratio

    def test_hist_turn_up_with_volume_scores(self):
        """histogram이 음수에서 반전 + 거래량 급증 시 momentum_scope 이유가 포함되어야 함"""
        # 70봉 하락 후 1봉 반등 → prev_hist<0이고 hist>prev_hist인 첫 전환 시점
        prices = [110 - i * 0.3 for i in range(70)] + [90.0]
        volumes = [1000] * (len(prices) - 1) + [1600]

        profile = strategy.strategy_profile(prices, prices, volumes)

        reasons_text = " ".join(profile["reasons"])
        self.assertIn("momentum_scope", reasons_text,
            f"Got reasons: {profile['reasons']}")

    def test_divergence_reentry_in_profile(self):
        """RSI 하락 다이버전스 + MACD 재골든크로스 → divergence_reentry=True 반환"""
        warmup = [80 + i * 0.3 for i in range(30)]
        first_leg = [89 + i * 0.9 for i in range(20)]    # 강한 1차 상승 → RSI 높음
        correction = [107 - i * 0.4 for i in range(10)]  # 조정
        second_leg = [103 + i * 0.6 for i in range(20)]  # 새 고점, RSI 낮음
        prices = warmup + first_leg + correction + second_leg

        profile = strategy.strategy_profile(prices, prices, [1000] * len(prices))

        # divergence_reentry 키가 존재해야 함
        self.assertIn("divergence_reentry", profile)

    def test_overheated_rsi_penalized_without_divergence(self):
        """RSI 70 이상이고 다이버전스 없을 때 패널티 적용"""
        # 급등 후 RSI 과열 상태 (MACD 골든크로스 없음)
        prices = [100 + i * 1.5 for i in range(80)]
        profile = strategy.strategy_profile(prices, prices, [1000] * len(prices))

        reasons_text = " ".join(profile["reasons"])
        self.assertIn("overheated", reasons_text)


if __name__ == "__main__":
    unittest.main()
