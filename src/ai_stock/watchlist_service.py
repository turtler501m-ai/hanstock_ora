# -*- coding: utf-8 -*-
"""탭5 관찰종목: 결정적 상태 머신 (§5.5).

AI 단독으로 confirmed가 되지 못한다. confirmed는 결정적 검증 로직만 결정한다.
stale 후보는 confirmed가 될 수 없다.
"""
from __future__ import annotations

from src.db import ai_scan_repository as scan_repository
from src.db import ai_watchlist_repository as watchlist_repository

from typing import Any

from src.ai_stock.constants import (
    DECISION_INSUFFICIENT,
    WATCH_CONFIRMED,
    WATCH_DISCOVERED,
    WATCH_STATUSES,
    WATCH_TRANSITIONS,
)
from src.ai_stock.freshness import is_stale


# confirmed 결정 기준 기본값(정책에서 override, §1.4)
DEFAULT_MIN_FINAL = 65.0
DEFAULT_MIN_RULE = 40.0
DEFAULT_MAX_RISK = 60.0


def register(candidate: dict[str, Any]) -> dict[str, Any]:
    cid = int(candidate["candidate_id"])
    watchlist_repository.upsert_watch(cid, {
        "market": candidate["market"],
        "symbol": candidate.get("symbol"),
        "status": WATCH_DISCOVERED,
        "initial_score": candidate.get("final_score"),
        "current_score": candidate.get("final_score"),
        "initial_price": candidate.get("current_price"),
        "current_price": candidate.get("current_price"),
        "related_narratives": candidate.get("related_narratives", []),
        "market_regime": candidate.get("market_regime"),
        "invalidation_conditions": candidate.get("invalidation_conditions", []),
    })
    watchlist_repository.update_watch_status(cid, WATCH_DISCOVERED, reason="registered")
    return watchlist_repository.get_watch(cid)


def _policy_for(candidate: dict[str, Any]) -> dict[str, Any]:
    pol = watchlist_repository.get_policy(candidate.get("strategy_id") or "ai_stock_default_v1", candidate["market"]) or {}
    return pol


def can_confirm(candidate: dict[str, Any]) -> tuple[bool, list[str]]:
    """confirmed 가능 여부 — 결정적 검증 (§5.5). AI 출력은 입력일 뿐."""
    reasons: list[str] = []
    pol = _policy_for(candidate)
    min_final = float(pol.get("min_final_score") or DEFAULT_MIN_FINAL)
    min_rule = float(pol.get("min_rule_score") or DEFAULT_MIN_RULE)
    max_risk = float(pol.get("max_risk_score") or DEFAULT_MAX_RISK)

    if candidate.get("decision") == DECISION_INSUFFICIENT:
        reasons.append("insufficient_data")
    if (candidate.get("final_score") or 0) < min_final:
        reasons.append(f"final_score<{min_final}")
    if (candidate.get("rule_score") or 0) < min_rule:
        reasons.append(f"rule_score<{min_rule}")
    if (candidate.get("risk_score") or 0) > max_risk:
        reasons.append(f"risk_score>{max_risk}")
    if is_stale(candidate.get("data_as_of"), "ai_eval"):
        reasons.append("stale_data")
    if not candidate.get("current_price"):
        reasons.append("no_entry_price")
    return (len(reasons) == 0, reasons)


def transition(candidate_id: int, to_status: str, *, reason: str | None = None) -> dict[str, Any]:
    if to_status not in WATCH_STATUSES:
        raise ValueError(f"invalid status {to_status}")
    current = watchlist_repository.get_watch(candidate_id)
    if current is None:
        raise ValueError("watch item not found")
    allowed = WATCH_TRANSITIONS.get(current["status"], set())
    if to_status not in allowed:
        raise ValueError(f"transition {current['status']}->{to_status} not allowed")

    if to_status == WATCH_CONFIRMED:
        candidate = scan_repository.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError("candidate not found for confirmation")
        ok, blocks = can_confirm(candidate)
        if not ok:
            raise ValueError("confirm blocked: " + ", ".join(blocks))

    watchlist_repository.update_watch_status(candidate_id, to_status, reason=reason)
    result = watchlist_repository.get_watch(candidate_id)
    result["market"] = current["market"]
    return result
