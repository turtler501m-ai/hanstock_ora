import unittest
from unittest.mock import Mock, patch

import src.api.kis_api as kis_api
from src.api.kis_api import KIStockAPI, KISAccountError, KISRateLimitError


class KIStockAPITests(unittest.TestCase):
    def test_balance_rate_limit_retries(self):
        api = KIStockAPI.__new__(KIStockAPI)
        api.base_url = "https://example.test"
        api.access_token = "token"

        response = Mock()
        response.status_code = 500
        response.text = '{"msg_cd":"EGW00201","msg1":"rate limit"}'
        response.json.return_value = {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate limit"}

        original_interval = kis_api._KIS_MIN_INTERVAL
        try:
            kis_api._KIS_MIN_INTERVAL = 0
            with patch.object(kis_api.config, "kistock_account", "1234567801"), \
                    patch.object(kis_api.config, "trading_env", "demo"), \
                    patch.object(kis_api.config, "kistock_app_key", "key"), \
                    patch.object(kis_api.config, "kistock_app_secret", "secret"), \
                    patch.object(kis_api.logger, "error"), \
                    patch.object(kis_api.HTTP, "get", return_value=response) as get:
                with self.assertRaises(KISRateLimitError):
                    api.get_balance()

                self.assertEqual(get.call_count, 5)
        finally:
            kis_api._KIS_MIN_INTERVAL = original_interval

    def test_balance_invalid_account_does_not_retry(self):
        api = KIStockAPI.__new__(KIStockAPI)
        api.base_url = "https://example.test"
        api.access_token = "token"

        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "APBK0917",
            "msg1": "ERROR : INPUT INVALID_CHECK_ACNO",
        }

        original_interval = kis_api._KIS_MIN_INTERVAL
        try:
            kis_api._KIS_MIN_INTERVAL = 0
            with patch.object(kis_api.config, "kistock_account", "1234567801"), \
                    patch.object(kis_api.config, "trading_env", "demo"), \
                    patch.object(kis_api.config, "kistock_app_key", "key"), \
                    patch.object(kis_api.config, "kistock_app_secret", "secret"), \
                    patch.object(kis_api.HTTP, "get", return_value=response) as get:
                with self.assertRaises(KISAccountError):
                    api.get_balance()

                self.assertEqual(get.call_count, 1)
        finally:
            kis_api._KIS_MIN_INTERVAL = original_interval

    def test_demo_trade_history_uses_current_tr_id_and_order_status_params(self):
        api = KIStockAPI.__new__(KIStockAPI)
        api.base_url = "https://example.test"
        api.access_token = "token"

        response = Mock()
        response.status_code = 200
        response.json.return_value = {"rt_cd": "0", "output1": [{"odno": "D12345"}]}

        original_interval = kis_api._KIS_MIN_INTERVAL
        try:
            kis_api._KIS_MIN_INTERVAL = 0
            with patch.object(kis_api.config, "kistock_account", "1234567801"), \
                    patch.object(kis_api.config, "trading_env", "demo"), \
                    patch.object(kis_api.HTTP, "get", return_value=response) as get:
                rows = api.get_trade_history("20260523", "20260524")

            self.assertEqual(rows, [{"odno": "D12345"}])
            headers = get.call_args.kwargs["headers"]
            params = get.call_args.kwargs["params"]
            self.assertEqual(headers["tr_id"], "VTTC0081R")
            self.assertEqual(params["CCLD_DVSN"], "00")
            self.assertEqual(params["EXCG_ID_DVSN_CD"], "KRX")
        finally:
            kis_api._KIS_MIN_INTERVAL = original_interval

    def test_trade_history_follows_continuation_pages(self):
        api = KIStockAPI.__new__(KIStockAPI)
        api.base_url = "https://example.test"
        api.access_token = "token"

        first = Mock()
        first.status_code = 200
        first.headers = {"tr_cont": "M"}
        first.json.return_value = {
            "rt_cd": "0",
            "output1": [{"odno": "D12345"}],
            "ctx_area_fk100": "next-fk",
            "ctx_area_nk100": "next-nk",
        }
        second = Mock()
        second.status_code = 200
        second.headers = {}
        second.json.return_value = {"rt_cd": "0", "output1": [{"odno": "D67890"}]}

        original_interval = kis_api._KIS_MIN_INTERVAL
        try:
            kis_api._KIS_MIN_INTERVAL = 0
            with patch.object(kis_api.config, "kistock_account", "1234567801"), \
                    patch.object(kis_api.config, "trading_env", "demo"), \
                    patch.object(kis_api.HTTP, "get", side_effect=[first, second]) as get:
                rows = api.get_trade_history("20260501", "20260524")

            self.assertEqual(rows, [{"odno": "D12345"}, {"odno": "D67890"}])
            self.assertEqual(get.call_count, 2)
            second_params = get.call_args_list[1].kwargs["params"]
            self.assertEqual(second_params["CTX_AREA_FK100"], "next-fk")
            self.assertEqual(second_params["CTX_AREA_NK100"], "next-nk")
        finally:
            kis_api._KIS_MIN_INTERVAL = original_interval

    def test_place_order_attaches_hashkey_when_available(self):
        api = KIStockAPI.__new__(KIStockAPI)
        api.base_url = "https://example.test"
        api.access_token = "token"

        response = Mock()
        response.status_code = 200
        response.json.return_value = {"rt_cd": "0", "msg1": "ok"}

        original_interval = kis_api._KIS_MIN_INTERVAL
        try:
            kis_api._KIS_MIN_INTERVAL = 0
            with patch.object(kis_api.config, "dry_run", False), \
                    patch.object(kis_api.config, "trading_env", "demo"), \
                    patch.object(kis_api.config, "enable_live_trading", False), \
                    patch.object(kis_api.config, "kistock_account", "1234567801"), \
                    patch.object(kis_api.config, "kistock_app_key", "key"), \
                    patch.object(kis_api.config, "kistock_app_secret", "secret"), \
                    patch.object(api, "_hashkey", return_value="hash-value") as hashkey, \
                    patch.object(kis_api.HTTP, "post", return_value=response) as post:
                result = api.place_order("005930", "buy", 0, 3)

            self.assertEqual(result["rt_cd"], "0")
            hashkey.assert_called_once_with({
                "CANO": "12345678",
                "ACNT_PRDT_CD": "01",
                "PDNO": "005930",
                "ORD_DVSN": "01",
                "ORD_QTY": "3",
                "ORD_UNPR": "0",
            })
            self.assertEqual(post.call_args.kwargs["headers"]["hashkey"], "hash-value")
        finally:
            kis_api._KIS_MIN_INTERVAL = original_interval

    def test_cancel_order_uses_demo_cash_cancel_contract(self):
        api = KIStockAPI.__new__(KIStockAPI)
        api.base_url = "https://example.test"
        api.access_token = "token"
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"rt_cd": "0", "msg1": "canceled"}

        with patch.object(kis_api.config, "dry_run", False), \
                patch.object(kis_api.config, "trading_env", "demo"), \
                patch.object(kis_api.config, "enable_live_trading", False), \
                patch.object(kis_api.config, "kistock_account", "1234567801"), \
                patch.object(api, "_hashkey", return_value="hash"), \
                patch.object(kis_api.HTTP, "post", return_value=response) as post:
            result = api.cancel_order("D12345", qty=2)

        self.assertEqual(result["rt_cd"], "0")
        self.assertEqual(post.call_args.kwargs["headers"]["tr_id"], "VTTC0803U")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["ORGN_ODNO"], "D12345")
        self.assertEqual(body["RVSE_CNCL_DVSN_CD"], "02")
        self.assertEqual(body["QTY_ALL_ORD_YN"], "Y")

    def test_order_snapshot_normalizes_partial_fill(self):
        api = KIStockAPI.__new__(KIStockAPI)
        api.get_trade_history = Mock(return_value=[{
            "odno": "D12345",
            "ord_qty": "5",
            "tot_ccld_qty": "2",
            "rmn_qty": "3",
            "avg_prvs": "70100",
        }])

        result = api.get_order_snapshot("D12345", order_date="20260724")

        self.assertEqual(result["status"], "partially_filled")
        self.assertEqual(result["cumulative_filled_qty"], 2)
        self.assertEqual(result["average_fill_price"], 70100)


if __name__ == "__main__":
    unittest.main()
