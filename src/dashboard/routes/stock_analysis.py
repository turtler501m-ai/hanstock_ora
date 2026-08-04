"""Analysis and strategy HTTP handlers."""

import functools
import inspect
from fastapi import APIRouter
from src.dashboard.routes import stock as _stock

def _refresh_legacy_dependencies() -> None:
    globals().update({
        name: value for name, value in vars(_stock).items()
        if name not in {"router", "_refresh_legacy_dependencies", "_CompatRouter", "_stock"}
        and not name.startswith("__")
    })

class _CompatRouter(APIRouter):
    def api_route(self, path: str, **kwargs):
        register = super().api_route(path, **kwargs)
        def decorator(endpoint):
            @functools.wraps(endpoint)
            def dispatch(*args, **inner_kwargs):
                _refresh_legacy_dependencies()
                return endpoint(*args, **inner_kwargs)
            register(dispatch)
            return endpoint
        return decorator

_refresh_legacy_dependencies()
router = _CompatRouter(tags=["stock", "stock-analysis"])

@router.get("/api/ai-strategies")
def get_ai_strategies():
    from src.db.repository import load_ai_strategies
    return {"strategies": [_strategy_api_payload(strategy) for strategy in load_ai_strategies()]}


@router.post("/api/ai-strategies/{id}/autonomy/run")
def run_ai_strategy_autonomy(id: str, payload: dict = Body(default_factory=dict)):
    """Run guarded autonomy from the main Hanstock AI strategy screen."""
    from src.config import config
    from src.db.repository import load_ai_strategies

    strategy = next(
        (
            item
            for item in load_ai_strategies()
            if str(item.get("id")) == str(id)
        ),
        None,
    )
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if not bool(getattr(config, "autonomy_enabled", False)):
        raise HTTPException(
            status_code=409,
            detail="AUTONOMY_ENABLED=true is required",
        )
    market = str(payload.get("market") or "KR").upper()
    if market != "KR":
        raise HTTPException(
            status_code=400,
            detail="Hanstock main AI strategy autonomy currently supports KR",
        )
    from src.ai_stock.automation_service import run_strategy

    result = run_strategy(
        market=market,
        strategy_id=id,
        run_type="dashboard_manual",
    )
    return {
        "ok": not bool(result.get("autonomy", {}).get("error")),
        **result,
    }


def _qualify_demo_strategy_one_click(strategy_id: str) -> dict:
    """Run every lifecycle gate in the explicitly enabled environment."""
    from src.config import config
    from src.db.repository import load_ai_strategies

    from src.strategy.autonomy.ai_stock_integration import (
        _autonomy_execution_enabled,
    )

    if not bool(getattr(config, "autonomy_enabled", False)) or not (
        _autonomy_execution_enabled()
    ):
        raise HTTPException(
            status_code=409,

            detail="one-click qualification requires an enabled autonomy environment",
        )
    steps: list[dict] = []
    static_result = static_verify_ai_strategy(strategy_id)
    steps.append({"step": "static", "ok": bool(static_result["result"].get("success"))})
    if not steps[-1]["ok"]:
        raise HTTPException(status_code=409, detail="Static validation failed")
    api_result = verify_ai_strategy(strategy_id)
    steps.append({"step": "api", "ok": bool(api_result.get("success"))})
    if not steps[-1]["ok"]:
        raise HTTPException(status_code=409, detail="API validation failed")
    backtest_result = backtest_ai_strategy(strategy_id)
    steps.append(
        {"step": "backtest", "ok": bool(backtest_result["result"].get("success"))}
    )
    if not steps[-1]["ok"]:
        raise HTTPException(status_code=409, detail="Backtest failed")
    start_ai_strategy_paper(strategy_id)
    current = next(
        item
        for item in load_ai_strategies()
        if str(item.get("id")) == str(strategy_id)
    )
    risk = (current.get("profile") or {}).get("risk") or {}
    required_days = max(1, int(risk.get("paper_trading_required_days") or 1))
    paper_result = complete_ai_strategy_paper(
        strategy_id,
        PaperCompletePayload(
            days=required_days,
            observations=max(5, required_days),
            pass_result=True,
            notes="one-click simulated paper qualification",
        ),
    )
    steps.append(
        {"step": "paper", "ok": bool(paper_result["result"].get("success"))}
    )
    approved = approve_ai_strategy(strategy_id)
    steps.append(
        {
            "step": "strategy_approval",
            "ok": str(approved["strategy"].get("status")) == "approved",
        }
    )
    from src.db import ai_watchlist_repository as ai_stock_repository

    ai_stock_repository.upsert_policy(
        strategy_id,
        "KR",
        {
            "enabled": 1,
            "automation_level": 5,
            "auto_approve": 1,
            "auto_execute": 1,
        },
    )
    steps.append({"step": "automation_policy", "ok": True})
    return {
        "mode": "one_click",
        "environment": str(getattr(config, "autonomy_trading_env", "demo")),

        "steps": steps,
    }


@router.post("/api/autonomy/managed-orders/{order_id}/cancel")
def cancel_autonomy_managed_order(order_id: int):
    """Cancel a managed order through its canonical state machine."""
    from src.strategy.autonomy.ai_stock_integration import (
        cancel_managed_ai_stock_order,
    )

    try:
        result = cancel_managed_ai_stock_order(int(order_id))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": result["status"] == "canceled", **result}


@router.get("/api/strategy-context")
def get_strategy_context(strategy_id: str | None = None):
    from src.db.repository import load_ai_strategies
    from src.strategy_ids import INDEPENDENT_STOCK_SCHEDULE_IDS
    from src.dashboard.services.analysis_cycle_service import (
        ISOLATED_STRATEGY_IDS,
        get_latest_usable_analysis_cycle,
    )

    strategies = load_ai_strategies()
    active = next(
        (
            strategy
            for strategy in strategies
            if strategy_id
            and (
                str(strategy.get("id")) == str(strategy_id)
                or str(strategy.get("model")) == str(strategy_id)
            )
        ),
        None,
    )
    if strategy_id and active is None:
        raise HTTPException(status_code=404, detail=f"strategy not found: {strategy_id}")
    if active is None:
        active = next((strategy for strategy in strategies if strategy.get("selected")), None)
    if active is None and strategies:
        active = strategies[0]
    active_strategy_id = str(active.get("id")) if active else None
    isolated = active_strategy_id in ISOLATED_STRATEGY_IDS
    analysis_cycle = (
        None
        if isolated or not active_strategy_id
        else get_latest_usable_analysis_cycle(active_strategy_id, trader.config.trading_env)
    )
    profile = active.get("profile") if active else {}
    active_gate = _approval_gate(active) if active else {"ok": False, "missing": ["active strategy"]}
    active_operation = _operation_status(active) if active else {
        "ready": False,
        "mode": "blocked",
        "selected": False,
        "approved": False,

        "dry_run": bool(trader.config.dry_run),
        "live_enabled": bool(trader.config.enable_live_trading),
        "reason": "active strategy is missing",
    }
    active_gate["label"] = _approval_gate_label(active_gate)
    active_operation["label"] = _operation_status_label(active_operation)
    active_operation["reason_label"] = _operation_reason_label(active_operation)
    applied_strategies = [
        {
            "id": strategy.get("id"),
            "name": _strategy_display_name(strategy.get("id"), strategy.get("name")),
        }
        for strategy in strategies
        if strategy.get("selected")
        and str(strategy.get("status") or "") == "approved"
        and str(strategy.get("id") or "") not in INDEPENDENT_STOCK_SCHEDULE_IDS
    ]
    return {
        "applied_strategies": applied_strategies,
        "applied_strategy_count": len(applied_strategies),
        "active_strategy": {
            "id": active.get("id") if active else None,
            "name": active.get("name") if active else None,
            "display_name": _strategy_display_name(active.get("id"), active.get("name")) if active else "-",
            "model": (profile or {}).get("model") or (active.get("model") if active else None),
            "ai_weight": (profile or {}).get("ai_weight") if active else 0.0,
            "status": active.get("status") if active else None,
            "status_label": _strategy_status_label(active.get("status")) if active else "-",
            "strategy_version": active.get("strategy_version") if active else None,
            "profile_hash": active.get("profile_hash") if active else None,
            "last_used_at": active.get("last_used_at") if active else None,
            "approval_gate": active_gate,
            "operation_status": active_operation,
        },
        "analysis_flow": {
            "isolated": isolated,
            "cycle": analysis_cycle,
        },
        "safety": {
            "trading_env": trader.config.trading_env,
            "dry_run": bool(trader.config.dry_run),
            "enable_live_trading": bool(trader.config.enable_live_trading),
            "require_approval": bool(trader.config.require_approval),
        },
        "fallback": {
            "mode": "rule_based" if not bool(getattr(trader.config, "ai_strategy_enabled", False)) else "",
            "openai_configured": bool(str(getattr(trader.config, "openai_api_key", "") or "").strip()),
        },
    }


@router.post("/api/analysis-cycles")
def start_analysis_cycle(payload: dict = Body(default_factory=dict)):
    from src.dashboard.services.analysis_cycle_service import (
        AnalysisCycleError,
        start_common_analysis_cycle,
    )

    requested_strategy_id = str(payload.get("strategy_id") or "").strip()
    strategy = stock_service.resolve_dashboard_strategy(requested_strategy_id or None)

    if requested_strategy_id and strategy is None:
        raise HTTPException(status_code=404, detail=f"strategy not found: {requested_strategy_id}")
    strategy_id = str(strategy.get("id")) if strategy else "seven_split"
    try:
        cycle = start_common_analysis_cycle(
            strategy_id,
            trader.config.trading_env,
            mode=str(payload.get("mode") or "analysis"),
        )
    except AnalysisCycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "cycle": cycle}




@router.post("/api/ai-strategies")
def create_ai_strategy(payload: NewStrategyPayload):
    from src.db.repository import create_ai_strategy_record
    import time
    import uuid

    new_id = f"strategy_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    try:
        new_strat = create_ai_strategy_record({
            "id": new_id,
            "name": payload.name,
            "provider": "openai" if payload.model != "none" else "none",
            "model": payload.model,
            "weight": payload.weight,
            "description": payload.description,
            "selected": False,
            "status": "approved",
            "profile": payload.profile,
            "strategy_version": 1,
        })
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "strategy": new_strat}


@router.patch("/api/ai-strategies/{id}")
def update_ai_strategy(id: str, payload: UpdateStrategyPayload):
    from src.db.repository import update_ai_strategy_record

    try:
        found = update_ai_strategy_record(
            id,
            payload.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        status_code = 404 if str(exc) == "strategy not found" else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return {"ok": True, "strategy": found}


@router.delete("/api/ai-strategies/{id}")
def delete_ai_strategy(id: str):
    from src.db.repository import delete_ai_strategy_record


    if id in {"gpt_5_mini_default", "rule_only_default"}:
        raise HTTPException(status_code=409, detail="Built-in strategy cannot be deleted")
    try:
        delete_ai_strategy_record(id)
    except ValueError as exc:
        status_code = 404 if str(exc) == "strategy not found" else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return {"ok": True}




@router.post("/api/ai-strategies/{id}/select")
def select_ai_strategy(id: str, payload: SelectStrategyPayload):
    from src.db.repository import set_ai_strategy_selected

    try:
        found = set_ai_strategy_selected(id, payload.selected)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "strategy": found}


@router.post("/api/ai-strategies/selection")
def replace_ai_strategy_selection(payload: StrategySelectionPayload):
    """Replace the enabled AI strategy selection in one transaction."""
    from src.db.repository import (
        load_ai_strategies,
        replace_ai_strategy_selection as replace_selection,
    )
    strategies = load_ai_strategies()
    mutable_ids = [
        str(item.get("id") or "")
        for item in strategies
    ]
    selectable_ids = {
        str(item.get("id") or "")
        for item in strategies
        if str(item.get("status") or "") == "approved"
    }
    requested_ids = list(dict.fromkeys(
        str(strategy_id).strip()
        for strategy_id in payload.strategy_ids
        if str(strategy_id).strip()
    ))
    invalid_ids = sorted(set(requested_ids) - selectable_ids)
    if invalid_ids:
        raise HTTPException(
            status_code=409,
            detail=(
                "사용하도록 선택할 수 없는 전략입니다: "
                + ", ".join(invalid_ids)
            ),
        )
    try:
        updated = replace_selection(
            requested_ids,
            mutable_strategy_ids=mutable_ids,
        )
    except ValueError as exc:

        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "selected_strategy_ids": [
            str(item.get("id"))
            for item in updated
            if item.get("selected")
        ],
        "strategies": [_strategy_api_payload(item) for item in updated],
    }


def _auto_validate_selected_strategy(strategy_id: str) -> dict:
    """Run the standard gates and approve one explicitly selected strategy."""
    from src.db.repository import load_ai_strategies

    steps = []
    static_result = static_verify_ai_strategy(strategy_id)
    static_ok = bool(static_result.get("result", {}).get("ok"))
    steps.append({"step": "static", "ok": static_ok})
    if not static_ok:
        return {"ok": False, "strategy_id": strategy_id, "steps": steps}

    current = next(
        item for item in load_ai_strategies()
        if str(item.get("id")) == str(strategy_id)
    )
    if str(current.get("provider") or "none") != "none":
        api_result = verify_ai_strategy(strategy_id)
        api_ok = bool(api_result.get("success"))
        steps.append({"step": "api", "ok": api_ok})
        if not api_ok:
            return {"ok": False, "strategy_id": strategy_id, "steps": steps}

    backtest_result = backtest_ai_strategy(strategy_id)
    backtest_ok = bool(backtest_result.get("result", {}).get("success"))
    steps.append({"step": "backtest", "ok": backtest_ok})
    if not backtest_ok:
        return {"ok": False, "strategy_id": strategy_id, "steps": steps}

    current = next(
        item for item in load_ai_strategies()
        if str(item.get("id")) == str(strategy_id)
    )
    gate = _approval_gate(current)
    if "paper trading" in gate.get("missing", []):
        if not (bool(trader.config.dry_run) or str(trader.config.trading_env).lower() != "real"):
            steps.append({"step": "paper", "ok": False, "reason": "manual paper validation required in real mode"})
            return {"ok": False, "strategy_id": strategy_id, "steps": steps}
        risk = (current.get("profile") or {}).get("risk") or {}
        required_days = max(1, int(risk.get("paper_trading_required_days") or 1))
        paper_result = complete_ai_strategy_paper(
            strategy_id,
            PaperCompletePayload(
                days=required_days,
                observations=max(5, required_days),
                pass_result=True,
                notes="automatic demo qualification after static/API/backtest gates",
            ),
        )

        paper_ok = bool(paper_result.get("result", {}).get("success"))
        steps.append({"step": "paper", "ok": paper_ok})
        if not paper_ok:
            return {"ok": False, "strategy_id": strategy_id, "steps": steps}

    approved = approve_ai_strategy(strategy_id)
    approved_ok = str(approved.get("strategy", {}).get("status")) == "approved"
    steps.append({"step": "approval", "ok": approved_ok})
    return {
        "ok": approved_ok,
        "strategy_id": strategy_id,
        "steps": steps,
        "strategy": _strategy_api_payload(approved["strategy"]),
    }


@router.post("/api/ai-strategies/apply-selected")
def apply_selected_ai_strategies():
    """Apply every selected strategy to the shared AI schedule slot."""
    from src.db.repository import (
        load_ai_strategies,
        record_ai_strategy_event,
    )
    from src.strategy_ids import INDEPENDENT_STOCK_SCHEDULE_IDS

    all_selected = [
        item for item in load_ai_strategies()
        if item.get("selected")
        and str(item.get("status") or "") == "approved"
    ]
    if not all_selected:
        raise HTTPException(status_code=409, detail="Select at least one AI strategy")
    selected = [
        item for item in all_selected
        if str(item.get("id") or "") not in INDEPENDENT_STOCK_SCHEDULE_IDS
    ]
    independent_ids = [
        str(item["id"])
        for item in all_selected
        if str(item.get("id") or "") in INDEPENDENT_STOCK_SCHEDULE_IDS
    ]
    strategy_ids = [str(item["id"]) for item in selected]
    for strategy in selected:
        record_ai_strategy_event(
            str(strategy["id"]),
            "applied_to_shared_schedule",
            {
                "verification_mode": "demo_account_trading",
                "applied_strategy_ids": strategy_ids,
            },
            strategy.get("strategy_version"),
        )
    from src.db.repository import list_strategy_schedules, save_strategy_schedule
    from src.strategy_ids import AI_STOCK_SCHEDULE_ID

    existing_schedule_ids = {
        str(item.get("strategy_id") or "")
        for item in list_strategy_schedules(enabled_only=False)
    }
    if AI_STOCK_SCHEDULE_ID not in existing_schedule_ids:

        save_strategy_schedule(
            AI_STOCK_SCHEDULE_ID,
            enabled=False,
            mode="analysis_only",
            auto_approve=False,
        )
    return {
        "ok": True,
        "applied_strategy_ids": strategy_ids,
        "excluded_strategy_ids": independent_ids,
        "schedule_strategy_id": AI_STOCK_SCHEDULE_ID,
        "verification_mode": "demo_account_trading",
    }




def _static_validate_strategy(strategy: dict) -> dict:
    warnings = []
    errors = []
    profile = strategy.get("profile") or {}
    weight = float(profile.get("ai_weight", strategy.get("weight", 0.0)) or 0.0)
    if weight > 0.6:
        warnings.append("AI weight is high; consider <= 0.6 before live use")
    if not str(strategy.get("description") or "").strip():
        warnings.append("Description is empty; rationale will be less auditable")
    risk = profile.get("risk") if isinstance(profile.get("risk"), dict) else {}
    if not risk.get("max_risk_per_trade_pct"):
        warnings.append("Risk profile does not define max_risk_per_trade_pct")
    if profile.get("allow_candidate_promotion") and strategy.get("status") != "approved":
        warnings.append("Candidate promotion should stay disabled until approval")
    if strategy.get("provider") == "openai" and strategy.get("model") == "none":
        errors.append("OpenAI provider requires a model")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "status": "passed" if not errors else "failed",
    }


def _easy_strategy_preset(preset: str) -> dict:
    presets = {
        "safe": {
            "label": "안정형",
            "name": "쉬운 안정형 전략",
            "weight": 0.0,
            "description": "AI 호출 없이 룰 기반 신호만 사용하고 1회 리스크를 낮춘 기본 전략입니다.",
            "risk_pct": 0.5,
            "allow_candidate_promotion": False,
        },
        "balanced": {
            "label": "균형형",
            "name": "쉬운 균형형 전략",
            "weight": 0.2,
            "description": "룰 기반 신호를 중심으로 후보 점수와 리스크 균형을 맞추는 전략입니다.",
            "risk_pct": 1.0,
            "allow_candidate_promotion": False,
        },
        "aggressive": {

            "label": "공격형",
            "name": "쉬운 공격형 전략",
            "weight": 0.35,
            "description": "더 많은 후보 탐색을 허용하되 승인 대기 흐름을 유지하는 전략입니다.",
            "risk_pct": 1.5,
            "allow_candidate_promotion": True,
        },
    }
    if preset not in presets:
        raise HTTPException(status_code=404, detail="Unknown strategy preset")

    item = dict(presets[preset])
    weight = float(item["weight"])
    item["profile"] = {
        "model": "none",
        "ai_weight": weight,
        "risk": {
            "max_risk_per_trade_pct": item["risk_pct"],
            "max_total_open_risk_pct": 2.0,
            "max_sector_exposure_pct": 20.0,
            "max_liquidity_participation_pct": 0.5,
            "max_strategy_exposure_pct": 30.0,
            "max_data_age_seconds": 60,
            "min_cash_reserve_pct": 20.0,
        },
        "market_regime_filter": ["neutral", "bull", "low_volatility"],
        "allow_candidate_promotion": item["allow_candidate_promotion"],
        "preset": preset,
        "strategy_type": {
            "safe": "conservative",
            "balanced": "balanced",
            "aggressive": "aggressive",
        }[preset],
        "risk_level": {
            "safe": "conservative",
            "balanced": "balanced",
            "aggressive": "aggressive",
        }[preset],
    }
    return item


@router.post("/api/ai-strategy-presets/{preset}/apply")
def apply_ai_strategy_preset(preset: str):
    from src.db.repository import (
        create_ai_strategy_record,
        load_ai_strategies,
        set_ai_strategy_selected,
        update_ai_strategy_record,
    )
    import time
    import uuid

    preset_data = _easy_strategy_preset(preset)
    now = _now_kst_text()
    strategy_id = f"easy_{preset}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    strategy_data = {
        "id": strategy_id,
        "name": preset_data["name"],
        "provider": "none",

        "model": "none",
        "weight": preset_data["weight"],
        "description": preset_data["description"],
        "selected": True,
        "status": "approved",
        "profile": preset_data["profile"],
        "strategy_version": 1,
        "last_used_at": now,
    }
    existing = next(
        (
            item for item in load_ai_strategies()
            if item.get("name") == preset_data["name"]
        ),
        None,
    )
    try:
        if existing:
            strategy = update_ai_strategy_record(
                str(existing["id"]),
                {
                    "provider": "none",
                    "model": "none",
                    "weight": preset_data["weight"],
                    "description": preset_data["description"],
                    "profile": preset_data["profile"],
                    "last_used_at": now,
                },
            )
            strategy = set_ai_strategy_selected(str(strategy["id"]), True)
        else:
            strategy = create_ai_strategy_record(strategy_data)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "preset": preset, "message": f"{preset_data['label']} 전략을 적용했습니다.", "strategy": strategy}


@router.post("/api/ai-strategies/{id}/static-verify")
def static_verify_ai_strategy(id: str):
    from src.db.repository import load_ai_strategies, record_ai_strategy_event, save_ai_strategies

    strategies = load_ai_strategies()
    strategy = next((item for item in strategies if item["id"] == id), None)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    result = _static_validate_strategy(strategy)
    result["success"] = bool(result.get("ok"))
    now = _now_kst_text()
    for item in strategies:
        if item["id"] == id:
            item["last_verified_at"] = now
            _store_validation_check(item, "static", result)
            if result["ok"] and item.get("status") == "draft":
                item["status"] = "verified"
            strategy = item
            break
    save_ai_strategies(strategies)
    record_ai_strategy_event(id, "static_verified", result, strategy.get("strategy_version"))
    return {"ok": True, "result": result, "strategy": strategy}



@router.post("/api/ai-strategies/{id}/verify")
def verify_ai_strategy(id: str):
    from src.db.repository import load_ai_strategies, record_ai_strategy_event, save_ai_strategies
    from src.strategy.predict import ModelPredictor
    import time

    strategies = load_ai_strategies()
    strategy = next((item for item in strategies if item["id"] == id), None)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    def persist_result(result: dict) -> dict:
        nonlocal strategy
        now = _now_kst_text()
        for item in strategies:
            if item["id"] == id:
                item["last_verified_at"] = now
                _store_validation_check(item, "api", result)
                if result.get("success") and item.get("status") == "draft":
                    item["status"] = "verified"
                strategy = item
                break
        save_ai_strategies(strategies)
        record_ai_strategy_event(id, "verified", result, strategy.get("strategy_version"))
        return result

    if strategy["provider"] == "none":
        return persist_result({"ok": True, "success": True, "speed_ms": 1, "message": "Rule/local strategy validation passed"})

    predictor = ModelPredictor(
        strategy_profile=strategy.get("profile") or {},
        description=strategy.get("description") or "",
    )
    predictor.enabled = True
    predictor.model_name = strategy["model"]
    # model_name을 전략 모델로 덮어썼으므로 캐시 시그니처를 재계산한다.
    predictor.strategy_signature = predictor._build_strategy_signature()

    test_features = {
        "strategy_score": 3.0,
        "rsi": 28.5,
        "rsi2": 12.0,
        "macd_hist": 0.5,
        "sma20_gap": 0.02,
        "sma60_gap": -0.01,
        "bb_position": -0.05,
        "return_5d": 0.01,
        "return_20d": -0.05,
        "volatility_20d": 0.02,
        "volume_ratio_20d": 1.6,
        "max_drawdown_20d": -0.08,
    }

    started_at = time.time()
    try:
        prediction = predictor.predict(test_features)
        duration_ms = int((time.time() - started_at) * 1000)
        if prediction.get("fallback_reason") and not prediction.get("ml_score"):

            return persist_result({
                "ok": True,
                "success": False,
                "speed_ms": duration_ms,
                "message": f"API validation failed: {prediction.get('fallback_reason')}",
            })
        return persist_result({
            "ok": True,
            "success": True,
            "speed_ms": duration_ms,
            "message": f"API validation passed. final_score={prediction.get('final_score')} ml_score={prediction.get('ml_score')}",
        })
    except Exception as exc:
        return persist_result({
            "ok": True,
            "success": False,
            "speed_ms": int((time.time() - started_at) * 1000),
            "message": f"Prediction error: {type(exc).__name__} - {exc}",
        })


@router.post("/api/ai-strategies/{id}/backtest")
def backtest_ai_strategy(id: str):
    from src.db.repository import load_ai_strategies, record_ai_strategy_event, save_ai_strategies

    strategies = load_ai_strategies()
    strategy = next((item for item in strategies if item["id"] == id), None)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    result = _build_strategy_backtest(strategy)
    now = _now_kst_text()
    for item in strategies:
        if item["id"] == id:
            item["last_backtested_at"] = now
            _store_validation_check(item, "backtest", result)
            if result.get("success"):
                item["status"] = "backtested"
            else:
                item["status"] = "review_required"
            strategy = item
            break
    save_ai_strategies(strategies)
    record_ai_strategy_event(id, "backtested", result, strategy.get("strategy_version"))
    return {"ok": True, "result": result, "strategy": strategy}


@router.post("/api/ai-strategies/{id}/evolve")
def evolve_ai_strategy(id: str):
    from src.strategy.evolve import evolve_strategy
    result = evolve_strategy(id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Strategy evolution failed"))
    return {"ok": True, "result": result}


@router.post("/api/ai-strategies/{id}/paper/start")
def start_ai_strategy_paper(id: str):
    from src.db.repository import load_ai_strategies, record_ai_strategy_event, save_ai_strategies


    strategies = load_ai_strategies()
    strategy = next((item for item in strategies if item["id"] == id), None)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if bool(getattr(trader.config, "ai_require_backtest_pass", True)) and not _check_passed(strategy, "backtest"):
        raise HTTPException(status_code=409, detail="Backtest must pass before paper trading")

    result = {"ok": True, "success": True, "status": "running", "started_at": _now_kst_text()}
    for item in strategies:
        if item["id"] == id:
            item["last_paper_started_at"] = result["started_at"]
            item["status"] = "paper_running"
            _store_validation_check(item, "paper_start", result)
            strategy = item
            break
    save_ai_strategies(strategies)
    record_ai_strategy_event(id, "paper_started", result, strategy.get("strategy_version"))
    return {"ok": True, "result": result, "strategy": strategy}


@router.post("/api/ai-strategies/{id}/paper/complete")
def complete_ai_strategy_paper(id: str, payload: PaperCompletePayload | None = None):
    from src.db.repository import load_ai_strategies, record_ai_strategy_event, save_ai_strategies

    payload = payload or PaperCompletePayload()
    strategies = load_ai_strategies()
    strategy = next((item for item in strategies if item["id"] == id), None)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    result = _paper_result_from_payload(payload, strategy)
    now = _now_kst_text()
    for item in strategies:
        if item["id"] == id:
            item["last_paper_completed_at"] = now
            _store_validation_check(item, "paper", result)
            item["status"] = "paper_passed" if result.get("success") else "review_required"
            strategy = item
            break
    save_ai_strategies(strategies)
    record_ai_strategy_event(id, "paper_completed", result, strategy.get("strategy_version"))
    return {"ok": True, "result": result, "strategy": strategy}


@router.post("/api/ai-strategies/{id}/approve")
def approve_ai_strategy(id: str):
    from src.db.repository import load_ai_strategies, record_ai_strategy_event, save_ai_strategies

    strategies = load_ai_strategies()
    found = None
    for strategy in strategies:
        if strategy["id"] == id:
            gate = _approval_gate(strategy)
            if not gate["ok"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"Strategy approval blocked: missing {', '.join(gate['missing'])}",
                )
            strategy["status"] = "approved"
            found = strategy

            break
    if not found:
        raise HTTPException(status_code=404, detail="Strategy not found")
    save_ai_strategies(strategies)
    record_ai_strategy_event(id, "approved", {"gate": _approval_gate(found)}, found.get("strategy_version"))
    return {"ok": True, "strategy": found}


@router.post("/api/ai-strategies/{id}/retire")
def retire_ai_strategy(id: str):
    from src.db.repository import load_ai_strategies, record_ai_strategy_event, save_ai_strategies

    strategies = load_ai_strategies()
    found = None
    for strategy in strategies:
        if strategy["id"] == id:
            strategy["status"] = "retired"
            strategy["selected"] = False
            found = strategy
            break
    if not found:
        raise HTTPException(status_code=404, detail="Strategy not found")
    save_ai_strategies(strategies)
    record_ai_strategy_event(id, "retired", {}, found.get("strategy_version"))
    return {"ok": True, "strategy": found}


@router.get("/api/ai-strategies/{id}/events")
def get_ai_strategy_events(id: str, limit: int = 100):
    from src.db.repository import get_ai_strategy_events, load_ai_strategies

    if not any(strategy["id"] == id for strategy in load_ai_strategies()):
        raise HTTPException(status_code=404, detail="Strategy not found")
    return {"events": get_ai_strategy_events(id, limit=limit)}


@router.get("/api/ai-strategies/{id}/performance")
def get_ai_strategy_performance(id: str, days: int = 30):
    from src.db.repository import (
        get_ai_strategy_performance as load_performance,
        load_ai_strategies,
        refresh_scanned_candidate_forward_returns,
    )

    if not any(strategy["id"] == id for strategy in load_ai_strategies()):
        raise HTTPException(status_code=404, detail="Strategy not found")
    refresh_scanned_candidate_forward_returns(limit=500)
    return load_performance(id, days=days)


@router.post("/api/ai-strategies/{id}/performance/review")
def review_ai_strategy_performance(id: str, days: int = 30):
    from src.db.repository import load_ai_strategies, review_ai_strategy_performance as review_performance

    if not any(strategy["id"] == id for strategy in load_ai_strategies()):
        raise HTTPException(status_code=404, detail="Strategy not found")
    return review_performance(id, days=days)





@router.get("/api/watchlist")
def get_watchlist(strategy_id: str | None = None):
    from src.db.repository import load_watchlist_data, get_watchlist_extra_info
    from src.strategy.seven_split import STOCK_NAMES, STOCK_SECTORS, KOSPI_UNIVERSE
    from src.strategy.watchlist_policy import eligibility_reason, normalize_watchlist_policy
    from src.market_metadata import resolve_stock_name, resolve_stock_sector
    from collections import Counter

    data = load_watchlist_data()
    policy = normalize_watchlist_policy(data.get("policy"))
    names_by_symbol = data.get("names", {}) if isinstance(data.get("names"), dict) else {}
    inherited = False
    if strategy_id:
        from src.db.repository import load_strategy_universe_symbols

        symbols = load_strategy_universe_symbols(strategy_id)
        # This route is a dashboard view. Even isolated execution strategies should
        # show the shared watchlist when their dedicated universe is empty; execution
        # continues to enforce isolation in trader.build_runtime_plan(). This also
        # keeps older browser sessions with a stale strategy id from rendering blank.
        if not symbols:
            symbols = data.get("symbols", [])
            inherited = True
    else:
        symbols = data.get("symbols", [])
    symbols_detail = []
    sector_counts = Counter()
    eligible_count = 0
    ineligible_count = 0
    unknown_count = 0
    for code in symbols:
        extra = get_watchlist_extra_info(code)
        stored_name = str(names_by_symbol.get(code) or "").strip()
        static_name = STOCK_NAMES.get(code)
        sector = resolve_stock_sector(code, STOCK_SECTORS.get(code)) or "미분류"
        sector_counts[sector] += 1
        price = extra["price"]
        if price is None or float(price or 0) <= 0:
            policy_status = "unknown"
            policy_reason = "현재가 미수집"
            unknown_count += 1
        else:
            rejection = eligibility_reason(
                price=price,
                market_cap=None,
                known_mid_large=code in KOSPI_UNIVERSE,
                policy=policy,
            )
            if rejection:
                policy_status = "ineligible"
                policy_reason = rejection
                ineligible_count += 1
            else:
                policy_status = "eligible"
                policy_reason = "조건 충족"
                eligible_count += 1
        symbols_detail.append({
            "symbol": code,
            "name": resolve_stock_name(code, stored_name or static_name),

            "sector": sector,
            "price": price,
            "score": extra["score"],
            "reason": extra["reason"],
            "change_rate": extra["change_rate"],
            "rsi": extra["rsi"],
            "updated_at": extra["updated_at"],
            "policy_status": policy_status,
            "policy_reason": policy_reason,
        })
    total_count = len(symbols_detail)
    sector_summary = [
        {
            "sector": sector,
            "count": count,
            "ratio": round((count / total_count * 100) if total_count else 0.0, 1),
        }
        for sector, count in sorted(
            sector_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    return {
        "strategy_id": strategy_id,
        "inherited": inherited,
        "universe_source": "shared" if inherited or not strategy_id else "strategy",
        "symbols": symbols_detail,
        "ai_auto_add": data.get("ai_auto_add", False),
        "ai_auto_add_threshold": data.get("ai_auto_add_threshold", 3.0),
        "policy": policy,
        "summary": {
            "total_count": total_count,
            "eligible_count": eligible_count,
            "ineligible_count": ineligible_count,
            "unknown_count": unknown_count,
            "sector_count": len(sector_counts),
            "sectors": sector_summary,
        },
    }



@router.post("/api/watchlist")
def add_to_watchlist(payload: WatchlistAddPayload):
    from src.db.repository import load_watchlist_data, save_watchlist_data
    from src.strategy.seven_split import sync_watchlist_runtime, STOCK_NAMES, KOSPI_UNIVERSE
    from src.strategy.watchlist_policy import eligibility_reason, normalize_watchlist_policy
    from src.market_metadata import resolve_stock_name

    code = payload.symbol.strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail="유효하지 않은 종목코드 형식입니다. (6자리 숫자)")

    settings_data = load_watchlist_data()
    policy = normalize_watchlist_policy(settings_data.get("policy"))
    quote = _get_api().get_quote(code)
    rejection = eligibility_reason(
        price=quote.get("current"),
        market_cap=quote.get("market_cap"),
        known_mid_large=code in KOSPI_UNIVERSE,

        policy=policy,
    )
    if rejection:
        raise HTTPException(status_code=400, detail=rejection)

    if payload.strategy_id:
        from src.db.repository import add_strategy_universe_symbol, load_strategy_universe_symbols

        if code in load_strategy_universe_symbols(payload.strategy_id):
            raise HTTPException(status_code=400, detail="Already registered for this strategy")
        name = resolve_stock_name(code, STOCK_NAMES.get(code, "Unknown"))
        add_strategy_universe_symbol(payload.strategy_id, code, name)
        return {
            "ok": True,
            "strategy_id": payload.strategy_id,
            "symbol": code,
            "name": name,
        }

    data = load_watchlist_data()
    if code in data["symbols"]:
        raise HTTPException(status_code=400, detail="이미 관심목록에 등록되어 있는 종목입니다.")

    data["symbols"].append(code)
    save_watchlist_data(data)
    sync_watchlist_runtime()

    return {
        "ok": True,
        "symbol": code,
        "name": resolve_stock_name(code, STOCK_NAMES.get(code, "알 수 없는 종목"))
    }


@router.post("/api/watchlist/policy")
def update_watchlist_policy(payload: WatchlistPolicyPayload):
    from src.db.repository import save_watchlist_data
    from src.strategy.watchlist_policy import normalize_watchlist_policy

    policy = normalize_watchlist_policy(payload.model_dump())
    save_watchlist_data({"policy": policy})
    return {
        "ok": True,
        "policy": policy,
        "message": "관심종목 정책이 수동 추가와 AI 자동 추가에 적용되었습니다.",
    }



@router.delete("/api/watchlist/{symbol}")
def delete_from_watchlist(symbol: str, strategy_id: str | None = None):
    from src.db.repository import load_watchlist_data, save_watchlist_data
    from src.strategy.seven_split import sync_watchlist_runtime

    code = symbol.strip()
    if strategy_id:
        from src.db.repository import remove_strategy_universe_symbol

        if remove_strategy_universe_symbol(strategy_id, code) <= 0:
            raise HTTPException(status_code=404, detail="Symbol is not registered for this strategy")

        return {"ok": True, "strategy_id": strategy_id}

    data = load_watchlist_data()
    if code not in data["symbols"]:
        raise HTTPException(status_code=404, detail="관심목록에 없는 종목입니다.")

    data["symbols"].remove(code)
    save_watchlist_data(data)
    sync_watchlist_runtime()

    return {"ok": True}



@router.post("/api/watchlist/toggle-auto")
def toggle_watchlist_auto_add(payload: WatchlistTogglePayload):
    from src.db.repository import load_watchlist_data, save_watchlist_data

    data = load_watchlist_data()
    data["ai_auto_add"] = payload.enabled
    if payload.threshold is not None:
        data["ai_auto_add_threshold"] = payload.threshold
    save_watchlist_data(data)

    return {
        "ok": True,
        "ai_auto_add": data["ai_auto_add"],
        "ai_auto_add_threshold": data.get("ai_auto_add_threshold", 3.0)
    }




@router.get("/api/ai-allocation")
def get_ai_allocation():
    missing = _required_env_missing()
    if missing:
        raise HTTPException(status_code=503, detail=f"Missing environment variables: {', '.join(missing)}")

    def _build():
        api = _get_api()
        parsed = _parse_balance(_get_balance_data(api))
        from src.db.repository import load_ai_strategies

        strategies = load_ai_strategies()
        active_strategy = next((strategy for strategy in strategies if strategy.get("selected")), None)
        holdings = []
        for holding in parsed["holdings"]:
            daily = api.get_daily(holding["symbol"], n=120)
            prices = [float(row["stck_clpr"]) for row in daily if row.get("stck_clpr")]
            highs = [float(row["stck_hgpr"]) for row in daily if row.get("stck_hgpr")]
            volumes = [float(row["acml_vol"]) for row in daily if row.get("acml_vol")]
            prices.reverse()
            highs.reverse()
            volumes.reverse()
            holdings.append({
                "symbol": holding["symbol"],
                "name": holding["name"],
                "qty": holding["qty"],
                "price": holding["price"],

                "value": holding["value"],
                "prices": prices,
                "highs": highs,
                "volumes": volumes,
            })
        capital = trader.operating_capital(parsed["total_eval"])
        plan = trader.generate_ai_weight_plan(holdings, capital)
        if active_strategy:
            for position in plan.get("positions", []):
                position["strategy_id"] = active_strategy.get("id")
                position["strategy_version"] = active_strategy.get("strategy_version")
                position["profile_hash"] = active_strategy.get("profile_hash")
                position["ai_strategy_name"] = active_strategy.get("name")
        return plan

    try:
        return snapshot_read_through("ai_allocation", _build)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI allocation failed: {e}") from e




@router.get("/api/finrl/status")
def get_finrl_status():
    return _vendor_status("finrl", VENDOR_PROJECTS["finrl"])




@router.get("/api/vendors")
def get_vendors():
    return {"vendors": [_vendor_status(slug, meta) for slug, meta in VENDOR_PROJECTS.items()]}




@router.get("/api/vendors/{slug}")
def get_vendor(slug: str):
    if slug not in VENDOR_PROJECTS:
        raise HTTPException(status_code=404, detail="vendor not found")
    return _vendor_status(slug, VENDOR_PROJECTS[slug])




@router.get("/api/finrl/pipeline")
def get_finrl_pipeline():
    return {
        "pipeline": [
            {
                "stage": "Data",
                "source": "KIS balance + KIS daily chart",
                "finrl_reference": "meta/data_processor.py",
                "status": "adapted",
            },
            {
                "stage": "Feature Engineering",
                "source": "RSI, RSI2, SMA, Bollinger, MACD, volatility",
                "finrl_reference": "meta/preprocessor/preprocessors.py",

                "status": "adapted",
            },
            {
                "stage": "Environment",
                "source": "current portfolio snapshot",
                "finrl_reference": "meta/env_stock_trading/env_stocktrading.py",
                "status": "dashboard proxy",
            },
            {
                "stage": "Agent Policy",
                "source": "deterministic weight policy inspired by FinRL-X",
                "finrl_reference": "agents/stablebaselines3/models.py",
                "status": "lightweight adapter",
            },
            {
                "stage": "Execution",
                "source": "approval queue + KIS order API",
                "finrl_reference": "trade.py",
                "status": "protected by DRY_RUN and approval",
            },
        ],
    }
