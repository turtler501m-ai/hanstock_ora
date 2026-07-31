from __future__ import annotations

import base64
import binascii
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse


def dashboard_auth_config() -> dict[str, str | bool]:
    enabled = os.environ.get("DASHBOARD_AUTH_ENABLED", "false").lower() in {
        "1", "true", "yes", "on",
    }
    return {
        "enabled": enabled,
        "username": os.environ.get("DASHBOARD_AUTH_USERNAME", ""),
        "password": os.environ.get("DASHBOARD_AUTH_PASSWORD", ""),
    }


def dashboard_basic_credentials(request: Request) -> tuple[str, str] | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    username, separator, password = decoded.partition(":")
    return (username, password) if separator else None


async def require_dashboard_auth(request: Request, call_next):
    auth = dashboard_auth_config()
    if not auth["enabled"]:
        return await call_next(request)
    username = str(auth["username"])
    password = str(auth["password"])
    if not username or not password:
        return JSONResponse(
            status_code=503,
            content={"detail": "dashboard authentication is enabled but not configured"},
        )
    credentials = dashboard_basic_credentials(request)
    if credentials is not None:
        supplied_username, supplied_password = credentials
        if secrets.compare_digest(supplied_username, username) and secrets.compare_digest(
            supplied_password, password
        ):
            return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"detail": "dashboard authentication required"},
        headers={"WWW-Authenticate": 'Basic realm="Hanstock Dashboard"'},
    )
