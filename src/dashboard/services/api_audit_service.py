from __future__ import annotations

import os
import json
import re
import socket
import threading
import time
import uuid
from typing import Any

from src.notifier.slack import send_slack
from src.utils.logger import logger


_READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_CAPTURE_BYTES = 256 * 1024
_SERVER_NAME = socket.gethostname()
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
_FEATURE_NAMES = {
    "get_balance": "계좌 잔고 조회",
    "get_trades": "거래내역 조회",
    "get_approvals": "주문 승인목록 조회",
    "create_approval": "주문 승인요청 생성",
    "approve_order": "주문 승인 실행",
    "reject_order": "주문 승인 거절",
    "sell_all_holdings": "보유종목 전량매도",
    "sync_trade_order_status": "주문 체결상태 동기화",
    "activate_kill_switch": "킬스위치 활성화",
    "deactivate_kill_switch": "킬스위치 해제",
    "get_performance": "투자성과 조회",
    "get_signals": "매매신호 조회",
    "get_watchlist": "관심종목 조회",
    "get_scheduler_status": "자동매매 일정상태 조회",
    "get_trade_sync_status": "거래 동기화상태 조회",
    "get_local_trade_cleanup_candidates": "로컬 거래 정리대상 조회",
    "mistock_balance": "미스톡 계좌 잔고 조회",
    "mistock_performance": "미스톡 투자성과 조회",
}
_FEATURE_WORDS = {
    "get": "조회",
    "list": "목록 조회",
    "load": "불러오기",
    "create": "생성",
    "update": "수정",
    "save": "저장",
    "delete": "삭제",
    "approve": "승인",
    "reject": "거절",
    "retry": "재시도",
    "cancel": "취소",
    "sync": "동기화",
    "start": "시작",
    "stop": "중지",
    "status": "상태",
    "balance": "잔고",
    "trade": "거래",
    "trades": "거래내역",
    "order": "주문",
    "orders": "주문목록",
    "holding": "보유종목",
    "holdings": "보유종목",
    "performance": "성과",
    "signal": "신호",
    "signals": "신호목록",
    "strategy": "전략",
    "scheduler": "자동매매 일정",
    "settings": "설정",
    "system": "시스템",
    "mistock": "미스톡",
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


def korean_result(status_code: int) -> str:
    if int(status_code) < 400:
        return "성공"
    if int(status_code) < 500:
        return "요청오류"
    return "서버오류"


def korean_feature_name(feature: str) -> str:
    raw = str(feature or "unmatched_api").strip()
    if raw in _FEATURE_NAMES:
        return _FEATURE_NAMES[raw]
    translated = [
        _FEATURE_WORDS.get(word, word)
        for word in re.split(r"[_\s]+", raw)
        if word
    ]
    return " ".join(translated)[:120] or "알 수 없는 API"


def sanitize_error(value: Any) -> str:
    text = str(value or "-").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"https?://\S+", "[URL제거]", text)
    text = re.sub(r"(?i)(token|secret|api[_-]?key|account|cano)\s*[=:]\s*\S+", r"\1=[보호됨]", text)
    text = re.sub(r"\b\d{8,12}\b", "[번호보호]", text)
    return text[:240] or "-"


def api_audit_message(
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    *,
    feature: str = "unknown",
    request_id: str = "-",
    summary: str = "-",
    error: str = "-",
    server: str = "",
) -> str:
    safe_method = str(method or "UNKNOWN").upper()[:12]
    safe_path = str(path or "/").split("?", 1)[0][:300]
    safe_feature = korean_feature_name(feature)
    safe_request_id = str(request_id or "-")[:32]
    safe_summary = str(summary or "-").replace("\n", " ")[:500]
    safe_error = sanitize_error(error)
    safe_server = str(server or f"{_SERVER_NAME}:{os.getpid()}")[:120]
    return (
        f"[API점검] 서버={safe_server} 요청ID={safe_request_id} "
        f"기능={safe_feature} 요청={safe_method} {safe_path} "
        f"수행결과={korean_result(status_code)} HTTP상태={int(status_code)} "
        f"처리시간ms={max(0.0, float(duration_ms)):.1f} "
        f"결과요약={safe_summary} 오류내용={safe_error}"
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


def error_from_api_body(body: bytes, status_code: int) -> str:
    if int(status_code) < 400 or not body:
        return "-"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "응답 본문을 해석할 수 없음"
    if isinstance(payload, dict):
        value = payload.get("detail") or payload.get("error") or payload.get("message")
        if isinstance(value, list):
            value = "; ".join(str(item) for item in value[:3])
        return sanitize_error(value or "상세 오류 없음")
    return "상세 오류 없음"


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
        caught_error: Exception | None = None

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
        except Exception as exc:
            caught_error = exc
            raise
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
            error = (
                sanitize_error(f"{type(caught_error).__name__}: {caught_error}")
                if caught_error is not None
                else error_from_api_body(bytes(body), status_code)
            )
            message = api_audit_message(
                method,
                route_path,
                status_code,
                duration_ms,
                feature=feature,
                request_id=request_id,
                summary=summary,
                error=error,
            )
            if status_code >= 500:
                logger.error(message)
            elif status_code >= 400:
                logger.warning(message)
            else:
                logger.info(message)
            send_api_slack_async(method, route_path, status_code, duration_ms)
