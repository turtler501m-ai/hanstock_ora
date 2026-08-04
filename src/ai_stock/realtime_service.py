# -*- coding: utf-8 -*-
"""2차 실시간 타이밍 엔진 (§4.8·§6.8).

1차 후보 풀에 있는 종목에 대해서만 실시간 진입/청산 신호를 만든다.
신호가 곧 주문이 아니다(정책·리스크 게이트는 automation_service). 실시간 단절/stale
시 신규 진입(entry)을 생성하지 않고 보유 보호(exit)만 허용한다.
KR은 KISWebSocketClient 실시간, US는 폴링으로 동작(한계는 §4.8).
"""
from __future__ import annotations

from src.db import ai_scan_repository as scan_repository
from src.db import ai_watchlist_repository as watchlist_repository
from src.db import ai_snapshot_repository as snapshot_repository

from typing import Any

from src.ai_stock.constants import (
    SIGNAL_ENTRY,
    SIGNAL_EXIT,
    SIGNAL_HOLD,
    WATCH_CONFIRMED,
)
from src.ai_stock.freshness import is_stale, now as _now
from src.ai_stock.markets import require_storable_market



def run_realtime_cycle(market: str, *, strategy_id: str = "ai_stock_default_v1") -> dict[str, Any]:
    """2차 실시간 사이클 (§4.8). 후보 풀(watching/confirmed)에 대해서만 신호 생성.

    주기 실행(폴링/스케줄)은 이 사이클을 반복 호출한다. 단절 시 신규 진입은 만들지 않는다.
    """
    from src.ai_stock.market_data import get_provider

    market = require_storable_market(market)
    provider = get_provider()
    pool = [w for w in watchlist_repository.list_watchlist(market=market)
            if w.get("status") in ("watching", WATCH_CONFIRMED)]
    signals = []
    errors = []
    for w in pool:
        try:
            quote = provider.quote(market, w.get("symbol"))
        except Exception as exc:
            quote = None
            errors.append({"candidate_id": w.get("candidate_id"), "symbol": w.get("symbol"), "error": str(exc)})
        try:
            sig = evaluate_timing(market, w["candidate_id"], realtime_quote=quote, strategy_id=strategy_id)
            signals.append(sig)
        except ValueError:
            continue
    return {"market": market, "pool_size": len(pool), "count": len(signals), "signals": signals, "errors": errors}


def evaluate_timing(market: str, candidate_id: int, *,
                    realtime_quote: dict[str, Any] | None = None,
                    strategy_id: str = "ai_stock_default_v1") -> dict[str, Any]:
    """후보 1건의 2차 타이밍 신호 생성. 후보 풀(confirmed/watching)만 대상."""
    market = require_storable_market(market)
    watch = watchlist_repository.get_watch(candidate_id)
    candidate = scan_repository.get_candidate(candidate_id)
    if watch is None or candidate is None:
        raise ValueError("candidate not in pool")  # 즉흥 신호 금지(§4.8)

    pol = watchlist_repository.get_policy(strategy_id, market) or {}
    min_conf = float(pol.get("timing_min_confidence") or 0.6)

    quote = realtime_quote or {}
    price = quote.get("price")
    data_as_of = quote.get("data_as_of")
    stale = is_stale(data_as_of, "intraday_price")
    connected = price is not None and not stale

    signal_type = SIGNAL_HOLD
    trigger = None
    confidence = 0.0

    if not connected:
        # 실시간 단절 → 신규 진입 차단, 보유 보호용 exit만 (§4.8)
        signal_type = SIGNAL_EXIT if watch["status"] == WATCH_CONFIRMED else SIGNAL_HOLD
        trigger = "invalidation"
    else:
        entry = watch.get("initial_price") or candidate.get("current_price") or price
        # 단순 결정적 트리거: 진입가 대비 돌파/이탈
        if entry and price >= float(entry) * 1.005:
            signal_type, trigger, confidence = SIGNAL_ENTRY, "breakout", 0.7
        elif entry and price <= float(entry) * 0.97:
            signal_type, trigger, confidence = SIGNAL_EXIT, "stop", 0.8

    # 진입은 confirmed + 확신도 기준 충족 + 데이터 연결 시에만
    decision = "proceed"
    blocked = []
    if signal_type == SIGNAL_ENTRY:
        if watch["status"] != WATCH_CONFIRMED:
            blocked.append("not_confirmed")
        if confidence < min_conf:
            blocked.append("low_timing_confidence")
        if not connected:
            blocked.append("realtime_stale_or_disconnected")
    if blocked:
        decision = "blocked"

    signal = {
        "strategy_id": strategy_id,
        "market": market,
        "candidate_id": candidate_id,
        "symbol": candidate.get("symbol"),
        "instrument_type": candidate.get("instrument_type"),
        "signal_type": signal_type,
        "trigger": trigger,
        "ref_price": watch.get("initial_price"),
        "signal_price": price,
        "ai_timing_confidence": confidence,
        "decision": decision,
        "blocked_reason": ", ".join(blocked) if blocked else ("realtime_stale_or_disconnected" if stale else None),
        "automation_level": int(pol.get("automation_level") or 4),
        "data_as_of": data_as_of,
    }
    snapshot_repository.save_timing_signal(signal)
    return signal
