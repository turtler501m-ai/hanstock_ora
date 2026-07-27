import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from src.kis_client import CircuitBreakerState, KISClient, KISClientConfig


class _FakeResponse:
    def __init__(self, payload=None, status_code=200, raise_error=None, text=""):
        self._payload = payload or {}
        self.status_code = status_code
        self._raise_error = raise_error
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self._raise_error is not None:
            raise self._raise_error


class KISClientTests(unittest.TestCase):
    def make_config(self, **overrides):
        config = {
            "base_url": "https://example.test",
            "app_key": "app-key-12345678",
            "app_secret": "secret-value",
            "account_no": "1234567801",
            "trading_env": "demo",
            "circuit_cooldown_seconds": 60,
            "circuit_max_errors": 5,
        }
        config.update(overrides)
        return KISClientConfig(**config)

    def test_headers_include_auth_credentials_and_tr_id(self):
        client = KISClient(
            self.make_config(),
            session=Mock(),
            access_token="token-abc",
        )

        headers = client.headers("VTTC8434R")

        self.assertEqual(headers["authorization"], "Bearer token-abc")
        self.assertEqual(headers["appkey"], "app-key-12345678")
        self.assertEqual(headers["appsecret"], "secret-value")
        self.assertEqual(headers["tr_id"], "VTTC8434R")
        self.assertEqual(headers["custtype"], "P")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_uses_cached_token_when_cache_matches_and_not_near_expiry(self):
        now = datetime(2026, 4, 26, 10, 0, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "kis_token.json"
            cache_path.write_text(
                (
                    '{"token": "cached-token", '
                    '"expires_at": "2026-04-26T11:00:00", '
                    '"trading_env": "demo", '
                    '"base_url": "https://example.test", '
                    '"app_key_prefix": "app-key-"}'
                ),
                encoding="utf-8",
            )
            session = Mock()
            client = KISClient(
                self.make_config(token_cache_path=cache_path),
                session=session,
                clock=lambda: now,
            )

        self.assertEqual(client.access_token, "cached-token")
        session.post.assert_not_called()

    def test_fetches_token_when_cache_is_expiring_soon(self):
        now = datetime(2026, 4, 26, 10, 0, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "kis_token.json"
            cache_path.write_text(
                (
                    '{"token": "stale-token", '
                    '"expires_at": "2026-04-26T10:03:00", '
                    '"trading_env": "demo", '
                    '"base_url": "https://example.test", '
                    '"app_key_prefix": "app-key-"}'
                ),
                encoding="utf-8",
            )
            session = Mock()
            session.post.return_value = _FakeResponse({"access_token": "fresh-token"})
            client = KISClient(
                self.make_config(token_cache_path=cache_path),
                session=session,
                clock=lambda: now,
            )

            rewritten = cache_path.read_text(encoding="utf-8")

        self.assertEqual(client.access_token, "fresh-token")
        self.assertIn('"token": "fresh-token"', rewritten)
        session.post.assert_called_once()

    def test_circuit_breaker_blocks_until_cooldown_then_resets(self):
        state = CircuitBreakerState(error_count=5, opened_at=datetime(2026, 4, 26, 10, 0, 0))

        with self.assertRaisesRegex(RuntimeError, "retry after 30s"):
            state.ensure_can_proceed(
                datetime(2026, 4, 26, 10, 0, 30),
                max_errors=5,
                cooldown_seconds=60,
            )

        state.ensure_can_proceed(
            datetime(2026, 4, 26, 10, 1, 1),
            max_errors=5,
            cooldown_seconds=60,
        )
        self.assertEqual(state.error_count, 0)
        self.assertIsNone(state.opened_at)

    def test_circuit_status_auto_clears_after_cooldown(self):
        client = KISClient(
            self.make_config(),
            session=Mock(),
            clock=lambda: datetime(2026, 4, 26, 10, 2, 0),
            access_token="token",
            circuit=CircuitBreakerState(
                error_count=5,
                opened_at=datetime(2026, 4, 26, 10, 0, 0),
            ),
        )

        status = client.circuit_status()

        self.assertFalse(status["opened"])
        self.assertEqual(status["error_count"], 0)
        self.assertIsNone(status["opened_at"])

    def test_create_hashkey_returns_empty_string_on_failure(self):
        session = Mock()
        session.post.side_effect = RuntimeError("hashkey error")
        client = KISClient(
            self.make_config(),
            session=session,
            access_token="token",
        )

        value = client.create_hashkey({"PDNO": "005930"})

        self.assertEqual(value, "")

    def test_mark_failure_opens_circuit_at_threshold(self):
        timestamps = iter(
            [
                datetime(2026, 4, 26, 9, 0, 0),
                datetime(2026, 4, 26, 9, 0, 1),
                datetime(2026, 4, 26, 9, 0, 2),
                datetime(2026, 4, 26, 9, 0, 3),
                datetime(2026, 4, 26, 9, 0, 4),
            ]
        )
        client = KISClient(
            self.make_config(),
            session=Mock(),
            access_token="token",
            clock=lambda: next(timestamps),
        )

        for _ in range(5):
            client.mark_failure()

        self.assertEqual(client.circuit.error_count, 5)
        self.assertEqual(client.circuit.opened_at, datetime(2026, 4, 26, 9, 0, 4))

    def test_mark_failure_logs_unknown_message_when_error_is_empty(self):
        client = KISClient(
            self.make_config(),
            session=Mock(),
            access_token="token",
        )

        with patch("src.utils.logger.logger.error") as log_error:
            client.mark_failure("")

        self.assertEqual(client.circuit.error_count, 1)
        self.assertIn("unknown KIS API failure", log_error.call_args.args[0])

    def test_get_volume_rank_logs_http_failure_detail(self):
        session = Mock()
        session.get.return_value = _FakeResponse(status_code=500, text="server unavailable")
        client = KISClient(
            self.make_config(),
            session=session,
            access_token="token",
        )

        with patch("src.utils.logger.logger.error") as log_error:
            result = client.get_volume_rank(top_n=5)

        self.assertEqual(result, [])
        self.assertIn("Volume rank HTTP 500: server unavailable", log_error.call_args.args[0])

    def test_get_volume_rank_logs_kis_failure_detail(self):
        session = Mock()
        session.get.return_value = _FakeResponse(
            {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "temporary unavailable"},
            status_code=200,
        )
        client = KISClient(
            self.make_config(),
            session=session,
            access_token="token",
        )

        with patch("src.utils.logger.logger.error") as log_error:
            result = client.get_volume_rank(top_n=5)

        self.assertEqual(result, [])
        self.assertIn(
            "Volume rank KIS rt_cd=1 msg_cd=EGW00123 msg1=temporary unavailable",
            log_error.call_args.args[0],
        )

    def test_get_volume_rank_uses_volume_rank_tr_id(self):
        session = Mock()
        session.get.return_value = _FakeResponse(
            {"rt_cd": "0", "output": [{"mksc_shrn_iscd": "005930"}]},
            status_code=200,
        )
        client = KISClient(
            self.make_config(),
            session=session,
            access_token="token",
        )

        result = client.get_volume_rank(top_n=5)

        self.assertEqual(result, ["005930"])
        self.assertEqual(session.get.call_args.kwargs["headers"]["tr_id"], "FHPST01710000")
        self.assertEqual(session.get.call_args.kwargs["params"]["FID_COND_MRKT_DIV_CODE"], "J")
        self.assertNotIn("FID_COND_MRK_DIV_CODE", session.get.call_args.kwargs["params"])

    def test_get_daily_logs_http_failure_detail(self):
        session = Mock()
        session.get.return_value = _FakeResponse(status_code=500, text="server unavailable")
        client = KISClient(
            self.make_config(),
            session=session,
            access_token="token",
        )

        with patch("src.utils.logger.logger.error") as log_error:
            result = client.get_daily("005930", n=5)

        self.assertEqual(result, [])
        self.assertIn(
            "Daily chart HTTP 500 symbol=005930: server unavailable",
            log_error.call_args.args[0],
        )

    def test_get_daily_logs_kis_failure_detail(self):
        session = Mock()
        session.get.return_value = _FakeResponse(
            {"rt_cd": "1", "msg_cd": "MCA00001", "msg1": "invalid symbol"},
            status_code=200,
        )
        client = KISClient(
            self.make_config(),
            session=session,
            access_token="token",
        )

        with patch("src.utils.logger.logger.error") as log_error:
            result = client.get_daily("Q530107", n=5)

        self.assertEqual(result, [])
        self.assertIn(
            "Daily chart KIS symbol=Q530107 rt_cd=1 msg_cd=MCA00001 msg1=invalid symbol",
            log_error.call_args.args[0],
        )

    def test_place_order_uses_live_tr_id_for_live_environment(self):
        session = Mock()
        session.post.side_effect = [
            _FakeResponse({"HASH": "hash-value"}),
            _FakeResponse({"rt_cd": "0", "msg1": "ok"}),
        ]
        client = KISClient(
            self.make_config(trading_env="live"),
            session=session,
            access_token="token",
        )

        result = client.place_order("005930", "sell", 70000, 1)

        self.assertEqual(result["rt_cd"], "0")
        order_call = session.post.call_args_list[1]
        self.assertEqual(order_call.kwargs["headers"]["tr_id"], "TTTC0801U")
        self.assertEqual(order_call.kwargs["headers"]["hashkey"], "hash-value")

    def test_cancel_domestic_order_uses_revise_cancel_endpoint_and_exchange(self):
        session = Mock()
        session.post.side_effect = [
            _FakeResponse({"HASH": "hash-value"}),
            _FakeResponse({"rt_cd": "0", "msg1": "ok"}),
        ]
        client = KISClient(
            self.make_config(),
            session=session,
            access_token="token",
        )

        result = client.cancel_domestic_order("OD123", exchange_id="NXT")

        self.assertEqual(result["rt_cd"], "0")
        order_call = session.post.call_args_list[1]
        self.assertEqual(order_call.args[0], "https://example.test/uapi/domestic-stock/v1/trading/order-rvsecncl")
        self.assertEqual(order_call.kwargs["headers"]["tr_id"], "VTTC0803U")
        self.assertEqual(order_call.kwargs["headers"]["hashkey"], "hash-value")
        self.assertEqual(order_call.kwargs["json"]["ORGN_ODNO"], "OD123")
        self.assertEqual(order_call.kwargs["json"]["RVSE_CNCL_DVSN_CD"], "02")
        self.assertEqual(order_call.kwargs["json"]["QTY_ALL_ORD_YN"], "Y")
        self.assertEqual(order_call.kwargs["json"]["EXCG_ID_DVSN_CD"], "NXT")

    def test_revise_domestic_order_uses_revise_cancel_endpoint(self):
        session = Mock()
        session.post.side_effect = [
            _FakeResponse({"HASH": "hash-value"}),
            _FakeResponse({"rt_cd": "0", "msg1": "ok"}),
        ]
        client = KISClient(
            self.make_config(),
            session=session,
            access_token="token",
        )

        result = client.revise_domestic_order("OD123", qty=2, price=71000)

        self.assertEqual(result["rt_cd"], "0")
        body = session.post.call_args_list[1].kwargs["json"]
        self.assertEqual(body["RVSE_CNCL_DVSN_CD"], "01")
        self.assertEqual(body["ORD_QTY"], "2")
        self.assertEqual(body["ORD_UNPR"], "71000")

    def test_parse_us_symbol_uses_explicit_exchange_map(self):
        client = KISClient(
            self.make_config(),
            session=Mock(),
            access_token="token",
        )

        with patch.dict("os.environ", {"MISTOCK_EXCHANGE_MAP": "BRK.B=NYSE;XYZ=AMEX"}):
            self.assertEqual(client._parse_us_symbol("BRK.B"), ("BRK.B", "NYS", "NYSE"))
            self.assertEqual(client._parse_us_symbol("XYZ"), ("XYZ", "AMS", "AMEX"))

    def test_place_overseas_order_uses_mapped_exchange_code(self):
        session = Mock()
        session.post.side_effect = [
            _FakeResponse({"HASH": "hash-value"}),
            _FakeResponse({"rt_cd": "0", "msg1": "ok"}),
        ]
        client = KISClient(
            self.make_config(),
            session=session,
            access_token="token",
        )

        with patch.dict("os.environ", {"MISTOCK_EXCHANGE_MAP": "BRK.B=NYSE"}):
            result = client.place_overseas_order("BRK.B", "buy", 420.25, 2)

        self.assertEqual(result["rt_cd"], "0")
        order_call = session.post.call_args_list[1]
        self.assertEqual(order_call.kwargs["headers"]["tr_id"], "VTTT1002U")
        self.assertEqual(order_call.kwargs["headers"]["hashkey"], "hash-value")
        self.assertEqual(order_call.kwargs["json"]["OVRS_EXCG_CD"], "NYSE")
        self.assertEqual(order_call.kwargs["json"]["PDNO"], "BRK.B")
        self.assertEqual(order_call.kwargs["json"]["ORD_SVR_DVSN_CD"], "0")

    def test_place_overseas_order_preserves_kis_error_payload(self):
        session = Mock()
        session.post.side_effect = [
            _FakeResponse({"HASH": "hash-value"}),
            _FakeResponse(
                {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "mock trading rejected"},
                status_code=500,
            ),
        ]
        client = KISClient(
            self.make_config(),
            session=session,
            access_token="token",
        )

        result = client.place_overseas_order("AAPL", "buy", 123.45, 1)

        self.assertEqual(result["rt_cd"], "1")
        self.assertEqual(result["msg_cd"], "EGW00123")
        self.assertEqual(result["msg1"], "mock trading rejected")
        self.assertEqual(result["status_code"], 500)
        self.assertEqual(result["request"]["tr_id"], "VTTT1002U")
        self.assertEqual(result["request"]["ORD_SVR_DVSN_CD"], "0")

    def test_cancel_overseas_order_uses_revise_cancel_endpoint(self):
        session = Mock()
        session.post.side_effect = [
            _FakeResponse({"HASH": "hash-value"}),
            _FakeResponse({"rt_cd": "0", "msg1": "ok"}),
        ]
        client = KISClient(
            self.make_config(),
            session=session,
            access_token="token",
        )

        result = client.cancel_overseas_order("AAPL", "OD987")

        self.assertEqual(result["rt_cd"], "0")
        order_call = session.post.call_args_list[1]
        self.assertEqual(order_call.args[0], "https://example.test/uapi/overseas-stock/v1/trading/order-rvsecncl")
        self.assertEqual(order_call.kwargs["headers"]["tr_id"], "VTTT1004U")
        self.assertEqual(order_call.kwargs["json"]["ORGN_ODNO"], "OD987")
        self.assertEqual(order_call.kwargs["json"]["RVSE_CNCL_DVSN_CD"], "02")


if __name__ == "__main__":
    unittest.main()
