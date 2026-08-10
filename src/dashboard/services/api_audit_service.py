from __future__ import annotations

import os
import threading

from src.notifier.slack import send_slack
from src.utils.logger import logger


_READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}


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


def api_audit_message(method: str, path: str, status_code: int, duration_ms: float) -> str:
    safe_method = str(method or "UNKNOWN").upper()[:12]
    safe_path = str(path or "/").split("?", 1)[0][:300]
    return (
        f"[API] {safe_method} {safe_path} status={int(status_code)} "
        f"duration_ms={max(0.0, float(duration_ms)):.1f}"
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
