import base64
import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi import Request
from fastapi.responses import JSONResponse

from src.dashboard import require_dashboard_auth


class DashboardAuthTests(unittest.TestCase):
    @staticmethod
    def _authorization(username: str, password: str) -> dict[str, str]:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    @staticmethod
    def _request(headers: dict[str, str] | None = None) -> Request:
        encoded_headers = [
            (key.lower().encode("ascii"), value.encode("ascii"))
            for key, value in (headers or {}).items()
        ]
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/openapi.json",
                "raw_path": b"/openapi.json",
                "query_string": b"",
                "headers": encoded_headers,
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 8000),
            }
        )

    @staticmethod
    def _call(headers: dict[str, str] | None = None):
        async def call_next(_request):
            return JSONResponse({"ok": True})

        return asyncio.run(require_dashboard_auth(DashboardAuthTests._request(headers), call_next))

    def test_auth_disabled_preserves_existing_access(self):
        with patch.dict(os.environ, {"DASHBOARD_AUTH_ENABLED": "false"}, clear=False):
            response = self._call()

        self.assertEqual(response.status_code, 200)

    def test_auth_enabled_rejects_missing_credentials(self):
        env = {
            "DASHBOARD_AUTH_ENABLED": "true",
            "DASHBOARD_AUTH_USERNAME": "operator",
            "DASHBOARD_AUTH_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=False):
            response = self._call()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], 'Basic realm="Hanstock Dashboard"')

    def test_auth_enabled_is_enforced_outside_test_mode(self):
        env = {
            "HANSTOCK_TESTING": "0",
            "DASHBOARD_AUTH_ENABLED": "true",
            "DASHBOARD_AUTH_USERNAME": "operator",
            "DASHBOARD_AUTH_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=False):
            response = self._call()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], 'Basic realm="Hanstock Dashboard"')

    def test_auth_enabled_rejects_invalid_credentials(self):
        env = {
            "DASHBOARD_AUTH_ENABLED": "true",
            "DASHBOARD_AUTH_USERNAME": "operator",
            "DASHBOARD_AUTH_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=False):
            response = self._call(self._authorization("operator", "wrong"))

        self.assertEqual(response.status_code, 401)

    def test_auth_enabled_accepts_valid_credentials(self):
        env = {
            "DASHBOARD_AUTH_ENABLED": "true",
            "DASHBOARD_AUTH_USERNAME": "operator",
            "DASHBOARD_AUTH_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=False):
            response = self._call(self._authorization("operator", "secret"))

        self.assertEqual(response.status_code, 200)

    def test_auth_enabled_without_configuration_fails_closed(self):
        env = {
            "DASHBOARD_AUTH_ENABLED": "true",
            "DASHBOARD_AUTH_USERNAME": "",
            "DASHBOARD_AUTH_PASSWORD": "",
        }
        with patch.dict(os.environ, env, clear=False):
            response = self._call()

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
