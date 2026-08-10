import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.dashboard.services import api_audit_service


def _scope(method: str, path: str) -> dict:
    return {"type": "http", "method": method, "path": path}


class ApiAuditTests(unittest.TestCase):
    def test_message_omits_query_and_body_secrets(self):
        message = api_audit_service.api_audit_message(
            "post",
            "/api/settings?token=secret",
            200,
            12.34,
            feature="update settings",
            request_id="abc123",
            summary="ok=True",
        )

        self.assertTrue(message.startswith("[API점검] 서버="), msg=message)
        self.assertIn("요청ID=abc123", message)
        self.assertIn("기능=수정 설정", message)
        self.assertIn("수행결과=성공 HTTP상태=200", message)
        self.assertIn("처리시간ms=12.3 결과요약=ok=True 오류내용=-", message)
        self.assertNotIn("secret", message)

    def test_slack_policy_skips_successful_reads(self):
        self.assertFalse(api_audit_service.should_send_api_slack("GET", 200))
        self.assertTrue(api_audit_service.should_send_api_slack("POST", 200))
        self.assertTrue(api_audit_service.should_send_api_slack("GET", 500))

    def test_log_policy_skips_successful_reads(self):
        self.assertFalse(api_audit_service.should_log_api_audit("GET", 200))
        self.assertFalse(api_audit_service.should_log_api_audit("HEAD", 204))
        self.assertTrue(api_audit_service.should_log_api_audit("POST", 200))
        self.assertTrue(api_audit_service.should_log_api_audit("GET", 400))
        self.assertTrue(api_audit_service.should_log_api_audit("GET", 500))

    def test_successful_get_is_not_logged(self):
        scope = _scope("GET", "/api/mistock/health")

        async def app(inner_scope, _receive, send):
            inner_scope["route"] = SimpleNamespace(
                name="mistock_health", path="/api/mistock/health"
            )
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"ok":true}'})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        middleware = api_audit_service.ApiAuditMiddleware(app)
        with (
            patch.object(api_audit_service.logger, "info") as info,
            patch.object(api_audit_service.logger, "warning") as warning,
            patch.object(api_audit_service.logger, "error") as error,
        ):
            asyncio.run(middleware(scope, receive, send))

        info.assert_not_called()
        warning.assert_not_called()
        error.assert_not_called()

    def test_slack_notification_is_concise_and_async(self):
        sent = []

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with (
            patch.dict(os.environ, {"HANSTOCK_API_SLACK": "true"}),
            patch.object(api_audit_service.threading, "Thread", ImmediateThread),
            patch.object(
                api_audit_service,
                "send_slack",
                side_effect=lambda **kwargs: sent.append(kwargs),
            ),
        ):
            api_audit_service.send_api_slack_async(
                "POST", "/api/holdings/sell-all", 200, 87.2
            )

        self.assertEqual(len(sent), 1)
        self.assertEqual(
            sent[0]["text"],
            "[한스톡 API] 성공 | POST /api/holdings/sell-all | 200 | 87ms",
        )

    def test_api_middleware_logs_status_and_notifies(self):
        scope = _scope("POST", "/api/system/kill")
        sent = []

        async def app(inner_scope, _receive, send):
            inner_scope["route"] = SimpleNamespace(
                name="activate_kill_switch", path="/api/system/kill"
            )
            await send({"type": "http.response.start", "status": 201, "headers": []})
            await send(
                {"type": "http.response.body", "body": b'{"ok":true,"status":"active"}'}
            )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        middleware = api_audit_service.ApiAuditMiddleware(app)

        with (
            patch.object(api_audit_service.logger, "info") as info,
            patch.object(api_audit_service, "send_api_slack_async") as slack,
            patch.object(api_audit_service.uuid, "uuid4", return_value=SimpleNamespace(hex="abc123def456")),
        ):
            asyncio.run(middleware(scope, receive, send))

        self.assertEqual(sent[0]["status"], 201)
        logged = info.call_args.args[0]
        self.assertIn(
            "요청ID=abc123def456 기능=킬스위치 활성화 "
            "요청=POST /api/system/kill 수행결과=성공 HTTP상태=201",
            logged,
        )
        self.assertIn("결과요약=ok=True,status=active 오류내용=-", logged)
        slack.assert_called_once()

    def test_result_classification(self):
        self.assertEqual(api_audit_service.api_result(200), "success")
        self.assertEqual(api_audit_service.api_result(409), "client_error")
        self.assertEqual(api_audit_service.api_result(500), "server_error")
        self.assertEqual(api_audit_service.korean_result(200), "성공")
        self.assertEqual(api_audit_service.korean_result(409), "요청오류")
        self.assertEqual(api_audit_service.korean_result(500), "서버오류")

    def test_error_summary_is_sanitized(self):
        body = b'{"detail":"account=1234567801 token=abc failed"}'
        error = api_audit_service.error_from_api_body(body, 409)

        self.assertNotIn("1234567801", error)
        self.assertNotIn("token=abc", error)
        self.assertIn("[보호됨]", error)

    def test_non_api_request_is_not_audited(self):
        scope = _scope("GET", "/static/js/app.js")
        sent = []

        async def app(_scope, _receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        middleware = api_audit_service.ApiAuditMiddleware(app)

        with (
            patch.object(api_audit_service.logger, "info") as info,
            patch.object(api_audit_service, "send_api_slack_async") as slack,
        ):
            asyncio.run(middleware(scope, receive, send))

        info.assert_not_called()
        slack.assert_not_called()

    def test_payload_summary_extracts_operational_counts_only(self):
        summary = api_audit_service.summarize_api_payload(
            {
                "status": "created",
                "holdings": [{"symbol": "005930"}],
                "orders": [{"id": 1}, {"id": 2}],
                "failed_count": 1,
                "account_no": "secret-account",
                "cash": 123456,
            }
        )

        self.assertEqual(
            summary,
            "status=created,holdings_count=1,orders_count=2,failed_count=1",
        )
        self.assertNotIn("secret-account", summary)
        self.assertNotIn("123456", summary)


if __name__ == "__main__":
    unittest.main()
