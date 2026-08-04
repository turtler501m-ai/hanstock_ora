# -*- coding: utf-8 -*-
"""AI스톡 라우터 (§7·F00·F02).

페이지(FileResponse)와 /api/ai-stock/* 엔드포인트.
저장소 기반 API는 완전 구현하고, 서비스 의존 API는 지연 import로 연결한다.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse

from src.dashboard.core import WEB_DIR
from src.ai_stock import constants as C
from src.ai_stock.markets import normalize_market, require_storable_market, MarketError
from src.ai_stock.safety import safety_state
from src.ai_stock.schemas import envelope
from src.db import ai_dashboard_repository as repo

router = APIRouter(tags=["ai-stock"])


def _market_param(market: str | None) -> str:
    if market is None or str(market).strip() == "":
        return C.MARKET_ALL
    try:
        return normalize_market(market)  # 잘못된 값은 흡수하지 않고 400
    except MarketError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _policy_view(policy: dict[str, Any] | None) -> dict[str, Any] | None:
    if not policy:
        return None
    p = dict(policy)
    level = int(p.get("automation_level") or C.DEFAULT_AUTOMATION_LEVEL)
    flags = []
    if level >= C.AUTOMATION_APPROVE and int(p.get("auto_approve") or 0):
        flags.append("auto_approval_enabled")
    if level >= C.AUTOMATION_EXECUTE and int(p.get("auto_execute") or 0):
        flags.append("auto_execute_enabled")
    if int(p.get("allow_fallback_trade") or 0):
        flags.append("fallback_trade_allowed")
    if int(p.get("allow_stale_data_trade") or 0):
        flags.append("stale_trade_allowed")
    if not int(p.get("enabled", 1)):
        flags.append("policy_disabled")
    p["risk_flags"] = flags
    p["next_blocked_stage"] = (
        "approve" if level < C.AUTOMATION_APPROVE else
        "execute" if level < C.AUTOMATION_EXECUTE else
        None
    )
    p["requires_live_guard_for_execute"] = level >= C.AUTOMATION_EXECUTE
    return p


# --------------------------------------------------------------------------- #
# 페이지 + 상태 + 설정 (§7.3 상태·설정)
# --------------------------------------------------------------------------- #
@router.get("/ai-stock", response_class=FileResponse)
def ai_stock_page():
    return FileResponse(WEB_DIR / "templates" / "ai_stock.html")


@router.get("/api/ai-stock/status")
def ai_stock_status():
    from src.config import config

    model_status = "ready" if getattr(config, "ai_strategy_enabled", False) and getattr(config, "openai_api_key", "") else "fallback"
    data = {
        "model": getattr(config, "openai_model", "gpt-5-mini"),
        "model_status": model_status,
        "markets": list(C.STORABLE_MARKETS),
        "automation_levels": list(C.AUTOMATION_LEVELS),
        "default_automation_level": C.DEFAULT_AUTOMATION_LEVEL,
    }
    return envelope(data, market=C.MARKET_ALL, meta={"data_quality": "good"})


@router.get("/api/ai-stock/decision-pipeline")
def ai_stock_decision_pipeline(
    market: str = Query(default=C.MARKET_KR),
    strategy_id: str | None = Query(default=None),
    limit: int = Query(default=40, ge=1, le=100),
):
    """Return one operational view from evidence through managed execution."""
    from src.ai_stock.decision_pipeline_service import build_pipeline

    m = require_storable_market(market)
    return envelope(
        build_pipeline(
            market=m,
            strategy_id=strategy_id or None,
            limit=limit,
        ),
        market=m,
    )


@router.get("/api/ai-stock/autonomy-audit")
def ai_stock_autonomy_audit(market: str | None = Query(default=None)):
    """Dashboard view of K01 autonomous-strategy implementation coverage."""
    from src.config import config

    m = _market_param(market)
    market_filter = m if m != C.MARKET_ALL else None
    positions = repo.list_strategy_positions(market=market_filter, active_only=False)
    active_positions = [p for p in positions if p.get("status") in {"pending_entry", "open", "exit_pending"}]
    decisions = repo.list_strategy_decisions(market=market_filter, limit=50)
    orders = repo.list_managed_orders(market=market_filter, limit=50)
    reservations = repo.list_risk_reservations(market=market_filter, limit=50)
    protections = repo.list_position_protections(market=market_filter)
    unprotected = repo.list_unprotected_strategy_positions(market=market_filter)
    policies = [_policy_view(p) for p in repo.list_policies(market=market_filter)]

    runtime_mode = {
        "autonomy_enabled": bool(getattr(config, "autonomy_enabled", False)),
        "autonomy_trading_env": str(getattr(config, "autonomy_trading_env", "demo")),
        "autonomy_require_approval": bool(getattr(config, "autonomy_require_approval", True)),
        "autonomy_enable_live_trading": bool(getattr(config, "autonomy_enable_live_trading", False)),
        "autonomy_live_opt_in": bool(getattr(config, "autonomy_live_opt_in", False)),
        "global_trading_env": str(getattr(config, "trading_env", "demo")),
        "dry_run": bool(getattr(config, "dry_run", True)),
        "enable_live_trading": bool(getattr(config, "enable_live_trading", False)),
        "live_hard_stop_adapter": "not_implemented",
    }
    coverage = _autonomy_coverage(runtime_mode)
    status_counts = {
        "positions": _count_by(positions, "status"),
        "decisions": _count_by(decisions, "final_action"),
        "orders": _count_by(orders, "status"),
        "reservations": _count_by(reservations, "status"),
        "protections": _count_by(protections, "status"),
    }
    blockers = [
        item["title"] for item in coverage if item["status"] in {"partial", "blocked"}
    ]
    if unprotected:
        blockers.append("미보호 전략 포지션 존재")
    if not runtime_mode["autonomy_enabled"]:
        blockers.append("AUTONOMY_ENABLED 비활성")
    return envelope(
        {
            "summary": {
                "overall_status": "partial",
                "judgement": "100% 완료 아님",
                "reason": "기존 AI스톡 스케줄에서 자율 판단과 canonical 승인 대기열까지 연결됐습니다. 실계좌 하드스톱 브로커 어댑터와 관리주문 제출·대조 운영은 계속 차단됩니다.",
                "runtime_mode": runtime_mode,
                "blockers": blockers,
            },
            "coverage": coverage,
            "status_counts": status_counts,
            "records": {
                "positions": positions[:50],
                "active_positions": active_positions[:50],
                "decisions": decisions,
                "managed_orders": orders,
                "risk_reservations": reservations,
                "position_protections": protections[:50],
                "unprotected_positions": unprotected,
                "automation_policies": policies,
            },
        },
        market=m,
    )


def _autonomy_coverage(runtime_mode: dict[str, Any]) -> list[dict[str, str]]:
    live_blocked = runtime_mode["live_hard_stop_adapter"] == "not_implemented"
    return [
        {
            "area": "전략별 포지션 소유권",
            "status": "implemented",
            "title": "strategy_positions 기반 가상 포지션",
            "evidence": "ai_strategy_positions, strategy_id/position_id 연결, 전략별 remaining_qty",
            "gap": "기존 일반 보유분을 자동 이관하는 전체 마이그레이션은 별도 운영 절차 필요",
        },
        {
            "area": "TradeIntent 스키마",
            "status": "implemented",
            "title": "구조화 거래 의도와 JSON Schema",
            "evidence": "TradeIntent, validate_trade_intent, OpenAI strict structured output",
            "gap": "전략별 프롬프트 품질 검증과 실데이터 샘플 검증은 계속 축적 필요",
        },
        {
            "area": "결정론 리스크",
            "status": "implemented",
            "title": "수량·손실·비중·신선도 fail-closed",
            "evidence": "RiskEnvelope, RiskLimits, RiskSnapshot, risk reservations",
            "gap": "상관관계 기반 집중 제한은 아직 명시 계산에 없음",
        },
        {
            "area": "시장 국면",
            "status": "partial",
            "title": "정량 국면 분류와 허용 국면 게이트",
            "evidence": "MarketRegimeClassifier, allowed_regimes",
            "gap": "실시간 지수/폭/환율/금리 빌더 완성도는 운영 데이터 소스 연결에 의존",
        },
        {
            "area": "보유 포지션 관리 루프",
            "status": "implemented",
            "title": "AI스톡 스케줄 기반 scan + manage_position 오케스트레이션",
            "evidence": "automation_service.run_strategy → run_ai_stock_autonomy_cycle → canonical approval queue",
            "gap": "상시 프로세스 대신 기존 DB 전략 스케줄 주기로 실행",
        },
        {
            "area": "주문 상태 머신",
            "status": "partial" if live_blocked else "implemented",
            "title": "관리 주문 상태와 체결 반영",
            "evidence": "ManagedOrderService, broker reconciliation, apply_fill",
            "gap": "실계좌 하드스톱 브로커 어댑터 미구현으로 live buy 제출 차단",
        },
        {
            "area": "승인·실행 파이프라인",
            "status": "partial",
            "title": "승인 전 재검증과 canonical approval 연결",
            "evidence": "ManagedApprovalBridge, AI스톡 스케줄 승인 생성, 대시보드 승인·거절 분기",
            "gap": "관리주문 브로커 제출과 체결 대조는 실계좌 보호 어댑터 완성 전까지 비활성",
        },
        {
            "area": "하드스톱 보호",
            "status": "blocked" if live_blocked else "implemented",
            "title": "체결 후 보호 없으면 신규 위험 차단",
            "evidence": "HardStopProtectionService, PaperProtectionBroker, UnavailableProtectionBroker",
            "gap": "KIS/해외 실계좌 조건부 손절 어댑터가 없어 실운영 매수 보호는 fail-closed",
        },
        {
            "area": "성과·건강 자동중단",
            "status": "implemented",
            "title": "전략 건강 평가와 halt 이벤트",
            "evidence": "StrategyHealthService, lifecycle health gate",
            "gap": "장기간 실거래 표본 기반 임계값 보정 필요",
        },
        {
            "area": "대시보드 현행화",
            "status": "implemented",
            "title": "K01 구현 감사 화면",
            "evidence": "/api/ai-stock/autonomy-audit, AI스톡 자율전략 감사 탭",
            "gap": "실시간 차트/개별 주문 액션 UI는 읽기 전용 이후 단계",
        },
    ]


def _count_by(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(field) or "none")
        counts[key] = counts.get(key, 0) + 1
    return counts


@router.get("/api/ai-stock/settings")
def ai_stock_settings():
    """AI스톡 관련 환경설정 그룹 조회 (§1.4)."""
    from src.config import config
    import os

    keys = {
        "AI_STRATEGY_ENABLED": getattr(config, "ai_strategy_enabled", False),
        "OPENAI_MODEL": getattr(config, "openai_model", "gpt-5-mini"),
        "AI_STOCK_SCAN_STALE_MIN": os.environ.get("AI_STOCK_SCAN_STALE_MIN", "30"),
    }
    return envelope({"settings": keys, "editable": sorted(_EDITABLE_ENV)}, market=C.MARKET_ALL)


# 환경설정 화면에서 변경 가능한 AI스톡 키 (§1.4). 안전 가드는 제외(별도 화면).
_EDITABLE_ENV = {
    "AI_STRATEGY_ENABLED", "OPENAI_MODEL", "OPENAI_TIMEOUT_SECONDS",
    "AI_STOCK_SCAN_STALE_MIN",
    "AI_STOCK_TTL_INTRADAY_PRICE_MIN", "AI_STOCK_TTL_DAILY_BAR_MIN",
    "AI_STOCK_TTL_MARKET_REGIME_MIN", "AI_STOCK_TTL_NARRATIVE_MIN",
    "AI_STOCK_TTL_AI_EVAL_MIN", "AI_STOCK_TTL_BRIEFING_DAILY_MIN",
}


@router.patch("/api/ai-stock/settings")
def ai_stock_settings_update(payload: dict = Body(...)):
    """AI스톡 환경설정 변경 (§1.4). 허용 키만, 범위 밖은 거부."""
    updates = payload.get("updates") if isinstance(payload.get("updates"), dict) else payload
    rejected = [k for k in updates if k not in _EDITABLE_ENV]
    if rejected:
        raise HTTPException(status_code=400, detail=f"not editable: {rejected}")
    try:
        from src.dashboard.services.env_service import write_env_values
        from src.dashboard.core import ENV_PATH

        clean = {k: str(v) for k, v in updates.items()}
        write_env_values(clean, ENV_PATH)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"failed to write settings: {exc}")
    return envelope({"updated": list(updates.keys())}, market=C.MARKET_ALL)


# --------------------------------------------------------------------------- #
# 시황 브리핑 (§4.7·§7.3) — 서비스 지연 연결
# --------------------------------------------------------------------------- #
@router.get("/api/ai-stock/overview")
def ai_stock_overview(market: str | None = Query(default=None)):
    m = _market_param(market)
    try:
        from src.ai_stock import briefing_service

        data = briefing_service.overview(m)
        return envelope(data, market=m)
    except ImportError:
        return envelope({"regimes": []}, market=m, errors=["briefing_service not available"])


@router.get("/api/ai-stock/briefings")
def ai_stock_briefings(market: str | None = Query(default=None), period: str = Query(default="daily")):
    m = require_storable_market(market) if market and market.upper() != "ALL" else C.MARKET_KR
    try:
        from src.ai_stock import briefing_service

        return envelope(briefing_service.list_briefings(m, period), market=m)
    except ImportError:
        return envelope({"briefings": []}, market=m, errors=["briefing_service not available"])


@router.get("/api/ai-stock/briefings/{period}/{key}")
def ai_stock_briefing_detail(period: str, key: str, market: str = Query(...)):
    m = require_storable_market(market)
    try:
        from src.ai_stock import briefing_service

        return envelope(briefing_service.get_briefing(m, period, key), market=m)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="briefing not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ImportError:
        raise HTTPException(status_code=503, detail="briefing_service not available")


@router.post("/api/ai-stock/briefings/generate")
def ai_stock_briefings_generate(market: str = Query(...), period: str = Query(default="daily")):
    m = require_storable_market(market)
    try:
        from src.ai_stock import briefing_service

        return envelope(briefing_service.generate(m, period), market=m)
    except ImportError:
        raise HTTPException(status_code=503, detail="briefing_service not available")


# --------------------------------------------------------------------------- #
# 유니버스 (§4.6·§7.3)
# --------------------------------------------------------------------------- #
@router.get("/api/ai-stock/universe")
def ai_stock_universe(market: str = Query(...)):
    m = require_storable_market(market)
    try:
        from src.ai_stock import universe

        return envelope(universe.describe(m), market=m)
    except ImportError:
        return envelope({"passed": [], "excluded": []}, market=m, errors=["universe not available"])


# --------------------------------------------------------------------------- #
# 내러티브 (§5.2)
# --------------------------------------------------------------------------- #
@router.get("/api/ai-stock/narratives")
def ai_stock_narratives(market: str | None = Query(default=None)):
    m = _market_param(market)
    try:
        from src.ai_stock import narrative_service

        return envelope(narrative_service.list_narratives(m), market=m)
    except ImportError:
        return envelope({"narratives": []}, market=m, errors=["narrative_service not available"])


# --------------------------------------------------------------------------- #
# 스캔 + 후보 (§5.3·§5.4)
# --------------------------------------------------------------------------- #
@router.post("/api/ai-stock/scans")
def ai_stock_create_scan(payload: dict = Body(default_factory=dict)):
    market = require_storable_market(payload.get("market"))
    strategy_id = str(payload.get("strategy_id") or "ai_stock_default_v1")
    try:
        from src.ai_stock import discovery_service

        result = discovery_service.run_scan(market=market, strategy_id=strategy_id, options=payload)
        return envelope(result, market=market)
    except repo.ScanConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ImportError:
        raise HTTPException(status_code=503, detail="discovery_service not available")


@router.get("/api/ai-stock/scans/{scan_id}")
def ai_stock_get_scan(scan_id: int):
    scan = repo.get_scan(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return envelope(scan, market=scan.get("market", C.MARKET_ALL))


@router.get("/api/ai-stock/candidates")
def ai_stock_candidates(
    market: str | None = Query(default=None),
    scan_id: int | None = Query(default=None),
    decision: str | None = Query(default=None),
    min_score: float | None = Query(default=None),
):
    m = _market_param(market)
    rows = repo.list_candidates(
        market=m if m != C.MARKET_ALL else None,
        scan_id=scan_id, decision=decision, min_score=min_score,
    )
    return envelope({"candidates": rows, "count": len(rows)}, market=m)


@router.get("/api/ai-stock/candidates/{candidate_id}")
def ai_stock_candidate_detail(candidate_id: int):
    cand = repo.get_candidate(candidate_id)
    if cand is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    try:
        from src.ai_stock import analysis_service

        cand = analysis_service.enrich(cand)
    except ImportError:
        pass
    return envelope(cand, market=cand.get("market", C.MARKET_ALL))


# --------------------------------------------------------------------------- #
# 관찰종목 (§5.5)
# --------------------------------------------------------------------------- #
@router.get("/api/ai-stock/watchlist")
def ai_stock_watchlist(market: str | None = Query(default=None), status: str | None = Query(default=None)):
    m = _market_param(market)
    rows = repo.list_watchlist(market=m if m != C.MARKET_ALL else None, status=status)
    return envelope({"watchlist": rows, "count": len(rows)}, market=m)


@router.post("/api/ai-stock/watchlist")
def ai_stock_watchlist_add(payload: dict = Body(...)):
    candidate_id = int(payload.get("candidate_id") or 0)
    if candidate_id <= 0:
        raise HTTPException(status_code=400, detail="candidate_id is required")
    cand = repo.get_candidate(candidate_id)
    if cand is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    try:
        from src.ai_stock import watchlist_service

        result = watchlist_service.register(cand)
    except ImportError:
        repo.upsert_watch(candidate_id, {
            "market": cand["market"], "symbol": cand.get("symbol"),
            "status": C.WATCH_DISCOVERED,
            "initial_score": cand.get("final_score"), "current_score": cand.get("final_score"),
            "initial_price": cand.get("current_price"), "current_price": cand.get("current_price"),
        })
        result = repo.get_watch(candidate_id)
    return envelope(result, market=cand["market"])


@router.patch("/api/ai-stock/watchlist/{candidate_id}")
def ai_stock_watchlist_update(candidate_id: int, payload: dict = Body(...)):
    to_status = str(payload.get("status") or "").strip()
    if to_status not in C.WATCH_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {C.WATCH_STATUSES}")
    try:
        from src.ai_stock import watchlist_service

        result = watchlist_service.transition(candidate_id, to_status, reason=payload.get("reason"))
        return envelope(result, market=result.get("market", C.MARKET_ALL))
    except ImportError:
        current = repo.get_watch(candidate_id)
        if current is None:
            raise HTTPException(status_code=404, detail="watch item not found")
        allowed = C.WATCH_TRANSITIONS.get(current["status"], set())
        if to_status not in allowed:
            raise HTTPException(status_code=409, detail=f"transition {current['status']}->{to_status} not allowed")
        repo.update_watch_status(candidate_id, to_status, reason=payload.get("reason"))
        return envelope(repo.get_watch(candidate_id), market=current["market"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.delete("/api/ai-stock/watchlist/{candidate_id}")
def ai_stock_watchlist_remove(candidate_id: int):
    repo.remove_watch(candidate_id)
    return envelope({"removed": candidate_id}, market=C.MARKET_ALL)


# --------------------------------------------------------------------------- #
# 포트폴리오 · 성과 · 전략 (§5.6·5.7·5.8)
# --------------------------------------------------------------------------- #
@router.get("/api/ai-stock/portfolio")
def ai_stock_portfolio(market: str | None = Query(default=None), display_currency: str = Query(default="LOCAL")):
    m = _market_param(market)
    try:
        from src.ai_stock import portfolio_service

        return envelope(portfolio_service.summary(m, display_currency), market=m)
    except ImportError:
        return envelope({"positions": [], "weights": {}}, market=m, errors=["portfolio_service not available"])


@router.get("/api/ai-stock/performance")
def ai_stock_performance(market: str | None = Query(default=None)):
    m = _market_param(market)
    rows = repo.list_performance(market=m if m != C.MARKET_ALL else None)
    try:
        from src.ai_stock import performance_service

        summary = performance_service.summarize(rows, m)
    except ImportError:
        summary = {"sample": len(rows)}
    return envelope({"performance": rows, "summary": summary}, market=m)


@router.post("/api/ai-stock/performance/refresh")
def ai_stock_performance_refresh(market: str | None = Query(default=None)):
    m = _market_param(market)
    from src.ai_stock import performance_service

    return envelope(performance_service.run_update(m), market=m)


@router.get("/api/ai-stock/strategies")
def ai_stock_strategies(market: str | None = Query(default=None)):
    m = _market_param(market)
    try:
        from src.db.strategy_repository import load_ai_strategies

        strategies = load_ai_strategies()
    except Exception:
        strategies = []
    return envelope({"strategies": strategies}, market=m)


def _load_strategy_or_404(strategy_id: str) -> dict[str, Any]:
    from src.db.strategy_repository import load_ai_strategies

    for item in load_ai_strategies():
        if str(item.get("id")) == str(strategy_id):
            return item
    raise HTTPException(status_code=404, detail="strategy not found")


@router.post("/api/ai-stock/strategies/{strategy_id}/validate")
def ai_stock_strategy_validate(strategy_id: str, market: str = Query(...)):
    m = require_storable_market(market)
    strategy = _load_strategy_or_404(strategy_id)
    profile = strategy.get("profile") or {}
    weights = profile.get("weights") or {}
    errors = []
    if weights:
        total = sum(float(v or 0) for v in weights.values())
        if abs(total - 1.0) > 0.001:
            errors.append("weights_sum")
    strategy_market = str(profile.get("market") or strategy.get("market") or m).upper()
    if strategy_market not in ("ALL", m):
        errors.append("market_mismatch")
    result = {
        "strategy_id": strategy_id,
        "market": m,
        "status": "passed" if not errors else "failed",
        "errors": errors,
        "profile_hash": strategy.get("profile_hash"),
        "strategy_version": strategy.get("strategy_version"),
    }
    return envelope(result, market=m)


@router.post("/api/ai-stock/strategies/{strategy_id}/select")
def ai_stock_strategy_select(strategy_id: str, payload: dict = Body(default_factory=dict)):
    m = require_storable_market(payload.get("market"))
    from src.db.strategy_repository import load_ai_strategies, save_ai_strategies, record_ai_strategy_event

    strategies = load_ai_strategies()
    found = None
    for item in strategies:
        selected = str(item.get("id")) == str(strategy_id)
        if selected:
            found = item
        item["selected"] = bool(selected)
    if found is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    save_ai_strategies(strategies)
    record_ai_strategy_event(strategy_id, "ai_stock_selected", {"market": m}, found.get("strategy_version"))
    return envelope({"strategy_id": strategy_id, "selected": True}, market=m)


# --------------------------------------------------------------------------- #
# 실행 계획 (§5.9)
# --------------------------------------------------------------------------- #
@router.get("/api/ai-stock/execution-plans")
def ai_stock_execution_plans(market: str | None = Query(default=None)):
    m = _market_param(market)
    rows = repo.list_execution_plans(market=m if m != C.MARKET_ALL else None)
    return envelope({"plans": rows, "count": len(rows)}, market=m)


@router.post("/api/ai-stock/execution-plans")
def ai_stock_create_plan(payload: dict = Body(...)):
    candidate_id = int(payload.get("candidate_id") or 0)
    try:
        from src.ai_stock import execution_plan_service

        result = execution_plan_service.create_plan(candidate_id, options=payload)
        return envelope(result, market=result.get("market", C.MARKET_ALL))
    except ImportError:
        raise HTTPException(status_code=503, detail="execution_plan_service not available")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/ai-stock/execution-plans/{plan_id}/queue-approval")
def ai_stock_queue_approval(plan_id: int):
    plan = repo.get_execution_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    candidate = repo.get_candidate(int(plan.get("candidate_id") or 0))
    if not candidate:
        raise HTTPException(status_code=404, detail="candidate not found")
    try:
        from src.ai_stock.automation_service import _queue_approval, evaluate_manual_approval_gate

        strategy_id = str(plan.get("strategy_id") or "ai_stock_default_v1")
        policy = repo.get_policy(strategy_id, plan["market"]) or {}
        gate = evaluate_manual_approval_gate(policy=policy, candidate=candidate, plan=plan)
        if not gate["proceed"]:
            repo.log_execution_run({
                "strategy_id": strategy_id,
                "market": plan["market"],
                "scan_id": candidate.get("scan_id"),
                "candidate_id": candidate.get("candidate_id"),
                "plan_id": plan_id,
                "run_type": "manual_approval",
                "automation_level": gate["automation_level"],
                "status": "blocked",
                "blocked_stage": "manual_approval",
                "blocked_reason": ",".join(gate["blocked_reason"]),
                "policy_snapshot": policy,
            })
            raise HTTPException(status_code=409, detail="approval blocked: " + ", ".join(gate["blocked_reason"]))

        aid = _queue_approval(plan["market"], candidate, plan, strategy_id)
        approval_db = "main" if plan["market"] == C.MARKET_KR else "mistock"
        repo.update_execution_plan_approval(
            plan_id,
            approval_market=plan["market"],
            approval_db=approval_db,
            approval_id=aid,
            approval_status="pending",
        )
        updated = repo.get_execution_plan(plan_id)
        return envelope({"plan": updated, "approval_id": aid}, market=plan["market"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# --------------------------------------------------------------------------- #
# 자동화 정책 · 실행 이력 (§6.6·6.7)
# --------------------------------------------------------------------------- #
@router.get("/api/ai-stock/automation-policies")
def ai_stock_policies(market: str | None = Query(default=None), strategy_id: str | None = Query(default=None)):
    m = _market_param(market)
    if strategy_id and m in C.STORABLE_MARKETS:
        policy = repo.get_policy(strategy_id, m)
        return envelope({"policy": _policy_view(policy)}, market=m)
    policies = [_policy_view(p) for p in repo.list_policies(market=m if m != C.MARKET_ALL else None)]
    return envelope({"policies": policies}, market=m)


@router.put("/api/ai-stock/automation-policies/{strategy_id}")
def ai_stock_policy_upsert(strategy_id: str, payload: dict = Body(...)):
    m = require_storable_market(payload.get("market"))
    try:
        from src.ai_stock import automation_service

        result = automation_service.set_policy(strategy_id, m, payload)
    except ImportError:
        result = repo.upsert_policy(strategy_id, m, payload)
    return envelope({"policy": _policy_view(result)}, market=m)


@router.get("/api/ai-stock/automation-runs")
def ai_stock_runs(market: str | None = Query(default=None), strategy_id: str | None = Query(default=None)):
    m = _market_param(market)
    rows = repo.list_execution_runs(market=m if m != C.MARKET_ALL else None, strategy_id=strategy_id)
    return envelope({"runs": rows, "count": len(rows)}, market=m)


# --------------------------------------------------------------------------- #
# 2차 실시간 타이밍 신호 (§4.8·§6.8)
# --------------------------------------------------------------------------- #
@router.get("/api/ai-stock/timing-signals")
def ai_stock_timing(market: str | None = Query(default=None), candidate_id: int | None = Query(default=None)):
    m = _market_param(market)
    rows = repo.list_timing_signals(market=m if m != C.MARKET_ALL else None, candidate_id=candidate_id)
    return envelope({"signals": rows, "count": len(rows)}, market=m)


@router.post("/api/ai-stock/timing-signals/scan")
def ai_stock_timing_scan(market: str = Query(...), strategy_id: str = Query(default="ai_stock_default_v1")):
    """2차 실시간 사이클 1회 실행 (§4.8)."""
    m = require_storable_market(market)
    from src.ai_stock import realtime_service

    return envelope(realtime_service.run_realtime_cycle(m, strategy_id=strategy_id), market=m)
