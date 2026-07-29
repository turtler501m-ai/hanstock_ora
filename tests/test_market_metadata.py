import unittest
from unittest.mock import patch

from src.market_metadata import (
    normalize_kr_symbol,
    normalize_kr_order_symbol,
    resolve_stock_name,
    resolve_stock_sector,
)


class MarketMetadataTests(unittest.TestCase):
    def test_normalize_kr_symbol_pads_numeric_codes(self):
        self.assertEqual(normalize_kr_symbol("5930"), "005930")
        self.assertEqual(normalize_kr_symbol("q530107"), "Q530107")

    def test_normalize_kr_order_symbol_removes_kis_market_prefix(self):
        self.assertEqual(normalize_kr_order_symbol("Q530107"), "530107")
        self.assertEqual(normalize_kr_order_symbol("005930"), "005930")

    def test_resolve_stock_name_replaces_placeholder_from_metadata(self):
        with patch(
            "src.market_metadata.load_kr_stock_metadata",
            return_value={"005930": {"name": "삼성전자", "sector": "반도체"}},
        ):
            self.assertEqual(resolve_stock_name("005930", "우량 종목"), "삼성전자")

    def test_resolve_stock_sector_uses_metadata_before_placeholder(self):
        with patch(
            "src.market_metadata.load_kr_stock_metadata",
            return_value={"005930": {"name": "삼성전자", "sector": "반도체"}},
        ):
            self.assertEqual(resolve_stock_sector("005930", "미분류"), "반도체")

    def test_resolve_stock_name_keeps_non_placeholder_fallback(self):
        with patch("src.market_metadata.load_kr_stock_metadata", return_value={}):
            self.assertEqual(resolve_stock_name("123456", "사용자입력명"), "사용자입력명")


if __name__ == "__main__":
    unittest.main()
