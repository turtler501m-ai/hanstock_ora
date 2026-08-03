from __future__ import annotations

import threading
import time
from pathlib import Path


_LOCK = threading.Lock()
_PATH = Path(".runtime") / "openai-rate-limit-until"


def remaining_cooldown(now: float | None = None) -> float:
    current = time.time() if now is None else float(now)
    with _LOCK:
        try:
            until = float(_PATH.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0.0
    return max(0.0, until - current)


def require_available() -> None:
    remaining = remaining_cooldown()
    if remaining > 0:
        raise RuntimeError(f"OpenAI rate-limit cooldown active ({remaining:.0f}s remaining)")


def record_rate_limit(retry_after: str | int | float | None = None) -> float:
    try:
        delay = float(retry_after or 60)
    except (TypeError, ValueError):
        delay = 60.0
    delay = max(15.0, min(delay, 900.0))
    until = time.time() + delay
    with _LOCK:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(str(until), encoding="utf-8")
    return delay
