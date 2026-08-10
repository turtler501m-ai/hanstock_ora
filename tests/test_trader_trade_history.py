import unittest
from unittest.mock import Mock, patch

from src import trader


class _HistoryResponse:
    def __init__(self, token: str, *, tr_cont: str = "M", row_id: str = "1"):
        self.headers = {"tr_cont": tr_cont}
        self._payload = {
            "output1": [{"odno": row_id}],
            "ctx_area_fk100": "fk",
            "ctx_area_nk100": token,
        }

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class TraderTradeHistoryTests(unittest.TestCase):
    def _api(self):
        api = object.__new__(trader.KIStockAPI)
        api.account_no = "1234567801"
        api.trading_env = "demo"
        api.base_url = "https://example.test"
        api._headers = Mock(return_value={})
        return api

    def test_trade_history_uses_read_throttle_and_stops_on_repeated_token(self):
        responses = [
            _HistoryResponse("next", row_id="1"),
            _HistoryResponse("next", row_id="2"),
        ]
        with patch.object(trader.HTTP, "get", side_effect=responses) as get, patch.object(
            trader, "_kis_throttle"
        ) as read_throttle, patch.object(
            trader, "_kis_order_throttle"
        ) as order_throttle:
            rows = self._api().get_trade_history("20260801", "20260810")

        self.assertEqual([row["odno"] for row in rows], ["1", "2"])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(read_throttle.call_count, 2)
        order_throttle.assert_not_called()

    def test_trade_history_honors_page_limit(self):
        responses = [
            _HistoryResponse("one", row_id="1"),
            _HistoryResponse("two", row_id="2"),
        ]
        with patch.dict("os.environ", {"KIS_TRADE_HISTORY_MAX_PAGES": "2"}), patch.object(
            trader.HTTP, "get", side_effect=responses
        ) as get, patch.object(trader, "_kis_throttle"):
            rows = self._api().get_trade_history("20260801", "20260810")

        self.assertEqual(len(rows), 2)
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
