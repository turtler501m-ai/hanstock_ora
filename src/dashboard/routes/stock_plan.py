"""Risk, system and scheduler HTTP handlers."""
import functools
import uuid
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
router = _CompatRouter(tags=["stock", "stock-plan"])
@router.get("/api/risk/status")
def get_risk_status():
    def _build():
        api = _get_api()
        balance_data = _get_balance_data(api, allow_cache=True)
        parsed = _parse_balance(balance_data)

        total_capital = trader.config.total_capital
        pnl = parsed.get("pnl", 0)
        loss_pct = abs(pnl) / total_capital * 100 if total_capital > 0 and pnl < 0 else 0
        max_daily_loss = getattr(trader.config, "max_daily_loss_pct", 3.0)

        return {
            "total_capital": total_capital,
            "current_total": parsed.get("total_eval", 0),
            "stock_eval": parsed.get("stock_eval", 0),
            "cash": parsed.get("cash", 0),
            "cash_ratio": parsed.get("cash_ratio", 0),
            "stock_ratio": parsed.get("stock_ratio", 0),
            "daily_pnl": pnl,
            "daily_loss_pct": round(loss_pct, 2),
            "max_daily_loss_pct": max_daily_loss,
            "loss_halt": loss_pct >= max_daily_loss,
        }

    try:
        result = snapshot_read_through("risk_status", _build)
        # kill_switch는 로컬 상태라 stale 스냅샷에도 항상 현재값을 덮어쓴다.
        result["halted"] = bool(result.get("loss_halt")) or Path(".runtime/kill_switch.json").exists()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@router.post("/api/system/kill")
def activate_kill_switch():
    kill_file = Path(".runtime/kill_switch.json")
    kill_file.parent.mkdir(parents=True, exist_ok=True)
    with open(kill_file, "w") as f:
        json.dump({"active": True, "ts": trader.datetime.now(trader.KST).isoformat()}, f)
    return {"ok": True, "msg": "Kill switch activated"}



@router.post("/api/system/unkill")
def deactivate_kill_switch():
    kill_file = Path(".runtime/kill_switch.json")
    if kill_file.exists():
        kill_file.unlink()
    return {"ok": True, "msg": "Kill switch deactivated"}




@router.get("/api/scheduler/status")
def get_scheduler_status(
    strategy_id: str | None = None,
    compact: bool = True,
    run_id: str | None = None,
    period: str = "monthly",
):
    global _scheduler_run_state
    _dashboard_scheduler_service.refresh()

    config = {

        "cron_tz": os.environ.get("HANSTOCK_CRON_TZ", "Asia/Seoul"),
        "daily_auto_retries": os.environ.get("HANSTOCK_DAILY_AUTO_RETRIES", "3"),
        "daily_auto_retry_delay_seconds": os.environ.get("HANSTOCK_DAILY_AUTO_RETRY_DELAY_SECONDS", "10"),
        "scheduler_retries": os.environ.get("HANSTOCK_SCHEDULER_RETRIES", "1"),
        "scheduler_retry_delay_seconds": os.environ.get("HANSTOCK_SCHEDULER_RETRY_DELAY_SECONDS", "5"),
        "slack_enabled": os.environ.get("HANSTOCK_SCHEDULER_SLACK", "true"),
        "sync_enabled": os.environ.get("HANSTOCK_ORDER_STATUS_SYNC", "true"),
        "result_path": os.environ.get("HANSTOCK_SCHEDULER_RESULT_PATH", ".runtime/daily_auto_last_result.json"),
        "trading_env": trader.config.trading_env,
        "dry_run": trader.config.dry_run,
        "order_submission": trader.runtime_flags().order_submission_enabled,
    }

    period_options = {
        "daily": (1, "일별"),
        "weekly": (7, "주별"),
        "monthly": (30, "월별"),
    }
    if period not in period_options:
        raise HTTPException(
            status_code=400,
            detail="period must be one of: daily, weekly, monthly",
        )
    period_days, period_label = period_options[period]

    last_result = None
    try:
        from src.db.repository import load_recent_scheduler_results, load_latest_scheduler_result
        last_result = load_recent_scheduler_results(days=period_days)
        if last_result is None and period == "monthly":
            last_result = load_latest_scheduler_result()
    except Exception:
        pass

    if last_result is None and period == "monthly":
        path = Path(config["result_path"])
        if path.exists():
            try:
                last_result = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass

    if last_result is None and period != "monthly":
        last_result = {
            "mode": None,
            "recorded_at": None,
            "result": {
                "results": [],
                "auto_approved": [],
                "auto_approval_errors": [],
                "errors": [],
                "execution_runs": [],
                "status": "empty",
                "ok": True,
            },
        }

    if isinstance(last_result, dict):
        last_result["period"] = period
        last_result["period_label"] = period_label
        last_result["range_days"] = period_days
        last_result["summary_label"] = f"{period_label} 집계"

    last_result = _enrich_scheduler_display(last_result)
    if compact:
        last_result = _compact_scheduler_status_result(last_result)

    active_strategy_id = "seven_split"
    strategy_name_by_id = {}
    applied_strategies = []
    active_strategy_name = "기본 룰베이스 (Seven Split)"
    try:
        from src.db.repository import load_ai_strategies

        strategies = load_ai_strategies()
        strategy_name_by_id = {
            str(strategy.get("id") or ""): _strategy_display_name(strategy.get("id"), strategy.get("name"))
            for strategy in strategies
            if strategy.get("id")
        }
        from src.strategy_ids import (
            AI_STOCK_SCHEDULE_ID,
            INDEPENDENT_STOCK_SCHEDULE_IDS,
        )

        applied_strategies = [
            strategy
            for strategy in strategies
            if strategy.get("selected")
            and str(strategy.get("status") or "") == "approved"
            and str(strategy.get("id") or "") not in INDEPENDENT_STOCK_SCHEDULE_IDS
        ]
        applied_names = [
            _strategy_display_name(strategy.get("id"), strategy.get("name"))
            for strategy in applied_strategies
        ]
        if applied_names:
            strategy_name_by_id[AI_STOCK_SCHEDULE_ID] = (
                "AI 적용: " + ", ".join(applied_names)
            )
        active = next(
            (
                strategy
                for strategy in strategies
                if strategy_id
                and (
                    strategy.get("id") == strategy_id
                    or strategy.get("model") == strategy_id
                )
            ),
            None,
        )
        if active is None:
            active = next((strategy for strategy in strategies if strategy.get("selected")), None)

        if active:
            active_strategy_id = active.get("id") or active.get("model") or "seven_split"
            active_strategy_name = active.get("name") or active_strategy_id
    except Exception:
        pass
    active_strategy_name = STRATEGY_DISPLAY_NAMES.get(active_strategy_id, active_strategy_name or active_strategy_id)

    if isinstance(last_result, dict) and isinstance(last_result.get("result"), dict):
        result_data = last_result["result"]
        for collection in ("results", "auto_approved", "auto_approval_errors"):
            for item in result_data.get(collection) or []:
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("strategy_id") or result_data.get("strategy_id") or "seven_split")
                item["strategy_id"] = sid
                item["strategy_name"] = strategy_name_by_id.get(sid) or _strategy_display_name(sid)

    strategy_dispatch = {
        "enabled_count": 0,
        "schedule_count": 0,
        "universe_count": 0,
        "schedules": [],
    }
    try:
        from src.db.repository import list_strategy_schedules, load_strategy_universe

        schedules = [
            schedule
            for schedule in list_strategy_schedules(enabled_only=False)
            if str(schedule.get("strategy_id") or "") == AI_STOCK_SCHEDULE_ID
            or str(schedule.get("strategy_id") or "") in INDEPENDENT_STOCK_SCHEDULE_IDS
        ]
        schedule_items = []
        total_universe_count = 0
        for schedule in schedules:
            sid = schedule.get("strategy_id")
            if str(sid or "") == AI_STOCK_SCHEDULE_ID and applied_strategies:
                for strategy in applied_strategies:
                    applied_id = str(strategy.get("id") or "")
                    universe_count = len(load_strategy_universe(applied_id))

                    total_universe_count += universe_count
                    from src.db.ai_watchlist_repository import get_policy

                    policy = get_policy(applied_id, "KR") or {}
                    policy_auto_approve = bool(policy.get("auto_approve"))
                    policy_auto_execute = bool(policy.get("auto_execute"))
                    schedule_items.append({
                        **schedule,
                        "strategy_id": applied_id,
                        "schedule_strategy_id": AI_STOCK_SCHEDULE_ID,
                        "shared_schedule": True,
                        **_schedule_display_payload(
                            schedule,
                            _strategy_display_name(applied_id, strategy.get("name")),
                        ),
                        "universe_count": universe_count,
                        "policy_automation_level": int(
                            policy.get("automation_level") or 0
                        ),
                        "policy_auto_approve": policy_auto_approve,
                        "policy_auto_execute": policy_auto_execute,
                        "execution_policy_label": (
                            "자동 주문 실행"
                            if policy_auto_execute
                            else "승인 대기열 등록"
                            if policy_auto_approve
                            else "계획만 생성"
                        ),
                    })
                continue
            universe_count = len(load_strategy_universe(sid)) if sid else 0
            total_universe_count += universe_count
            display_name = strategy_name_by_id.get(str(sid or "")) or _strategy_display_name(sid)
            schedule_items.append({
                **schedule,
                **_schedule_display_payload(schedule, display_name),
                "universe_count": universe_count,
            })
        enabled_count = sum(1 for item in schedule_items if item.get("enabled"))
        strategy_dispatch = {

            "enabled_count": enabled_count,
            "schedule_count": len(schedule_items),
            "universe_count": total_universe_count,
            "schedules": schedule_items,
            "summary": f"사용 {enabled_count}개 / 전체 {len(schedule_items)}개 / 감시종목 {total_universe_count}개",
        }
    except Exception:
        pass

    run_state = _compact_scheduler_run_state(_scheduler_run_state) if compact else _scheduler_run_state
    requested_run_matches = not run_id or str(run_state.get("run_id") or "") == run_id

    return {
        "config": config,
        "last_result": last_result,
        "run_state": run_state,
        "requested_run_id": run_id,
        "requested_run_matches": requested_run_matches,
        "result_period": period,
        "result_period_label": period_label,
        "result_range_days": period_days,
        "active_strategy_id": active_strategy_id,
        "active_strategy_name": active_strategy_name,
        "strategy_dispatch": strategy_dispatch,
    }




@router.post("/api/scheduler/run")
def trigger_scheduler_run(payload: dict = Body(...)):
    global _scheduler_run_state
    mode = str(payload.get("mode", "daily_auto")).lower()
    if mode not in {"daily_auto", "execute", "analysis_only"}:
        raise HTTPException(
            status_code=400,
            detail=f"지원하지 않는 국내장 스케줄러 모드입니다: '{mode}'. 'daily_auto', 'execute', 'analysis_only' 중 하나를 선택해 주세요."
        )

    include_ai_rebalance = bool(payload.get("include_ai_rebalance", True))
    auto_approve = bool(payload.get("auto_approve", mode == "daily_auto"))
    raw_categories = payload.get("allowed_categories")
    allowed_categories = None
    if isinstance(raw_categories, list):
        valid_categories = {"position", "candidate", "ai_rebalance"}
        allowed_categories = {

            str(category).strip()
            for category in raw_categories
            if str(category).strip() in valid_categories
        }
        if not allowed_categories:
            raise HTTPException(status_code=400, detail="No valid order categories were provided")

    # 실행 대상 전략: payload.strategy_id가 있으면 사용, 없으면 현재 선택된 전략을 강제.
    raw_strategy_ids = payload.get("strategy_ids")
    strategy_ids = []
    if isinstance(raw_strategy_ids, list):
        strategy_ids = list(dict.fromkeys(
            str(value).strip() for value in raw_strategy_ids if str(value).strip()
        ))
    force_strategy_id = payload.get("strategy_id")
    if force_strategy_id is not None:
        force_strategy_id = str(force_strategy_id).strip() or None
    registered_strategies = []
    if force_strategy_id is None and not strategy_ids:
        try:
            from src.db.repository import load_ai_strategies
            registered_strategies = load_ai_strategies()
            strategy_ids = [
                str(s.get("id")) for s in registered_strategies
                if s.get("selected")
                and str(s.get("status") or "") == "approved"
                and s.get("id")
            ]
        except Exception:
            strategy_ids = []
    if not registered_strategies:
        try:
            from src.db.repository import load_ai_strategies
            registered_strategies = load_ai_strategies()
        except Exception:
            registered_strategies = []

    if force_strategy_id and not strategy_ids:
        strategy_ids = [force_strategy_id]
    if not strategy_ids:

        raise HTTPException(
            status_code=409,
            detail="실행할 승인된 AI 전략 또는 스케줄 전략을 선택해 주세요.",
        )
    from src.strategy_ids import (
        AI_STOCK_SCHEDULE_ID,
        INDEPENDENT_STOCK_SCHEDULE_IDS,
    )
    registered_by_id = {
        str(item.get("id") or ""): item
        for item in registered_strategies
        if item.get("id")
    }
    fixed_ids = {
        "seven_split",
        AI_STOCK_SCHEDULE_ID,
        *INDEPENDENT_STOCK_SCHEDULE_IDS,
    }
    invalid_ids = []
    for strategy_id in strategy_ids:
        if strategy_id in fixed_ids:
            continue
        strategy = registered_by_id.get(strategy_id)
        if not strategy or not strategy.get("selected") or str(strategy.get("status") or "") != "approved":
            invalid_ids.append(strategy_id)
    if invalid_ids:
        raise HTTPException(
            status_code=409,
            detail=f"선택·승인되지 않은 전략은 실행할 수 없습니다: {', '.join(invalid_ids)}",
        )
    from src.strategy_ids import resolve_ai_schedule_strategy_ids
    resolved_strategy_ids = resolve_ai_schedule_strategy_ids(
        strategy_ids,
        strategies=registered_strategies,
    )
    if not resolved_strategy_ids:
        raise HTTPException(
            status_code=409,
            detail="AI 스케줄 슬롯에 적용된 승인 전략이 없습니다.",
        )

    run_id = uuid.uuid4().hex
    max_runtime_seconds = 600 if mode == "analysis_only" else 3600
    if not _dashboard_scheduler_service.claim(
        mode=mode,
        strategy_id=",".join(strategy_ids),
        run_id=run_id,
        max_runtime_seconds=max_runtime_seconds,
    ):
        raise HTTPException(status_code=409, detail="스케줄러가 이미 실행 중입니다.")

    t = threading.Thread(
        target=_bg_run_multiple_scheduled_cycles,
        args=(mode, include_ai_rebalance, auto_approve, strategy_ids, allowed_categories, run_id),
        daemon=True
    )
    t.start()
    return {
        "status": "started",
        "mode": mode,
        "strategy_id": strategy_ids[0] if len(strategy_ids) == 1 else None,
        "strategy_ids": strategy_ids,
        "allowed_categories": sorted(allowed_categories) if allowed_categories else None,
        "run_id": run_id,
        "max_runtime_seconds": max_runtime_seconds,
    }
