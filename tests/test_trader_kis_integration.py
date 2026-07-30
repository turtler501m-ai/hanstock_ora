import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from src import trader


class _FakeResponse:
    def __init__(self, payload=None, status_code=200, raise_error=None, headers=None):
        self._payload = payload or {}
        self.status_code = status_code
        self._raise_error = raise_error
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self._raise_error is not None:
            raise self._raise_error


class TraderKISIntegrationTests(unittest.TestCase):
    def setUp(self):
        trader.KIStockAPI.reset_circuit()

    def tearDown(self):
        trader.KIStockAPI.reset_circuit()

    def test_kistockapi_init_uses_matching_cached_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "kis_token.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "token": "cached-token",
                        "expires_at": "2099-01-01T00:00:00",
                        "trading_env": "demo",
                        "base_url": "https://example.test",
                        "app_key_prefix": "app-key-",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(trader, "TRADING_ENV", "demo"),
                patch.object(trader, "BASE_URL", "https://example.test"),
                patch.object(trader, "KISTOCK_APP_KEY", "app-key-12345678"),
                patch.object(trader.KIStockAPI, "TOKEN_CACHE", cache_path),
                patch.object(trader.HTTP, "post") as http_post,
            ):
                api = trader.KIStockAPI(notify_errors=False)

        self.assertEqual(api.access_token, "cached-token")
        http_post.assert_not_called()

    def test_kistockapi_init_builds_kis_client_with_trader_wiring(self):
        client_config = object()
        client = Mock()

        with (
            patch.object(trader, "build_kis_client_config", return_value=client_config),
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader, "KISClient", return_value=client) as kis_client_cls,
        ):
            api = trader.KIStockAPI(notify_errors=False)

        self.assertIs(api._client, client)
        self.assertIs(api.client_config, client_config)
        kis_client_cls.assert_called_once_with(client_config, session=trader.HTTP, access_token="token-abc")

    def test_real_check_config_uses_real_market_data_credentials_and_cache(self):
        with (
            patch.object(trader.config, "kis_real_check_app_key", "real-key-12345678"),
            patch.object(trader.config, "kis_real_check_app_secret", "real-secret"),
            patch.object(trader.config, "kis_real_check_account", "8765432101"),
        ):
            client_config = trader.build_kis_client_config(group="real_check")

        self.assertEqual(client_config.trading_env, "real")
        self.assertEqual(client_config.base_url, "https://openapi.koreainvestment.com:9443")
        self.assertEqual(client_config.app_key, "real-key-12345678")
        self.assertEqual(client_config.app_secret, "real-secret")
        self.assertEqual(client_config.account_no, "8765432101")
        self.assertEqual(client_config.token_cache_path, Path("data") / "kis_token_real_check.json")

    def test_build_market_data_api_returns_real_check_client_when_enabled(self):
        broker_api = Mock()
        real_check_api = Mock()

        with (
            patch.object(trader.config, "kis_real_check_enabled", True),
            patch.object(trader.config, "kis_real_check_app_key", "real-key-12345678"),
            patch.object(trader.config, "kis_real_check_app_secret", "real-secret"),
            patch.object(trader, "KIStockAPI", return_value=real_check_api) as api_cls,
        ):
            result = trader.build_market_data_api(broker_api)

        self.assertIs(result, real_check_api)
        api_cls.assert_called_once_with(notify_errors=False, group="real_check")

    def test_build_market_data_api_falls_back_when_real_check_credentials_missing(self):
        broker_api = Mock()

        with (
            patch.object(trader.config, "kis_real_check_enabled", True),
            patch.object(trader.config, "kis_real_check_app_key", ""),
            patch.object(trader.config, "kis_real_check_app_secret", ""),
            patch.object(trader, "KIStockAPI") as api_cls,
        ):
            result = trader.build_market_data_api(broker_api)

        self.assertIs(result, broker_api)
        api_cls.assert_not_called()

    def test_headers_delegate_to_client_and_sync_access_token(self):
        client = Mock()
        client.headers.return_value = {"tr_id": "VTTC8434R", "authorization": "Bearer fresh-token"}

        with (
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="stale-token"),
            patch.object(trader, "KISClient", return_value=client),
        ):
            api = trader.KIStockAPI(notify_errors=False)

        api.access_token = "fresh-token"
        headers = api._headers("VTTC8434R")

        self.assertEqual(headers["authorization"], "Bearer fresh-token")
        self.assertEqual(client.access_token, "fresh-token")
        client.headers.assert_called_once_with("VTTC8434R")

    def test_hashkey_delegates_to_client(self):
        client = Mock()
        client.create_hashkey.return_value = "hash-value"
        payload = {"PDNO": "005930", "ORD_QTY": "2"}

        with (
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader, "KISClient", return_value=client),
        ):
            api = trader.KIStockAPI(notify_errors=False)

        result = api._hashkey(payload)

        self.assertEqual(result, "hash-value")
        client.create_hashkey.assert_called_once_with(payload)

    def test_hashkey_returns_empty_string_when_client_raises(self):
        client = Mock()
        client.create_hashkey.side_effect = RuntimeError("boom")

        with (
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader, "KISClient", return_value=client),
        ):
            api = trader.KIStockAPI(notify_errors=False)

        self.assertEqual(api._hashkey({"PDNO": "005930"}), "")

    def test_get_balance_uses_expected_account_split_and_demo_tr_id(self):
        payload = {
            "rt_cd": "0",
            "output1": [{"pdno": "005930"}],
            "output2": [{"dnca_tot_amt": "1000000"}],
        }

        with (
            patch.object(trader, "TRADING_ENV", "demo"),
            patch.object(trader, "BASE_URL", "https://example.test"),
            patch.object(trader, "KISTOCK_APP_KEY", "app-key-12345678"),
            patch.object(trader, "KISTOCK_APP_SECRET", "secret-value"),
            patch.object(trader, "KISTOCK_ACCOUNT", "1234567801"),
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader.HTTP, "get", return_value=_FakeResponse(payload)) as http_get,
        ):
            api = trader.KIStockAPI(notify_errors=False)
            result = api.get_balance()

        self.assertEqual(result, payload)
        http_get.assert_called_once()
        call = http_get.call_args
        self.assertEqual(
            call.args[0],
            "https://example.test/uapi/domestic-stock/v1/trading/inquire-balance",
        )
        self.assertEqual(call.kwargs["headers"]["authorization"], "Bearer token-abc")
        self.assertEqual(call.kwargs["headers"]["tr_id"], "VTTC8434R")
        self.assertEqual(call.kwargs["params"]["CANO"], "12345678")
        self.assertEqual(call.kwargs["params"]["ACNT_PRDT_CD"], "01")

    def test_get_balance_routes_request_headers_through_client_delegate(self):
        payload = {"rt_cd": "0", "output1": [], "output2": [{}]}

        with (
            patch.object(trader, "TRADING_ENV", "real"),
            patch.object(trader, "BASE_URL", "https://example.test"),
            patch.object(trader, "KISTOCK_ACCOUNT", "1234567801"),
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader.HTTP, "get", return_value=_FakeResponse(payload)) as http_get,
            patch.object(trader.KIStockAPI, "_headers", return_value={"x-test-header": "delegated"}) as headers,
        ):
            api = trader.KIStockAPI(notify_errors=False)
            result = api.get_balance()

        self.assertEqual(result, payload)
        headers.assert_called_once_with("TTTC8434R")
        self.assertEqual(http_get.call_args.kwargs["headers"], {"x-test-header": "delegated"})

    def test_get_balance_http_error_does_not_expose_account_url(self):
        response = _FakeResponse(status_code=500)
        response.text = "Internal Server Error"

        with (
            patch.object(trader, "TRADING_ENV", "real"),
            patch.object(trader, "BASE_URL", "https://example.test"),
            patch.object(trader, "KISTOCK_ACCOUNT", "1234567801"),
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader.HTTP, "get", return_value=response),
        ):
            api = trader.KIStockAPI(notify_errors=False)
            with self.assertRaises(RuntimeError) as raised:
                api.get_balance()

        message = str(raised.exception)
        self.assertIn("KIS balance HTTP 500", message)
        self.assertNotIn("CANO=", message)
        self.assertNotIn("inquire-balance?", message)

    def test_get_daily_uses_domestic_market_division_for_stocks_and_etfs(self):
        response = _FakeResponse({"rt_cd": "0", "output2": [{"stck_bsop_date": "20260425"}]})

        with (
            patch.object(trader, "BASE_URL", "https://example.test"),
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader.HTTP, "get", side_effect=[response, response]) as http_get,
        ):
            api = trader.KIStockAPI(notify_errors=False)
            etf_daily = api.get_daily("102110", n=1)
            stock_daily = api.get_daily("005930", n=1)

        self.assertEqual(etf_daily, [{"stck_bsop_date": "20260425"}])
        self.assertEqual(stock_daily, [{"stck_bsop_date": "20260425"}])
        self.assertEqual(http_get.call_args_list[0].kwargs["params"]["FID_COND_MRKT_DIV_CODE"], "J")
        self.assertEqual(http_get.call_args_list[1].kwargs["params"]["FID_COND_MRKT_DIV_CODE"], "J")

    def test_get_quote_routes_request_headers_through_client_delegate(self):
        response = _FakeResponse({"output": {"stck_prpr": "70000", "askp1": "70100", "bidp1": "69900"}})

        with (
            patch.object(trader, "BASE_URL", "https://example.test"),
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader.HTTP, "get", return_value=response) as http_get,
            patch.object(trader.KIStockAPI, "_headers", return_value={"x-test-header": "delegated"}) as headers,
        ):
            api = trader.KIStockAPI(notify_errors=False)
            result = api.get_quote("005930")

        self.assertEqual(
            result,
            {"current": 70000.0, "ask1": 70100.0, "bid1": 69900.0, "market_cap": 0.0},
        )
        headers.assert_called_once_with("FHKST01010100")
        self.assertEqual(http_get.call_args.kwargs["headers"], {"x-test-header": "delegated"})

    def test_get_volume_rank_delegates_to_client_and_syncs_circuit_runtime(self):
        client = Mock()
        opened_at = datetime.now() - timedelta(seconds=11)
        client.circuit = SimpleNamespace(error_count=0, opened_at=None)

        def delegated_get_volume_rank(*, top_n):
            self.assertEqual(top_n, 7)
            self.assertEqual(client.circuit.error_count, 3)
            self.assertEqual(client.circuit.opened_at, opened_at)
            client.circuit.error_count = 0
            client.circuit.opened_at = None
            return ["005930", "000660"]

        client.get_volume_rank.side_effect = delegated_get_volume_rank

        with (
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader, "KISClient", return_value=client),
        ):
            api = trader.KIStockAPI(notify_errors=False)

        trader.KIStockAPI._err_count = 3
        trader.KIStockAPI._circuit_opened_at = opened_at

        result = api.get_volume_rank(top_n=7)

        self.assertEqual(result, ["005930", "000660"])
        client.get_volume_rank.assert_called_once_with(top_n=7)
        self.assertEqual(trader.KIStockAPI.circuit_status()["error_count"], 0)
        self.assertFalse(trader.KIStockAPI.circuit_status()["opened"])

    def test_get_daily_delegates_to_client_and_syncs_open_circuit_back_to_class(self):
        client = Mock()
        failure_opened_at = datetime.now() - timedelta(seconds=2)
        client.circuit = SimpleNamespace(error_count=0, opened_at=None)

        def delegated_get_daily(symbol, *, n):
            self.assertEqual(symbol, "102110")
            self.assertEqual(n, 3)
            self.assertEqual(client.circuit.error_count, 1)
            self.assertIsNone(client.circuit.opened_at)
            client.circuit.error_count = trader.KIStockAPI.MAX_ERRORS
            client.circuit.opened_at = failure_opened_at
            return [{"stck_bsop_date": "20260425"}]

        client.get_daily.side_effect = delegated_get_daily

        with (
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader, "KISClient", return_value=client),
        ):
            api = trader.KIStockAPI(notify_errors=False)

        trader.KIStockAPI._err_count = 1
        trader.KIStockAPI._circuit_opened_at = None

        result = api.get_daily("102110", n=3)

        self.assertEqual(result, [{"stck_bsop_date": "20260425"}])
        client.get_daily.assert_called_once_with("102110", n=3)
        status = trader.KIStockAPI.circuit_status()
        self.assertTrue(status["opened"])
        self.assertEqual(status["error_count"], trader.KIStockAPI.MAX_ERRORS)
        self.assertEqual(status["opened_at"], failure_opened_at.isoformat())

    def test_place_order_uses_live_sell_tr_id_and_hashkey(self):
        with (
            patch.object(trader, "ORDER_SUBMISSION_ENABLED", True),
            patch.object(trader, "TRADING_ENV", "real"),
            patch.object(trader, "BASE_URL", "https://example.test"),
            patch.object(trader, "KISTOCK_APP_KEY", "app-key-12345678"),
            patch.object(trader, "KISTOCK_APP_SECRET", "secret-value"),
            patch.object(trader, "KISTOCK_ACCOUNT", "1234567801"),
            patch.object(trader, "_KIS_ORDER_MIN_INTERVAL_SECONDS", 0),
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader.KIStockAPI, "_hashkey", return_value="hash-value"),
            patch.object(trader.HTTP, "post", return_value=_FakeResponse({"rt_cd": "0", "msg1": "ok"})) as http_post,
        ):
            api = trader.KIStockAPI(notify_errors=False)
            result = api.place_order("005930", "sell", 70000, 2)

        self.assertEqual(result["rt_cd"], "0")
        http_post.assert_called_once()
        call = http_post.call_args
        self.assertEqual(
            call.args[0],
            "https://example.test/uapi/domestic-stock/v1/trading/order-cash",
        )
        self.assertEqual(call.kwargs["headers"]["tr_id"], "TTTC0801U")
        self.assertEqual(call.kwargs["headers"]["hashkey"], "hash-value")
        self.assertEqual(call.kwargs["json"]["CANO"], "12345678")
        self.assertEqual(call.kwargs["json"]["ACNT_PRDT_CD"], "01")
        self.assertEqual(call.kwargs["json"]["ORD_DVSN"], "00")
        self.assertEqual(call.kwargs["json"]["ORD_QTY"], "2")
        self.assertEqual(call.kwargs["json"]["ORD_UNPR"], "70000")

    def test_place_order_routes_headers_and_hashkey_through_trader_helpers(self):
        delegated_headers = {"authorization": "Bearer token-abc", "tr_id": "VTTC0802U"}

        with (
            patch.object(trader, "ORDER_SUBMISSION_ENABLED", True),
            patch.object(trader, "TRADING_ENV", "demo"),
            patch.object(trader, "BASE_URL", "https://example.test"),
            patch.object(trader, "KISTOCK_ACCOUNT", "1234567801"),
            patch.object(trader, "_KIS_ORDER_MIN_INTERVAL_SECONDS", 0),
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader.KIStockAPI, "_headers", return_value=delegated_headers.copy()) as headers,
            patch.object(trader.KIStockAPI, "_hashkey", return_value="hash-value") as hashkey,
            patch.object(trader.HTTP, "post", return_value=_FakeResponse({"rt_cd": "0"})) as http_post,
        ):
            api = trader.KIStockAPI(notify_errors=False)
            result = api.place_order("005930", "buy", 0, 3)

        self.assertEqual(result, {"rt_cd": "0"})
        headers.assert_called_once_with("VTTC0802U")
        hashkey.assert_called_once_with({
            "CANO": "12345678",
            "ACNT_PRDT_CD": "01",
            "PDNO": "005930",
            "ORD_DVSN": "01",
            "ORD_QTY": "3",
            "ORD_UNPR": "0",
        })
        http_post.assert_called_once_with(
            "https://example.test/uapi/domestic-stock/v1/trading/order-cash",
            headers={"authorization": "Bearer token-abc", "tr_id": "VTTC0802U", "hashkey": "hash-value"},
            json=ANY,
            timeout=15,
        )

    def test_trade_history_uses_current_demo_tr_id_and_exchange_param(self):
        with (
            patch.object(trader, "TRADING_ENV", "demo"),
            patch.object(trader, "BASE_URL", "https://example.test"),
            patch.object(trader, "KISTOCK_ACCOUNT", "1234567801"),
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader.KIStockAPI, "_headers", return_value={"tr_id": "VTTC0081R"}) as headers,
            patch.object(trader.HTTP, "get", return_value=_FakeResponse({"output1": [{"odno": "D12345"}]})) as http_get,
        ):
            api = trader.KIStockAPI(notify_errors=False)
            rows = api.get_trade_history("20260523", "20260524")

        self.assertEqual(rows, [{"odno": "D12345"}])
        headers.assert_called_once_with("VTTC0081R")
        params = http_get.call_args.kwargs["params"]
        self.assertEqual(params["CCLD_DVSN"], "00")
        self.assertEqual(params["EXCG_ID_DVSN_CD"], "KRX")

    def test_trade_history_follows_continuation_pages(self):
        responses = [
            _FakeResponse(
                {
                    "output1": [{"odno": "D12345"}],
                    "ctx_area_fk100": "next-fk",
                    "ctx_area_nk100": "next-nk",
                },
                headers={"tr_cont": "M"},
            ),
            _FakeResponse({"output1": [{"odno": "D67890"}]}, headers={"tr_cont": ""}),
        ]
        with (
            patch.object(trader, "TRADING_ENV", "demo"),
            patch.object(trader, "BASE_URL", "https://example.test"),
            patch.object(trader, "KISTOCK_ACCOUNT", "1234567801"),
            patch.object(trader.KIStockAPI, "_load_or_fetch_token", return_value="token-abc"),
            patch.object(trader.KIStockAPI, "_headers", return_value={"tr_id": "VTTC0081R"}),
            patch.object(trader, "_kis_order_throttle"),
            patch.object(trader.HTTP, "get", side_effect=responses) as http_get,
        ):
            api = trader.KIStockAPI(notify_errors=False)
            rows = api.get_trade_history("20260501", "20260524")

        self.assertEqual(rows, [{"odno": "D12345"}, {"odno": "D67890"}])
        self.assertEqual(http_get.call_count, 2)
        second_params = http_get.call_args_list[1].kwargs["params"]
        self.assertEqual(second_params["CTX_AREA_FK100"], "next-fk")
        self.assertEqual(second_params["CTX_AREA_NK100"], "next-nk")

    def test_build_runtime_plan_works_with_api_public_methods_only(self):
        api = Mock()
        api.get_daily.return_value = []
        api.get_quote.return_value = {"current": 70000, "ask1": 70000, "bid1": 69900}
        balance = {
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "Samsung",
                    "evlu_pfls_rt": "6.0",
                }
            ],
            "output2": [
                {
                    "dnca_tot_amt": "1000000",
                    "tot_evlu_amt": "2000000",
                    "evlu_pfls_smtl_amt": "50000",
                }
            ],
        }

        with (
            patch("src.trader.generate_signal", return_value={
                "action": "sell",
                "qty": 1,
                "price": 71000,
                "reason": "trim winner",
                "indicators": {"rsi": 74},
            }),
            patch("src.trader.build_scan_universe", return_value=["000660"]),
            patch("src.trader.find_candidates", return_value={
                "candidates": [{"ticker": "000660", "name": "SK Hynix", "score": 4, "reasons": ["rsi"]}],
                "scan_summary": [{"ticker": "000660", "score": 4, "rsi": 28}],
                "scanned": 1,
                "min_score": 2,
                "scan_error": None,
            }),
            patch("src.trader.build_orders", return_value=[
                {
                    "ticker": "000660",
                    "quantity": 3,
                    "limit_price": 120000,
                    "estimated_cost": 360000,
                    "score": 4,
                    "reasons": ["rsi"],
                }
            ]),
        ):
            bundle = trader.build_runtime_plan(api, balance)

        self.assertEqual([row["category"] for row in bundle["plan"]], ["position", "candidate"])
        self.assertEqual([row["symbol"] for row in bundle["plan"]], ["005930", "000660"])
        self.assertEqual(api.get_daily.call_args.args[0], "005930")
        api.get_quote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
