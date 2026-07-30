import unittest

from src.strategy.watchlist_policy import (
    eligibility_reason,
    filter_registered_items,
)


class WatchlistPolicyTests(unittest.TestCase):
    def test_rejects_stock_below_5000_won(self):
        self.assertIsNotNone(
            eligibility_reason(price=4999, market_cap=1_000_000_000_000)
        )

    def test_rejects_small_cap_stock(self):
        self.assertIsNotNone(
            eligibility_reason(price=5000, market_cap=299_999_999_999)
        )

    def test_accepts_known_mid_large_stock_when_market_cap_is_unavailable(self):
        self.assertIsNone(
            eligibility_reason(price=5000, market_cap=None, known_mid_large=True)
        )

    def test_ai_universe_keeps_registered_symbols_only(self):
        items = [{"symbol": "005930"}, {"symbol": "000660"}]
        self.assertEqual(
            filter_registered_items(items, ["000660"]),
            [{"symbol": "000660"}],
        )


if __name__ == "__main__":
    unittest.main()
