"""Dashboard projection for the AI-led stock decision pipeline."""
from __future__ import annotations

from src.db import ai_dashboard_repository as repo

import json
from pathlib import Path
from typing import Any

from src.ai_stock import constants as C
from src.ai_stock.automation_service import evaluate_gate
from src.config import config



def _latest_scan(market: str, strategy_id: str | None) -> dict[str, Any] | None:
    scans = repo.list_scans(market=market, limit=100)
    if strategy_id:
        scans = [row for row in scans if row.get("strategy_id") == strategy_id]
    return scans[0] if scans else None


def _latest_by(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    result: dict[Any, dict[str, Any]] = {}
    for row in rows:
        value = row.get(key)
        if value is not None and value not in result:
            result[value] = row
    return result


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []
    return []


def _action(decision: str | None) -> str:
    return {
        C.DECISION_STRONG_WATCH: "매수 검토",
        C.DECISION_WATCH: "관찰",
        C.DECISION_NEUTRAL: "보류",
        C.DECISION_AVOID: "회피",
        C.DECISION_INSUFFICIENT: "판단 불가",
    }.get(str(decision), "판단 불가")


def _thesis(candidate: dict[str, Any]) -> dict[str, Any]:
    narratives = _as_list(candidate.get("related_narratives"))
    positives = _as_list(candidate.get("positive_factors"))
    negatives = _as_list(candidate.get("negative_factors"))
    invalidations = _as_list(candidate.get("invalidation_conditions"))
    warnings = _as_list(candidate.get("warnings"))
    ai_ready = bool(
        candidate.get("ai_score")
        and candidate.get("confidence") is not None
        and not candidate.get("fallback_used")
    )
    if ai_ready:
        source = "AI 판단"
        rationale = positives or narratives or ["AI 모델 평가가 적용됐습니다."]
    elif narratives:
        source = "뉴스 내러티브"
        rationale = narratives
    else:
        source = "수치 규칙 대체"
        rationale = ["AI 또는 종목별 뉴스 근거가 없어 수치 규칙 결과를 표시합니다."]
    return {
        "action": _action(candidate.get("decision")),
        "source": source,
        "ai_ready": ai_ready,
        "confidence": candidate.get("confidence"),
        "rationale": rationale[:5],
        "risks": (negatives + warnings)[:5],
        "invalidation_conditions": invalidations[:5],
    }


def _candidate_view(
    candidate: dict[str, Any],
    policy: dict[str, Any],
    watch: dict[str, Any] | None,
    plan: dict[str, Any] | None,
    run: dict[str, Any] | None,
) -> dict[str, Any]:
    gate = evaluate_gate(policy=policy, candidate=candidate, stage="plan")
    return {
        **candidate,
        "thesis": _thesis(candidate),
        "evidence": {
            "narratives": _as_list(candidate.get("related_narratives"))[:5],
            "narrative_score": candidate.get("narrative_score"),
            "data_quality": candidate.get("data_quality"),
            "data_as_of": candidate.get("data_as_of"),
            "market_regime": candidate.get("market_regime"),
        },
        "risk_gate": {
            "proceed": gate["proceed"],
            "blocked_reason": gate["blocked_reason"],
            "risk_score": candidate.get("risk_score"),
            "policy": {
                "max_risk_score": policy.get("max_risk_score"),
                "max_daily_loss_pct": policy.get("max_daily_loss_pct"),
                "max_position_pct": policy.get("max_position_pct"),
            },
        },
        "workflow": {
            "watch_status": watch.get("status") if watch else None,
            "plan_id": plan.get("id") if plan else None,
            "plan_status": plan.get("status") if plan else None,
            "approval_id": plan.get("approval_id") if plan else None,
            "approval_status": plan.get("approval_status") if plan else None,
            "last_run_status": run.get("status") if run else None,
            "last_blocked_stage": run.get("blocked_stage") if run else None,
            "last_blocked_reason": run.get("blocked_reason") if run else None,
        },
    }


def _heartbeat() -> dict[str, Any]:
    try:
        return json.loads(
            Path(".runtime/autonomy/heartbeat.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {"state": "unknown", "detail": "heartbeat unavailable"}


def build_pipeline(
    *,
    market: str = C.MARKET_KR,
    strategy_id: str | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    latest_scan = _latest_scan(market, strategy_id)
    candidates = (
        repo.list_candidates(scan_id=int(latest_scan["id"]), limit=limit)
        if latest_scan
        else []
    )
    policies = repo.list_policies(market=market)
    policy_by_strategy = {row["strategy_id"]: row for row in policies}
    watches = _latest_by(repo.list_watchlist(market=market), "candidate_id")
    plans = _latest_by(repo.list_execution_plans(market=market, limit=500), "candidate_id")
    runs = _latest_by(
        repo.list_execution_runs(market=market, strategy_id=strategy_id, limit=500),
        "candidate_id",
    )
    projected = []
    for candidate in candidates:
        cid = int(candidate["candidate_id"])
        projected.append(
            _candidate_view(
                candidate,
                policy_by_strategy.get(candidate.get("strategy_id"), {}),
                watches.get(cid),
                plans.get(cid),
                runs.get(cid),
            )
        )

    orders = repo.list_managed_orders(
        market=market, strategy_id=strategy_id, limit=30
    )
    decisions = repo.list_strategy_decisions(
        market=market, strategy_id=strategy_id, limit=30
    )
    heartbeat = _heartbeat()
    summary = {
        "candidate_count": len(projected),
        "ai_ready_count": sum(1 for row in projected if row["thesis"]["ai_ready"]),
        "news_evidence_count": sum(
            1 for row in projected if row["evidence"]["narratives"]
        ),
        "risk_pass_count": sum(1 for row in projected if row["risk_gate"]["proceed"]),
        "planned_count": sum(1 for row in projected if row["workflow"]["plan_id"]),
        "managed_order_count": len(orders),
        "decision_count": len(decisions),
    }
    blockers = []
    if not bool(getattr(config, "ai_strategy_enabled", False)):
        blockers.append("AI_STRATEGY_ENABLED 비활성")
    if heartbeat.get("state") != "running":
        blockers.append(f"자율 서비스 {heartbeat.get('state')}: {heartbeat.get('detail')}")
    if projected and not summary["ai_ready_count"]:
        blockers.append("최신 후보에 실제 AI 판단이 적용되지 않음")
    if projected and not summary["risk_pass_count"]:
        blockers.append("위험 게이트를 통과한 후보가 없음")

    return {
        "market": market,
        "strategy_id": strategy_id,
        "summary": summary,
        "blockers": blockers,
        "service": {
            "ai_enabled": bool(getattr(config, "ai_strategy_enabled", False)),
            "model": getattr(config, "openai_model", None),
            "autonomy_enabled": bool(getattr(config, "autonomy_enabled", False)),
            "approval_required": bool(
                getattr(config, "autonomy_require_approval", True)
            ),
            "heartbeat": heartbeat,
        },
        "latest_scan": latest_scan,
        "candidates": projected,
        "orders": orders,
        "decisions": decisions,
        "recent_runs": repo.list_execution_runs(
            market=market, strategy_id=strategy_id, limit=20
        ),
    }
