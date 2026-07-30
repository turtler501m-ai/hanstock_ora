import unittest
from datetime import datetime, timezone

from src.notifications import (
    build_candidates_payload,
    build_error_payload,
    build_order_payload,
    build_order_summary_payload,
    build_session_end_payload,
    build_session_start_payload,
    build_slack_payload,
    format_kst_timestamp,
    post_slack_payload,
    send_slack_message,
)


class _FakeResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class _FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or _FakeResponse()
        self.error = error
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.error:
            raise self.error
        return self.response


class NotificationTests(unittest.TestCase):
    def test_format_kst_timestamp_converts_timezone(self):
        value = datetime(2026, 4, 26, 3, 15, tzinfo=timezone.utc)
        self.assertEqual(format_kst_timestamp(value), "2026-04-26 12:15 KST")

    def test_build_slack_payload_wraps_color_in_attachment(self):
        payload = build_slack_payload(
            text="hello",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "body"}}],
            color="#123456",
        )
        self.assertEqual(payload["text"], "hello")
        self.assertNotIn("blocks", payload)
        self.assertEqual(payload["attachments"][0]["color"], "#123456")
        self.assertEqual(payload["attachments"][0]["fallback"], "hello")

    def test_build_session_start_payload_matches_expected_fields(self):
        payload = build_session_start_payload(
            cash=1_500_000,
            total=2_000_000,
            stock_count=3,
            now=datetime(2026, 4, 26, 9, 30, tzinfo=timezone.utc),
            mode="DRY_RUN",
            trading_env="demo",
        )
        text = payload["attachments"][0]["blocks"][0]["text"]["text"]
        self.assertIn("세븐 스플릿 자동매매 시작", payload["text"])
        self.assertIn("DRY_RUN", text)
        self.assertIn("2,000,000원", text)

    def test_build_order_payload_formats_market_sell_and_failure_color(self):
        payload = build_order_payload(
            name="Samsung",
            symbol="005930",
            action="sell",
            qty=7,
            price=0,
            reason="stop loss",
            ok=False,
            indicators={"rsi": 31.25, "sma20": 71000, "sma60": 68000, "rt": -15.34},
        )
        text = payload["attachments"][0]["blocks"][0]["text"]["text"]
        self.assertEqual(payload["attachments"][0]["color"], "#e74c3c")
        self.assertIn("시장가", text)
        self.assertIn("RSI: 31.2", text)
        self.assertIn("수익률: -15.34%", text)

    def test_build_order_payload_formats_us_stock_with_exchange_rate(self):
        payload = build_order_payload(
            name="Apple",
            symbol="AAPL",
            action="buy",
            qty=2.5,
            price=180.0,
            reason="split buy",
            ok=True,
            indicators={"rsi": 35.0, "sma20": 178.0, "sma60": 175.0, "rt": 1.25},
            exchange_rate=1300.0,
        )
        text = payload["attachments"][0]["blocks"][0]["text"]["text"]
        self.assertEqual(payload["attachments"][0]["color"], "#36a64f")
        self.assertIn("2.5000주", text)
        self.assertIn("$180.00 (₩234,000원)", text)
        self.assertIn("$450.00 (₩585,000원)", text)
        self.assertIn("RSI: 35.0", text)
        self.assertIn("수익률: +1.25%", text)

    def test_build_order_summary_payload_uses_two_line_summary_for_kr_stock(self):
        payload = build_order_summary_payload(
            name="삼성전자",
            symbol="005930",
            action="buy",
            qty=3,
            price=70000,
            reason="RSI recovery",
            ok=True,
            indicators={"rsi": 31.2, "rt": 1.5},
        )

        lines = payload["text"].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("성공 | 매수 삼성전자(005930) 3주 @ 70,000원 / 210,000원", lines[0])
        self.assertIn("사유: RSI recovery", lines[1])
        self.assertIn("RSI 31.2", lines[1])

    def test_build_order_summary_payload_uses_two_line_summary_for_us_stock(self):
        payload = build_order_summary_payload(
            name="Apple",
            symbol="AAPL",
            action="sell",
            qty=1.25,
            price=205.5,
            reason="take profit",
            ok=False,
            indicators={"rsi": 70, "rt": -0.25},
        )

        lines = payload["text"].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("실패 | 매도 Apple(AAPL) 1.2500주 @ $205.50 / $256.88", lines[0])
        self.assertIn("사유: take profit", lines[1])
        self.assertIn("수익률 -0.25%", lines[1])

    def test_build_order_summary_payload_uses_two_line_summary_for_us_stock_with_exchange_rate(self):
        payload = build_order_summary_payload(
            name="Apple",
            symbol="AAPL",
            action="sell",
            qty=1.25,
            price=205.5,
            reason="take profit",
            ok=False,
            indicators={"rsi": 70, "rt": -0.25},
            exchange_rate=1350.0,
        )

        lines = payload["text"].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("실패 | 매도 Apple(AAPL) 1.2500주 @ $205.50 (₩277,425원) / $256.88 (₩346,781원)", lines[0])
        self.assertIn("사유: take profit", lines[1])
        self.assertIn("수익률 -0.25%", lines[1])

    def test_build_candidates_payload_returns_none_for_empty_candidates(self):
        self.assertIsNone(build_candidates_payload([]))

    def test_build_candidates_payload_renders_candidate_lines(self):
        payload = build_candidates_payload([
            {"ticker": "005930", "current_price": 70123, "score": 4, "reasons": ["rsi", "macd"]},
        ])
        text = payload["attachments"][0]["blocks"][1]["text"]["text"]
        self.assertIn("*005930* (`005930`) 70,123원 | 점수 4 | rsi, macd", text)

    def test_build_candidates_payload_limits_long_lists(self):
        candidates = []
        for i in range(40):
            candidates.append({
                "ticker": f"{i:06d}",
                "name": f"Stock{i}",
                "current_price": 10000,
                "score": 3,
                "reasons": ["rsi pullback " * 10]  # Very long reasons to exceed limit
            })
        payload = build_candidates_payload(candidates)
        blocks = payload["attachments"][0]["blocks"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["type"], "section")
        self.assertIn("외 35종목", blocks[1]["text"]["text"])

    def test_build_session_end_payload_summarizes_results(self):
        payload = build_session_end_payload(
            results=[
                {"name": "Samsung", "action": "buy", "qty": 2, "reason": "score", "ok": True, "decision": "execute"},
                {"name": "SK", "action": "sell", "qty": 1, "reason": "tp", "ok": False, "decision": "execute"},
                {"name": "Naver", "action": "buy", "qty": 3, "reason": "queue", "ok": False, "decision": "queue"},
            ],
            cash=100_000,
            total=500_000,
            pnl=-25_000,
            now=datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc),
        )
        attachment = payload["attachments"][0]
        text = attachment["blocks"][0]["text"]["text"]
        orders = attachment["blocks"][1]["text"]["text"]
        self.assertEqual(attachment["color"], "#e74c3c")
        self.assertIn("매수성공: 1건", text)
        self.assertIn("승인대기: 1건", text)
        self.assertIn("승인대기 Naver 3주 - queue", orders)

    def test_build_session_end_payload_limits_long_lists(self):
        # Create enough results to exceed the 2800 character chunk limit
        results = []
        for i in range(40):
            results.append({
                "name": f"StockName{i} " * 5,  # Very long name
                "action": "buy",
                "qty": 10,
                "reason": "extremely long strategy reason description " * 5,
                "ok": True,
                "decision": "execute"
            })
        payload = build_session_end_payload(results, cash=1000, total=2000, pnl=100)
        blocks = payload["attachments"][0]["blocks"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["type"], "section")
        self.assertEqual(blocks[1]["type"], "section")
        self.assertIn("외 35건", blocks[1]["text"]["text"])

    def test_build_session_end_payload_handles_empty_results(self):
        payload = build_session_end_payload(results=[], cash=1, total=2, pnl=3)
        self.assertEqual(payload["attachments"][0]["color"], "#9E9E9E")

        self.assertIn("주문 없음", payload["text"])

    def test_build_error_payload_uses_error_color(self):
        payload = build_error_payload("boom")
        self.assertEqual(payload["text"], "세븐 스플릿 오류: boom")
        self.assertEqual(payload["attachments"][0]["color"], "#e74c3c")

    def test_post_slack_payload_returns_true_on_success(self):
        session = _FakeSession()
        payload = {"text": "hello"}
        self.assertTrue(post_slack_payload("https://example.test", payload, session))
        self.assertEqual(session.calls[0]["json"], payload)

    def test_post_slack_payload_logs_failures_and_returns_false(self):
        logs = []
        session = _FakeSession(response=_FakeResponse(status_code=500, text="server error"))
        ok = post_slack_payload("https://example.test", {"text": "hello"}, session, log_fn=logs.append)
        self.assertFalse(ok)
        self.assertIn("HTTP 500", logs[0])

    def test_send_slack_message_builds_and_posts_payload(self):
        session = _FakeSession()
        ok = send_slack_message(
            webhook_url="https://example.test",
            session=session,
            text="hello",
            color="#abcdef",
        )
        self.assertTrue(ok)
        sent = session.calls[0]["json"]
        self.assertEqual(sent["text"], "hello")
        self.assertEqual(sent["attachments"][0]["color"], "#abcdef")


if __name__ == "__main__":
    unittest.main()
