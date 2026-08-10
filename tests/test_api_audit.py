import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.dashboard import core
from src.dashboard.services import api_audit_service


def _request(method: str, path: str, query: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }
    )


class ApiAuditTests(unittest.TestCase):
    def test_message_omits_query_and_body_secrets(self):
        message = api_audit_service.api_audit_message(
            "post",
            "/api/settings?token=secret",
            200,
            12.34,
            feature="update settings",
        )

        self.assertEqual(
            message,
            "[API] POST /api/settings feature=update_settings result=success "
            "status=200 duration_ms=12.3",
        )
        self.assertNotIn("secret", message)

    def test_slack_policy_skips_successful_reads(self):
        self.assertFalse(api_audit_service.should_send_api_slack("GET", 200))
        self.assertTrue(api_audit_service.should_send_api_slack("POST", 200))
        self.assertTrue(api_audit_service.should_send_api_slack("GET", 500))

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
        request = _request("POST", "/api/system/kill", b"token=secret")

        async def call_next(_request):
            return JSONResponse({"ok": True}, status_code=201)

        with (
            patch.object(core.logger, "info") as info,
            patch.object(core, "send_api_slack_async") as slack,
        ):
            response = asyncio.run(core.audit_api_requests(request, call_next))

        self.assertEqual(response.status_code, 201)
        logged = info.call_args.args[0]
        self.assertIn(
            "[API] POST /api/system/kill feature=unmatched_api result=success status=201",
            logged,
        )
        self.assertNotIn("secret", logged)
        slack.assert_called_once()

    def test_result_classification(self):
        self.assertEqual(api_audit_service.api_result(200), "success")
        self.assertEqual(api_audit_service.api_result(409), "client_error")
        self.assertEqual(api_audit_service.api_result(500), "server_error")

    def test_non_api_request_is_not_audited(self):
        request = _request("GET", "/static/js/app.js")

        async def call_next(_request):
            return JSONResponse({}, status_code=200)

        with (
            patch.object(core.logger, "info") as info,
            patch.object(core, "send_api_slack_async") as slack,
        ):
            asyncio.run(core.audit_api_requests(request, call_next))

        info.assert_not_called()
        slack.assert_not_called()


if __name__ == "__main__":
    unittest.main()
