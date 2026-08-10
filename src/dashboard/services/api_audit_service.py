from __future__ import annotations

import os
import json
import threading
import time
import uuid
from typing import Any

from src.notifier.slack import send_slack
from src.utils.logger import logger


_READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_CAPTURE_BYTES = 256 * 1024
_SUMMARY_LIST_KEYS = {
    "holdings",
    "approvals",
    "trades",
    "orders",
    "results",
    "items",
    "candidates",
    "errors",
    "skipped",
    "canceled_buy_orders",
}
_SUMMARY_SCALAR_KEYS = {
    "ok",
    "status",
    "order_status",
    "id",
    "job_id",
    "created_count",
    "pending_count",
    "submitted_count",
    "executed_count",
    "failed_count",
    "processed_count",
    "success_count",
    "synced_count",
    "skipped_count",
    "new_buys_halted",
}


def api_slack_enabled() -> bool:
    return os.environ.get("HANSTOCK_API_SLACK", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def should_send_api_slack(method: str, status_code: int) -> bool:
    """Notify for mutations and failures, but not normal dashboard polling."""
    return int(status_code) >= 400 or str(method or "").upper() not in _READ_ONLY_METHODS


def api_result(status_code: int) -> str:
    if int(status_code) < 400:
        return "success"
    if int(status_code) < 500:
        return "client_error"
    return "server_error"


def api_audit_message(
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    *,
    feature: str = "unknown",
    request_id: str = "-",
    summary: str = "-",
) -> str:
    safe_method = str(method or "UNKNOWN").upper()[:12]
    safe_path = str(path or "/").split("?", 1)[0][:300]
    safe_feature = str(feature or "unknown").replace(" ", "_")[:120]
    safe_request_id = str(request_id or "-")[:32]
    safe_summary = str(summary or "-").replace("\n", " ")[:500]
    return (
        f"[API] request_id={safe_request_id} {safe_method} {safe_path} "
        f"feature={safe_feature} "
        f"result={api_result(status_code)} status={int(status_code)} "
        f"duration_ms={max(0.0, float(duration_ms)):.1f} summary={safe_summary}"
    )


def summarize_api_payload(payload: Any, *, content_bytes: int = 0, truncated: bool = False) -> str:
    if not isinstance(payload, dict):
        return f"content_bytes={max(0, int(content_bytes))}"

    parts: list[str] = []
    for key, value in payload.items():
        if key in _SUMMARY_LIST_KEYS and isinstance(value, list):
            parts.append(f"{key}_count={len(value)}")
        elif key in _SUMMARY_SCALAR_KEYS and isinstance(value, (str, int, float, bool)):
            text = str(value).replace(" ", "_")[:80]
            parts.append(f"{key}={text}")
        elif key.endswith("_count") and isinstance(value, (int, float)):
            parts.append(f"{key}={value}")
    if truncated:
        parts.append("payload_truncated=true")
    if not parts:
        parts.append(f"content_bytes={max(0, int(content_bytes))}")
    return ",".join(parts[:20])


def summarize_api_body(body: bytes, *, content_bytes: int, truncated: bool) -> str:
    if not body:
        return f"content_bytes={max(0, int(content_bytes))}"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"content_bytes={max(0, int(content_bytes))}"
    return summarize_api_payload(
        payload,
        content_bytes=content_bytes,
        truncated=truncated,
    )


def concise_slack_message(method: str, path: str, status_code: int, duration_ms: float) -> str:
    outcome = "성공" if int(status_code) < 400 else "실패"
    safe_path = str(path or "/").split("?", 1)[0][:180]
    return (
        f"[한스톡 API] {outcome} | {str(method or 'UNKNOWN').upper()} {safe_path} "
        f"| {int(status_code)} | {max(0.0, float(duration_ms)):.0f}ms"
    )


def send_api_slack_async(method: str, path: str, status_code: int, duration_ms: float) -> None:
    if not api_slack_enabled() or not should_send_api_slack(method, status_code):
        return

    message = concise_slack_message(method, path, status_code, duration_ms)

    def worker() -> None:
        try:
            send_slack(
                text=message,
                color="#2ecc71" if int(status_code) < 400 else "#e74c3c",
            )
        except Exception as exc:
            logger.warning(f"[API_AUDIT] Slack notification failed: {exc}")

    threading.Thread(target=worker, name="api-audit-slack", daemon=True).start()


class ApiAuditMiddleware:
    """Observe API responses without consuming or rewriting their ASGI body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "/")
        if path != "/api" and not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "UNKNOWN")
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        status_code = 500
        body = bytearray()
        content_bytes = 0
        truncated = False

        async def audit_send(message):
            nonlocal status_code, content_bytes, truncated
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status") or 500)
            elif message.get("type") == "http.response.body":
                chunk = message.get("body") or b""
                content_bytes += len(chunk)
                remaining = _MAX_CAPTURE_BYTES - len(body)
                if remaining > 0:
                    body.extend(chunk[:remaining])
                if len(chunk) > max(0, remaining):
                    truncated = True
            await send(message)

        try:
            await self.app(scope, receive, audit_send)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            route = scope.get("route")
            feature = getattr(route, "name", None) or "unmatched_api"
            route_path = getattr(route, "path", None) or path
            summary = summarize_api_body(
                bytes(body),
                content_bytes=content_bytes,
                truncated=truncated,
            )
            message = api_audit_message(
                method,
                route_path,
                status_code,
                duration_ms,
                feature=feature,
                request_id=request_id,
                summary=summary,
            )
            if status_code >= 500:
                logger.error(message)
            elif status_code >= 400:
                logger.warning(message)
            else:
                logger.info(message)
            send_api_slack_async(method, route_path, status_code, duration_ms)
