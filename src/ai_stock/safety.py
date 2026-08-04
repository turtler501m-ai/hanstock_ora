# -*- coding: utf-8 -*-
"""안전 상태/가드 노출 (§1.2·F02).

모든 AI스톡 API 응답에 포함되는 safety 블록을 만든다.
값은 환경변수 계층(config)에서 읽고, 런타임 trader 값이 있으면 우선한다.
"""
from __future__ import annotations

import os
from typing import Any

from src.config import config


def _runtime_bool(name: str, fallback: bool) -> bool:
    """trader 모듈의 런타임 값이 있으면 우선, 없으면 config/env fallback."""
    try:
        from src import trader  # 지연 import (무거운 모듈)

        val = getattr(trader, name, None)
        if val is not None:
            return bool(val)
    except Exception:
        pass
    env = os.environ.get(name)
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(fallback)


def safety_state() -> dict[str, Any]:
    """안전 가드 상태 (§1.2 기본값 유지 여부 확인용)."""
    trading_env = os.environ.get("TRADING_ENV") or getattr(config, "trading_env", "demo")
    try:
        from src import trader

        trading_env = trader.runtime_flags().trading_env or trading_env
    except Exception:
        pass
    return {
        "dry_run": _runtime_bool("DRY_RUN", getattr(config, "dry_run", True)),
        "trading_env": str(trading_env),
        "enable_live_trading": _runtime_bool(
            "ENABLE_LIVE_TRADING", getattr(config, "enable_live_trading", False)
        ),
        "require_approval": _runtime_bool(
            "REQUIRE_APPROVAL", getattr(config, "require_approval", True)
        ),
        "ai_strategy_enabled": bool(getattr(config, "ai_strategy_enabled", False)),
        "online_access_blocked": bool(getattr(config, "online_access_blocked", False)),
    }


def live_trading_allowed() -> bool:
    """자동 주문 Level 6이 환경 가드상 허용되는지 (§1.2.1).

    demo 주문은 DRY_RUN=false이면 허용한다. real 주문은 ENABLE_LIVE_TRADING=true가
    추가로 필요하다. 비표준 TRADING_ENV와 online 차단은 항상 거부한다.
    """
    s = safety_state()
    env = str(s["trading_env"]).lower()
    if s["online_access_blocked"] or s["dry_run"]:
        return False
    if env == "demo":
        return True
    if env == "real":
        return bool(s["enable_live_trading"])
    return False
