# -*- coding: utf-8 -*-
from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import threading
import src.dashboard.core as _core
from src.dashboard.core import *
from src.utils.logger import logger
from src.dashboard.presenters.scheduler_presenter import (
    _compact_scheduler_candidate_scan,
    _compact_scheduler_item,
    _compact_scheduler_status_result,
    _json_safe,
    _tail_items,
    _trim_text,
)
globals().update({k: v for k, v in _core.__dict__.items() if not k.startswith('__')})

router = APIRouter(tags=["stock"])
TRADE_SYNC_RESULT_PATH = Path(".runtime/trade_sync_last_result.json")
APPROVAL_BATCH_RESULT_PATH = Path(".runtime/approval_batch_last_result.json")
_approval_batch_lock = threading.Lock()
_approval_batch_state: dict = {}
_trade_sync_lock = threading.Lock()
_trade_sync_thread: threading.Thread | None = None
_holding_sell_request_lock = threading.Lock()

class NewStrategyPayload(BaseModel):
    name: str = Field(..., min_length=1)
    model: str = "none"
    weight: float = Field(0.0, ge=0.0, le=1.0)
    description: str = ""
    profile: dict | None = None
    status: str | None = None


class UpdateStrategyPayload(BaseModel):
    name: str | None = None
    model: str | None = None
    weight: float | None = Field(None, ge=0.0, le=1.0)
    description: str | None = None
    profile: dict | None = None
    status: str | None = None


class SelectStrategyPayload(BaseModel):
    selected: bool = True


class StrategySelectionPayload(BaseModel):
    strategy_ids: list[str] = Field(default_factory=list)


class PaperCompletePayload(BaseModel):
    days: int = 20
    observations: int = 20
    return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    pass_result: bool | None = None
    notes: str | None = None


class StrategyPerformanceReviewPayload(BaseModel):
    decision: str = "monitor"
    note: str = Field(default="", max_length=1000)


class AccountCashflowPayload(BaseModel):
    external_ref: str = Field(..., min_length=1, max_length=100)
    occurred_at: str = Field(..., min_length=10, max_length=40)
    amount: float
    kind: str
    confirmed: bool = False
    note: str = Field(default="", max_length=1000)


def _now_kst_text() -> str:
    return trader.datetime.now(trader.KST).strftime("%Y-%m-%d %H:%M:%S")


STRATEGY_DISPLAY_NAMES = {
    "seven_split": "기본 분할매매",
    "rule_only_default": "기본 기술룰",
    "gpt_5_mini_default": "GPT-5 미니 기본 전략",
    "ai_stock_default_v1": "AI 기본 종목발굴",
    "narrative_momentum_strategy": "내러티브 모멘텀",
    "plunge_bounce_strategy": "급락 반등",
    "heikin_ashi_scalping_strategy": "알파 하이킨아시",
    "issue_sector_rotation_strategy": "이슈 섹터 순환 모멘텀",
}

STRATEGY_STATUS_LABELS = {
    "draft": "초안",
    "verified": "검증완료",
    "backtested": "백테스트완료",
    "paper_running": "모의운영중",
    "paper_passed": "모의운영통과",
    "approved": "승인완료",
    "review_required": "검토필요",
    "retired": "사용중지",
}

STRATEGY_MODE_LABELS = {
    "daily_auto": "자동매매",
    "execute": "주문실행",
    "analysis_only": "분석전용",
}

APPROVAL_SOURCE_CLASSIFICATIONS = {
    "dashboard": ("수동 주문", "manual"),
    "manual": ("수동 주문", "manual"),
    "dashboard_holding_sell": ("수동 보유종목 매도", "manual"),
    "dashboard_sell_all": ("수동 전량매도", "manual"),
    "signal": ("수동 신호 주문", "manual"),
    "candidate": ("수동 후보 주문", "manual"),
    "execution_plan": ("수동 실행계획 주문", "manual"),
    "portfolio-optimizer": ("포트폴리오 최적화", "tool"),
    "ai-allocation": ("AI 자산배분", "tool"),
    "scheduler-test": ("테스트 주문", "test"),
    "auto_trader": ("자동매매 · 전략 미기록", "automation"),
    "autonomous_strategy": ("자율매매 · 전략 미기록", "automation"),
}


def _strategy_display_name(strategy_id: str | None, fallback: str | None = None) -> str:
    sid = str(strategy_id or "").strip()
    text = str(fallback or "").strip()
    if text:
        return text
    return STRATEGY_DISPLAY_NAMES.get(sid, sid or "-")


def _approval_classification(
    *,
    strategy_id: str | None,
    strategy_name: str | None,
    source: str | None,
) -> dict:
    strategy_id = str(strategy_id or "").strip()
    source = str(source or "").strip()
    if strategy_id:
        return {
            "order_classification": "strategy",
            "order_classification_label": (
                str(strategy_name or "").strip()
                or _strategy_display_name(strategy_id)
            ),
            "order_classification_detail": f"전략 주문 · {strategy_id}",
        }

    normalized_source = source.lower()
    label, kind = APPROVAL_SOURCE_CLASSIFICATIONS.get(
        normalized_source,
        (
            ("수동 주문", "manual")
            if not normalized_source
            or normalized_source.startswith("dashboard")
            or normalized_source.startswith("manual")
            else ("기타 주문", "other")
        ),
    )
    return {
        "order_classification": kind,
        "order_classification_label": label,
        "order_classification_detail": (
            "출처 미기록 · 수동 처리"
            if not source
            else f"출처: {source}"
        ),
    }


def _strategy_status_label(status: str | None) -> str:
    key = str(status or "").lower()
    return STRATEGY_STATUS_LABELS.get(key, status or "-")


def _operation_status_label(operation: dict | None) -> str:
    operation = operation or {}
    if operation.get("ready"):
        mode = operation.get("mode")
        if mode == "live":
            return "실전운영 가능"
        if mode == "dry_run":
            return "모의주문 가능"
        return "데모운영 가능"
    if operation.get("mode") == "inactive":
        return "선택 안됨"
    return "승인/검증 필요"


def _operation_reason_label(operation: dict | None) -> str:
    operation = operation or {}
    reason = str(operation.get("reason") or "")
    if reason == "strategy is not selected":
        return "현재 선택된 전략이 아닙니다."
    if reason.startswith("strategy status is "):
        return f"현재 상태가 {_strategy_status_label(reason.removeprefix('strategy status is '))}입니다."
    if reason.startswith("missing "):
        missing = [
            _approval_missing_label(item.strip())
            for item in reason.removeprefix("missing ").split(",")
            if item.strip()
        ]
        return f"필수 검증 미완료: {', '.join(missing)}"
    if reason == "selected, approved, and validation gate passed":
        return "선택, 승인, 검증 조건을 모두 통과했습니다."
    return reason or "-"


def _approval_missing_label(value: str) -> str:
    labels = {
        "static verification": "정적검증",
        "api verification": "API검증",
        "backtest": "백테스트",
        "paper trading": "모의운영",
        "active strategy": "활성전략",
    }
    return labels.get(value, value)


def _approval_gate_label(gate: dict | None) -> str:
    gate = gate or {}
    if gate.get("ok"):
        return "검증 통과"
    missing = [_approval_missing_label(item) for item in gate.get("missing") or []]
    return f"필수 검증 미완료: {', '.join(missing)}" if missing else "검증 필요"


def _strategy_mode_label(mode: str | None) -> str:
    return STRATEGY_MODE_LABELS.get(str(mode or "").lower(), mode or "-")


def _schedule_display_payload(schedule: dict, display_name: str | None = None) -> dict:
    enabled = bool(schedule.get("enabled"))
    interval = int(schedule.get("interval_minutes") or 0)
    start_hm = str(schedule.get("start_hm") or "").strip()
    end_hm = str(schedule.get("end_hm") or "").strip()
    weekdays = str(schedule.get("weekdays") or "1-5")
    mode = str(schedule.get("mode") or "")
    auto_approve = bool(schedule.get("auto_approve"))

    weekday_label = "월-금" if weekdays == "1-5" else weekdays
    window = f"{start_hm[:2]}:{start_hm[2:]}-{end_hm[:2]}:{end_hm[2:]}" if len(start_hm) == 4 and len(end_hm) == 4 else "-"
    return {
        "display_name": _strategy_display_name(schedule.get("strategy_id"), display_name),
        "enabled_label": "사용 중" if enabled else "중지",
        "interval_label": f"{interval}분마다" if interval else "-",
        "window_label": f"{weekday_label} {window}",
        "mode_label": _strategy_mode_label(mode),
        "auto_approve_label": "자동승인" if auto_approve else "승인대기",
        "last_run_label": schedule.get("last_run_at") or "아직 실행 이력 없음",
        "summary": (
            f"{'사용 중' if enabled else '중지'} · "
            f"{_strategy_mode_label(mode)} · "
            f"{interval}분마다 · {weekday_label} {window} · "
            f"{'자동승인' if auto_approve else '승인대기'}"
        ),
    }




def _enrich_scheduler_display(last_result: dict | None) -> dict | None:
    """Fill display-only stock fields omitted from persisted scheduler summaries."""
    if not isinstance(last_result, dict):
        return last_result
    result = last_result.get("result")
    if not isinstance(result, dict):
        return last_result

    plans = result.get("results") if isinstance(result.get("results"), list) else []
    approved = result.get("auto_approved") if isinstance(result.get("auto_approved"), list) else []
    approval_ids = {
        int(value)
        for item in [*plans, *approved]
        if isinstance(item, dict)
        for value in [item.get("approval_id") or item.get("id")]
        if str(value or "").isdigit()
    }
    symbols = {
        str(item.get("symbol") or "").strip()
        for item in [*plans, *approved]
        if isinstance(item, dict) and str(item.get("symbol") or "").strip()
    }

    approval_by_id: dict[int, dict] = {}
    latest_name_by_symbol: dict[str, str] = {}
    try:
        _init_approval_db()
        with trader.connect_db() as conn:
            conn.row_factory = sqlite3.Row
            if approval_ids:
                placeholders = ",".join("?" for _ in approval_ids)
                rows = conn.execute(
                    f"SELECT * FROM approvals WHERE id IN ({placeholders})",
                    tuple(sorted(approval_ids)),
                ).fetchall()
                approval_by_id = {int(row["id"]): dict(row) for row in rows}
            if symbols:
                placeholders = ",".join("?" for _ in symbols)
                rows = conn.execute(
                    f"SELECT symbol, name FROM approvals WHERE symbol IN ({placeholders}) ORDER BY id DESC",
                    tuple(sorted(symbols)),
                ).fetchall()
                for row in rows:
                    symbol = str(row["symbol"] or "").strip()
                    name = str(row["name"] or "").strip()
                    if symbol and name and name != symbol:
                        latest_name_by_symbol.setdefault(symbol, name)
    except (sqlite3.Error, OSError, TypeError, ValueError):
        pass

    try:
        from src.strategy.seven_split import STOCK_NAMES
        for symbol, name in STOCK_NAMES.items():
            latest_name_by_symbol.setdefault(str(symbol), str(name))
    except (ImportError, AttributeError, TypeError):
        pass

    from src.market_metadata import PLACEHOLDER_STOCK_NAMES, resolve_stock_name

    unknown_names = set(PLACEHOLDER_STOCK_NAMES)

    def enrich_name(item: dict) -> None:
        symbol = str(item.get("symbol") or "").strip()
        current = str(item.get("name") or "").strip()
        if symbol and (current in unknown_names or current == symbol):
            item["name"] = resolve_stock_name(symbol, latest_name_by_symbol.get(symbol, current or symbol))

    for item in plans:
        if isinstance(item, dict):
            enrich_name(item)

    for item in approved:
        if not isinstance(item, dict):
            continue
        value = item.get("approval_id") or item.get("id")
        approval = approval_by_id.get(int(value)) if str(value or "").isdigit() else None
        if approval:
            for key in ("symbol", "name", "action", "qty", "price", "status", "response_msg"):
                if item.get(key) in (None, "", "-") and approval.get(key) not in (None, ""):
                    item[key] = approval[key]
        enrich_name(item)
    return last_result


def _compact_scheduler_run_state(run_state: dict, item_limit: int = 100) -> dict:
    if not isinstance(run_state, dict):
        return run_state
    compact = dict(run_state)
    if isinstance(compact.get("result"), dict):
        wrapped = _compact_scheduler_status_result({"result": compact["result"]}, item_limit=item_limit)
        if isinstance(wrapped, dict):
            compact["result"] = wrapped.get("result")
            compact["result_compact"] = True
    return compact


def _validation_payload(strategy: dict) -> dict:
    import json

    raw = strategy.get("last_validation_result")
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        data = {}
    data = _json_safe(data)
    if "checks" not in data or not isinstance(data.get("checks"), dict):
        data = {"checks": {}, "latest": data if data else None}
    return data


def _strategy_api_payload(strategy: dict) -> dict:
    from src.config import config
    from src.strategy_ids import INDEPENDENT_STOCK_SCHEDULE_IDS

    payload = _json_safe(dict(strategy))
    payload["approval_gate"] = _approval_gate(strategy)
    payload["operation_status"] = _operation_status(strategy)
    payload["display_name"] = _strategy_display_name(strategy.get("id"), strategy.get("name"))
    payload["status_label"] = _strategy_status_label(strategy.get("status"))
    payload["selected_label"] = "현재 사용" if strategy.get("selected") else "대기"
    payload["schedule_category"] = _strategy_schedule_category(strategy)
    payload["schedule_category_label"] = {
        "safe": "안정형",
        "balanced": "균형형",
        "aggressive": "공격형",
    }[payload["schedule_category"]]
    payload["approval_gate"]["label"] = _approval_gate_label(payload["approval_gate"])
    payload["operation_status"]["label"] = _operation_status_label(payload["operation_status"])
    payload["operation_status"]["reason_label"] = _operation_reason_label(payload["operation_status"])
    payload["independent_schedule"] = str(strategy.get("id") or "") in INDEPENDENT_STOCK_SCHEDULE_IDS
    payload["autonomy"] = {
        "enabled": bool(getattr(config, "autonomy_enabled", False)),
        "environment": str(getattr(config, "autonomy_trading_env", "demo")),
        "require_approval": bool(
            getattr(config, "autonomy_require_approval", True)
        ),
        "applicable": str(strategy.get("status") or "") not in {
            "draft",
            "review_required",
            "suspended",
            "retired",
        },
    }
    return payload


def _strategy_schedule_category(strategy: dict) -> str:
    """Normalize varied strategy profiles into the three schedule UI groups."""
    profile = strategy.get("profile") or {}
    preset = str(profile.get("preset") or "").strip().lower()
    if preset in {"safe", "balanced", "aggressive"}:
        return preset

    strategy_type = str(profile.get("strategy_type") or "").strip().lower()
    risk_level = str(profile.get("risk_level") or "").strip().lower()
    if strategy_type in {"safe", "conservative"} or risk_level in {
        "safe",
        "conservative",
        "low",
    }:
        return "safe"
    if strategy_type in {"aggressive", "momentum"} or risk_level in {
        "aggressive",
        "high",
    }:
        return "aggressive"
    if strategy_type == "balanced" or risk_level in {"balanced", "medium"}:
        return "balanced"

    risk = profile.get("risk") if isinstance(profile.get("risk"), dict) else {}
    risk_pct = float(risk.get("max_risk_per_trade_pct") or 1.0)
    if risk_pct <= 0.75:
        return "safe"
    if risk_pct >= 1.25:
        return "aggressive"
    return "balanced"


def _store_validation_check(strategy: dict, check_name: str, result: dict) -> None:
    import json

    data = _validation_payload(strategy)
    safe_result = _json_safe(result)
    data["checks"][check_name] = safe_result
    data["latest"] = {"check": check_name, "result": safe_result}
    strategy["last_validation_result"] = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def _check_passed(strategy: dict, check_name: str) -> bool:
    result = _validation_payload(strategy).get("checks", {}).get(check_name, {})
    return bool(result.get("success") or result.get("ok") is True and result.get("status") == "passed")


def _approval_gate(strategy: dict) -> dict:
    return {"ok": True, "missing": []}


def _operation_status(strategy: dict) -> dict:
    gate = _approval_gate(strategy)
    status = str(strategy.get("status") or "")
    selected = bool(strategy.get("selected"))
    approved = status == "approved"
    ready = bool(selected and approved)
    if ready:
        if bool(trader.DRY_RUN):
            mode = "dry_run"
        elif bool(trader.ENABLE_LIVE_TRADING) and str(trader.TRADING_ENV).lower() == "real":
            mode = "live"
        else:
            mode = "demo"
        reason = "selected strategy; performance is verified through demo-account trading"
    elif not selected:
        mode = "inactive"
        reason = "strategy is not selected"
    elif not approved:
        mode = "blocked"
        reason = f"strategy status is {status or 'unknown'}"
    else:
        mode = "blocked"
        reason = f"missing {', '.join(gate.get('missing') or [])}"
    return {
        "ready": ready,
        "mode": mode,
        "selected": selected,
        "approved": approved,
        "dry_run": bool(trader.DRY_RUN),
        "live_enabled": bool(trader.ENABLE_LIVE_TRADING),
        "reason": reason,
    }


def _build_strategy_backtest(strategy: dict) -> dict:
    from src.strategy.backtest import run_historical_backtest
    profile = strategy.get("profile") or {}
    return run_historical_backtest(profile)


def _paper_result_from_payload(payload: PaperCompletePayload, strategy: dict) -> dict:
    profile = strategy.get("profile") or {}
    risk = profile.get("risk") if isinstance(profile.get("risk"), dict) else {}
    required_days = int(risk.get("paper_trading_required_days") or 20)
    passed = payload.pass_result
    if passed is None:
        passed = (
            payload.days >= required_days
            and payload.observations >= max(5, required_days // 2)
            and payload.max_drawdown_pct <= 10.0
        )
    return {
        "ok": True,
        "success": bool(passed),
        "status": "passed" if passed else "failed",
        "days": int(payload.days),
        "required_days": required_days,
        "observations": int(payload.observations),
        "return_pct": float(payload.return_pct),
        "max_drawdown_pct": float(payload.max_drawdown_pct),
        "notes": payload.notes or "",
        "message": "Paper trading gate completed",
    }


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
    from src.db import ai_stock_repository

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
        else get_latest_usable_analysis_cycle(active_strategy_id, trader.TRADING_ENV)
    )
    profile = active.get("profile") if active else {}
    active_gate = _approval_gate(active) if active else {"ok": False, "missing": ["active strategy"]}
    active_operation = _operation_status(active) if active else {
        "ready": False,
        "mode": "blocked",
        "selected": False,
        "approved": False,
        "dry_run": bool(trader.DRY_RUN),
        "live_enabled": bool(trader.ENABLE_LIVE_TRADING),
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
            "trading_env": trader.TRADING_ENV,
            "dry_run": bool(trader.DRY_RUN),
            "enable_live_trading": bool(trader.ENABLE_LIVE_TRADING),
            "require_approval": bool(trader.REQUIRE_APPROVAL),
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
            trader.TRADING_ENV,
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
        if not (bool(trader.DRY_RUN) or str(trader.TRADING_ENV).lower() != "real"):
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







@router.get("/api/approvals")
def get_approvals(limit: int = 50, strategy_id: str | None = None):
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be greater than 0")
    limit = min(limit, 200)
    auto_approval_enabled = _auto_approval_enabled()
    _reclaim_stale_executing_approvals()

    _init_approval_db()
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        if strategy_id:
            recent_rows = conn.execute(
                "SELECT * FROM approvals WHERE strategy_id = ? ORDER BY id DESC LIMIT ?",
                (strategy_id, limit),
            ).fetchall()
            actionable_rows = conn.execute(
                """
                SELECT * FROM approvals
                WHERE strategy_id = ? AND status IN ('pending', 'executing', 'failed')
                ORDER BY id DESC
                """,
                (strategy_id,),
            ).fetchall()
        else:
            recent_rows = conn.execute(
                "SELECT * FROM approvals ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            actionable_rows = conn.execute(
                """
                SELECT * FROM approvals
                WHERE status IN ('pending', 'executing', 'failed')
                ORDER BY id DESC
                """
            ).fetchall()
        rows_by_id = {int(row["id"]): row for row in recent_rows}
        rows_by_id.update({int(row["id"]): row for row in actionable_rows})
        rows = sorted(rows_by_id.values(), key=lambda row: int(row["id"]), reverse=True)
    try:
        from src.db.repository import load_ai_strategies

        strategy_names = {
            str(strategy.get("id") or ""): _strategy_display_name(
                strategy.get("id"),
                strategy.get("name"),
            )
            for strategy in load_ai_strategies()
            if strategy.get("id")
        }
    except Exception:
        strategy_names = {}

    approvals = []
    latest_trades = _latest_trades_by_approval_ids([int(row["id"]) for row in rows])
    open_sells = _latest_open_sell_trades_by_symbols([str(row["symbol"]) for row in rows])
    for row in rows:
        item = _approval_row(row)
        strategy_id_value = str(item.get("strategy_id") or "").strip()
        strategy_name_value = (
            strategy_names.get(strategy_id_value)
            if strategy_id_value
            else None
        )
        if strategy_name_value:
            item["strategy_name"] = strategy_name_value
        item.update(_approval_classification(
            strategy_id=strategy_id_value,
            strategy_name=strategy_name_value,
            source=item.get("source"),
        ))
        trade = latest_trades.get(int(item.get("id") or 0))
        if trade:
            item["trade_id"] = trade.get("id")
            item["broker_order_id"] = trade.get("broker_order_id")
            item["order_status"] = trade.get("order_status")
            item["filled_qty"] = _to_int(trade.get("filled_qty"))
            item["pre_order_qty"] = _to_int(trade.get("pre_order_qty"))
            item["retry_eligible"] = _approval_retry_eligible(item, trade)
        else:
            item["retry_eligible"] = _approval_retry_eligible(item, None)
        blocking_sell = open_sells.get(str(item.get("symbol") or "").strip())
        if blocking_sell:
            item["blocking_order_id"] = blocking_sell.get("broker_order_id")
            item["blocking_remaining_qty"] = max(
                0,
                _to_int(blocking_sell.get("qty")) - _to_int(blocking_sell.get("filled_qty")),
            )
        item["auto_approval_in_progress"] = (
            auto_approval_enabled
            and item.get("status") == "pending"
            and item.get("source") in {"dashboard_sell_all", "dashboard_holding_sell"}
        )
        approvals.append(item)
    return {
        "approvals": approvals,
        "actionable_count": len(actionable_rows),
        "recent_limit": limit,
    }


def _latest_open_sell_trades_by_symbols(symbols: list[str]) -> dict[str, dict]:
    normalized = sorted({str(symbol or "").strip() for symbol in symbols if str(symbol or "").strip()})
    if not normalized:
        return {}
    trader.init_db()
    placeholders = ", ".join("?" for _ in normalized)
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT *
            FROM trades
            WHERE symbol IN ({placeholders})
              AND action = 'sell'
              AND order_status IN ('open', 'partial')
              AND COALESCE(broker_order_id, '') != ''
              AND qty > COALESCE(filled_qty, 0)
            ORDER BY id DESC
            """,
            normalized,
        ).fetchall()
    latest: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        symbol = str(item.get("symbol") or "").strip()
        if symbol and symbol not in latest:
            latest[symbol] = item
    return latest


def _latest_trades_by_approval_ids(approval_ids: list[int]) -> dict[int, dict]:
    ids = [int(approval_id) for approval_id in approval_ids if int(approval_id or 0) > 0]
    if not ids:
        return {}
    trader.init_db()
    placeholders = ", ".join("?" for _ in ids)
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT *
            FROM trades
            WHERE source_approval_id IN ({placeholders})
            ORDER BY id DESC
            """,
            ids,
        ).fetchall()
    latest: dict[int, dict] = {}
    for row in rows:
        item = dict(row)
        approval_id = _to_int(item.get("source_approval_id"))
        if approval_id > 0 and approval_id not in latest:
            latest[approval_id] = item
    return latest


def _latest_trade_by_approval_id(approval_id: int) -> dict | None:
    return _latest_trades_by_approval_ids([approval_id]).get(int(approval_id))


def _approval_retry_eligible(item: dict, trade: dict | None) -> bool:
    if str(item.get("action") or "").lower() != "sell":
        return False
    if str(item.get("status") or "") == "pending":
        return False
    trade_status = str((trade or {}).get("order_status") or "").lower()
    approval_status = str(item.get("status") or "").lower()
    if trade_status in {"failed", "partial"}:
        return True
    return approval_status == "failed"


def _current_sellable_qty(symbol: str) -> int:
    try:
        parsed = _parse_balance(_get_balance_data(_get_api(), allow_cache=False))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"KIS balance API request failed: {exc}") from exc
    for holding in parsed.get("holdings", []):
        if str(holding.get("symbol") or "").strip() == str(symbol).strip():
            holding_qty = _to_int(holding.get("qty"))
            sellable_qty = _to_int(holding.get("sellable_qty", holding_qty))
            return max(0, min(holding_qty, sellable_qty))
    return 0


def _open_sell_order_from_history(api, symbol: str) -> dict | None:
    start_date, end_date = _order_history_window(MIN_ORDER_HISTORY_SYNC_DAYS)
    try:
        history = api.get_trade_history(start_date, end_date)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"KIS order history request failed: {exc}") from exc
    candidates = []
    for row in history:
        if _history_symbol(row) != str(symbol).strip():
            continue
        if _history_action(row) != "sell":
            continue
        remaining_qty = _history_remaining_qty(row)
        if remaining_qty <= 0:
            continue
        canceled = _history_text(row, "cncl_yn", "CNCL_YN", "canceled", "cancel_yn").upper()
        if canceled in {"Y", "취소"}:
            continue
        candidates.append(row)
    if not candidates:
        return None
    candidates.sort(key=lambda row: (_history_timestamp(row), _broker_order_id_from_history(row)), reverse=True)
    return candidates[0]


def _create_retry_approval_from_item(item: dict, *, retry_qty: int, source_approval_id: int) -> int:
    symbol = str(item.get("symbol") or "").strip()
    return _create_approval_row({
        "symbol": symbol,
        "name": item.get("name") or symbol,
        "action": "sell",
        "qty": retry_qty,
        "price": 0,
        "reason": f"retry approval #{source_approval_id}: {item.get('reason') or ''}".strip(),
        "source": "dashboard_retry",
        "strategy_id": item.get("strategy_id"),
        "strategy_version": item.get("strategy_version"),
        "profile_hash": item.get("profile_hash"),
        "source_candidate_id": item.get("source_candidate_id"),
    })


def _save_approval_batch_state(state: dict) -> None:
    APPROVAL_BATCH_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = APPROVAL_BATCH_RESULT_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(APPROVAL_BATCH_RESULT_PATH)


def _run_approval_batch(job_id: str, action: str, approval_ids: list[int]) -> None:
    results = []
    success_count = 0
    failed_count = 0
    for index, approval_id in enumerate(approval_ids, start=1):
        try:
            if action == "cancel-retry":
                result = cancel_blocking_sell_and_retry_approval(approval_id)
            else:
                result = retry_approval_order(approval_id)
            success_count += 1
            results.append({"approval_id": approval_id, "ok": True, "result": result})
        except Exception as exc:
            failed_count += 1
            detail = getattr(exc, "detail", None) or str(exc)
            results.append({"approval_id": approval_id, "ok": False, "error": str(detail)})

        with _approval_batch_lock:
            if _approval_batch_state.get("job_id") != job_id:
                return
            _approval_batch_state.update({
                "processed_count": index,
                "success_count": success_count,
                "failed_count": failed_count,
                "results": results,
            })
            _save_approval_batch_state(_approval_batch_state)

    with _approval_batch_lock:
        if _approval_batch_state.get("job_id") != job_id:
            return
        _approval_batch_state.update({
            "status": "completed",
            "completed_at": trader.datetime.now(trader.KST).isoformat(),
        })
        _save_approval_batch_state(_approval_batch_state)


@router.post("/api/approvals/batch")
def start_approval_batch(payload: dict = Body(...)):
    action = str(payload.get("action") or "").strip()
    if action not in {"retry", "cancel-retry"}:
        raise HTTPException(status_code=400, detail="action must be retry or cancel-retry")
    approval_ids = []
    for value in payload.get("approval_ids") or []:
        approval_id = _to_int(value)
        if approval_id > 0 and approval_id not in approval_ids:
            approval_ids.append(approval_id)
    if not approval_ids:
        raise HTTPException(status_code=400, detail="approval_ids is required")
    if len(approval_ids) > 50:
        raise HTTPException(status_code=400, detail="approval_ids must contain at most 50 items")

    with _approval_batch_lock:
        if _approval_batch_state.get("status") == "running":
            raise HTTPException(status_code=409, detail="another approval batch is already running")
        job_id = trader.datetime.now(trader.KST).strftime("%Y%m%d%H%M%S%f")
        _approval_batch_state.clear()
        _approval_batch_state.update({
            "job_id": job_id,
            "action": action,
            "status": "running",
            "total_count": len(approval_ids),
            "processed_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "results": [],
            "started_at": trader.datetime.now(trader.KST).isoformat(),
            "completed_at": None,
        })
        _save_approval_batch_state(_approval_batch_state)

    threading.Thread(
        target=_run_approval_batch,
        args=(job_id, action, approval_ids),
        name=f"approval-batch-{job_id}",
        daemon=True,
    ).start()
    return dict(_approval_batch_state)


@router.get("/api/approvals/batch/status")
def get_approval_batch_status():
    with _approval_batch_lock:
        if _approval_batch_state:
            return {"available": True, **dict(_approval_batch_state)}
    if APPROVAL_BATCH_RESULT_PATH.exists():
        try:
            saved = json.loads(APPROVAL_BATCH_RESULT_PATH.read_text(encoding="utf-8"))
            return {"available": True, **saved}
        except (OSError, ValueError, TypeError):
            pass
    return {"available": False, "status": "idle"}


@router.post("/api/approvals/{approval_id}/retry")
def retry_approval_order(approval_id: int):
    item = _approval_by_id(approval_id)
    if not item:
        raise HTTPException(status_code=404, detail="approval not found")
    trade = _latest_trade_by_approval_id(approval_id)
    if not _approval_retry_eligible(item, trade):
        raise HTTPException(status_code=409, detail="approval is not retryable")

    symbol = str(item.get("symbol") or "").strip()
    sellable_qty = _current_sellable_qty(symbol)
    if sellable_qty <= 0:
        raise HTTPException(
            status_code=409,
            detail="sellable quantity is zero; cancel or wait for the existing sell order to settle before retry",
        )

    original_qty = _to_int(item.get("qty"))
    filled_qty = _to_int((trade or {}).get("filled_qty"))
    remaining_qty = max(0, original_qty - filled_qty)
    retry_qty = min(sellable_qty, remaining_qty if remaining_qty > 0 else sellable_qty)
    if retry_qty <= 0:
        raise HTTPException(status_code=409, detail="no remaining quantity to retry")

    retry_id = _create_retry_approval_from_item(item, retry_qty=retry_qty, source_approval_id=approval_id)
    return {
        "id": retry_id,
        "status": "pending",
        "source_approval_id": approval_id,
        "symbol": symbol,
        "qty": retry_qty,
        "sellable_qty": sellable_qty,
    }


@router.post("/api/approvals/{approval_id}/cancel-retry")
def cancel_blocking_sell_and_retry_approval(approval_id: int):
    item = _approval_by_id(approval_id)
    if not item:
        raise HTTPException(status_code=404, detail="approval not found")
    trade = _latest_trade_by_approval_id(approval_id)
    if not _approval_retry_eligible(item, trade):
        raise HTTPException(status_code=409, detail="approval is not retryable")

    symbol = str(item.get("symbol") or "").strip()
    api = _get_api()
    blocking_order = _open_sell_order_from_history(api, symbol)
    if not blocking_order:
        return retry_approval_order(approval_id)

    order_no = _broker_order_id_from_history(blocking_order)
    remaining_qty = _history_remaining_qty(blocking_order)
    branch = _history_text(blocking_order, "ord_gno_brno", "ORD_GNO_BRNO")
    if not order_no or remaining_qty <= 0:
        raise HTTPException(status_code=409, detail="blocking sell order was not identifiable")

    try:
        cancel_result = api.cancel_order(
            order_no,
            qty=remaining_qty,
            original_order_branch=branch,
            cancel_all=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"KIS order cancellation failed: {exc}") from exc
    if str(cancel_result.get("rt_cd") or "") != "0":
        raise HTTPException(
            status_code=409,
            detail=f"KIS order cancellation rejected: {cancel_result.get('msg1') or cancel_result}",
        )

    trader.update_trade_order_status(
        order_no,
        order_status="canceled",
        filled_qty=_history_fill_qty(blocking_order),
        filled_price=_history_fill_price(blocking_order),
        response_msg="KIS blocking sell order canceled before retry",
        broker_result=cancel_result,
    )
    _clear_balance_cache()

    # The canceled broker order owns the currently locked quantity. Recreate its
    # entire remainder instead of capping it to an older failed approval amount.
    retry_qty = remaining_qty
    retry_id = _create_retry_approval_from_item(item, retry_qty=retry_qty, source_approval_id=approval_id)
    return {
        "id": retry_id,
        "status": "pending",
        "source_approval_id": approval_id,
        "symbol": symbol,
        "qty": retry_qty,
        "canceled_order_id": order_no,
        "cancel_result": cancel_result,
    }




@router.post("/api/approvals")
def create_approval(payload: dict = Body(...)):
    source = str(payload.get("source") or "")
    if source == "dashboard_holding_sell":
        symbol = str(payload.get("symbol") or "").strip()
        with _holding_sell_request_lock:
            if symbol and symbol in _active_dashboard_sell_symbols():
                raise HTTPException(
                    status_code=409,
                    detail=f"active sell request already exists for {symbol}",
                )
            approval_id = _create_approval_row(payload)
    else:
        approval_id = _create_approval_row(payload)
    if _auto_approval_enabled():
        if source == "dashboard_holding_sell":
            _run_auto_approval_batch_async([approval_id])
            return {
                "id": approval_id,
                "status": "pending",
                "auto_approved": False,
                "auto_approval_queued": True,
            }
        result = _approve_pending_approval(approval_id, "auto approval")
        result["auto_approved"] = True
        return result
    return {"id": approval_id, "status": "pending"}


def _create_approval_row(payload: dict) -> int:
    action = str(payload.get("action", "")).lower()
    if action not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="action must be buy or sell")

    symbol = str(payload.get("symbol", "")).strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    qty = _to_int(payload.get("qty"))
    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be greater than 0")

    price = _to_int(payload.get("price"))
    name = str(payload.get("name") or symbol)
    reason = str(payload.get("reason") or "")
    source = str(payload.get("source") or "dashboard")
    strategy_id = str(payload.get("strategy_id") or "").strip() or None
    strategy_version = _to_int(payload.get("strategy_version")) or None
    profile_hash = str(payload.get("profile_hash") or "").strip() or None
    source_candidate_id = _to_int(payload.get("source_candidate_id")) or None
    now = trader.datetime.now(trader.KST).strftime("%Y-%m-%d %H:%M:%S")

    _init_approval_db()
    with trader.connect_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO approvals
            (
                created_at, updated_at, symbol, name, action, qty, price, reason, source,
                status, response_msg, strategy_id, strategy_version, profile_hash, source_candidate_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?, ?, ?)
            """,
            (
                now, now, symbol, name, action, qty, price, reason, source,
                strategy_id, strategy_version, profile_hash, source_candidate_id,
            ),
        )
        approval_id = cursor.lastrowid
    return int(approval_id)


def _run_auto_approval_batch_async(approval_ids: list[int]) -> None:
    def worker() -> None:
        for approval_id in approval_ids:
            try:
                _approve_pending_approval(approval_id, "auto approval")
            except Exception as exc:
                logger.warning(f"sell-all auto approval failed approval_id={approval_id}: {exc}")

    import threading

    thread = threading.Thread(target=worker, name="sell-all-auto-approval", daemon=False)
    thread.start()


def _active_dashboard_sell_symbols() -> set[str]:
    trader.init_db()
    _init_approval_db()
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT a.symbol
            FROM approvals a
            WHERE a.action = 'sell'
              AND a.source IN ('dashboard_holding_sell', 'dashboard_sell_all')
              AND COALESCE(a.symbol, '') <> ''
              AND (
                    a.status IN ('pending', 'executing')
                    OR (
                        a.status = 'executed'
                        AND EXISTS (
                            SELECT 1
                            FROM trades t
                            WHERE t.source_approval_id = a.id
                              AND t.id = (
                                  SELECT MAX(t2.id)
                                  FROM trades t2
                                  WHERE t2.source_approval_id = a.id
                              )
                              AND t.action = 'sell'
                              AND t.order_status IN ('submitted', 'open', 'partial')
                              AND t.qty > COALESCE(t.filled_qty, 0)
                        )
                    )
              )
            """
        ).fetchall()
    return {str(row["symbol"]) for row in rows}


def _unsubmitted_dashboard_sell_symbols() -> set[str]:
    _init_approval_db()
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT symbol
            FROM approvals
            WHERE action = 'sell'
              AND source IN ('dashboard_holding_sell', 'dashboard_sell_all')
              AND status IN ('pending', 'executing')
              AND COALESCE(symbol, '') <> ''
            """
        ).fetchall()
    return {str(row["symbol"]) for row in rows}


def _cancel_open_buy_orders_before_liquidation(api) -> list[dict]:
    trader.init_db()
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM trades
            WHERE action = 'buy'
              AND order_status IN ('submitted', 'open', 'partial')
              AND COALESCE(broker_order_id, '') <> ''
              AND qty > COALESCE(filled_qty, 0)
            ORDER BY id ASC
            """
        ).fetchall()

    results = []
    for row in rows:
        item = dict(row)
        order_no = str(item.get("broker_order_id") or "")
        remaining_qty = max(
            0,
            _to_int(item.get("qty")) - _to_int(item.get("filled_qty")),
        )
        if remaining_qty <= 0:
            continue
        try:
            result = api.cancel_order(
                order_no,
                qty=remaining_qty,
                cancel_all=True,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"open buy cancellation failed for {order_no}: {exc}",
            ) from exc
        ok = str(result.get("rt_cd") or "") == "0"
        message = str(result.get("msg1") or "")
        no_cancelable_qty = "취소할 수량이 없습니다" in message
        if not ok and not no_cancelable_qty:
            raise HTTPException(
                status_code=409,
                detail=f"open buy cancellation rejected for {order_no}: {message or result}",
            )
        if ok:
            trader.update_trade_order_status(
                order_no,
                trade_id=_to_int(item.get("id")) or None,
                order_status="canceled",
                filled_qty=_to_int(item.get("filled_qty")),
                filled_price=_to_int(item.get("filled_price")),
                response_msg="Canceled open buy before dashboard liquidation",
                broker_result=result,
            )
        results.append({
            "broker_order_id": order_no,
            "remaining_qty": remaining_qty,
            "status": "canceled" if ok else "already_terminal",
            "message": message,
        })
    return results




@router.post("/api/holdings/sell-all")
def sell_all_holdings(payload: dict | None = Body(default=None)):
    missing = _required_env_missing()
    if missing:
        raise HTTPException(status_code=503, detail=f"Missing environment variables: {', '.join(missing)}")

    halt_new_buys = bool((payload or {}).get("halt_new_buys"))
    if halt_new_buys:
        activate_kill_switch()

    try:
        api = _get_api()
        canceled_buy_orders = (
            _cancel_open_buy_orders_before_liquidation(api)
            if halt_new_buys
            else []
        )
        parsed = _parse_balance(_get_balance_data(api, allow_cache=False))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"KIS balance API request failed: {e}") from e

    orders = []
    skipped = []
    with _holding_sell_request_lock:
        # Submitted broker orders already reserve their quantity at KIS and
        # therefore reduce sellable_qty. Only approvals not yet submitted need
        # a symbol-level duplicate guard here; otherwise newly filled buys could
        # never be liquidated while an older sell remains open.
        active_symbols = _unsubmitted_dashboard_sell_symbols()
        for holding in parsed.get("holdings", []):
            symbol = str(holding.get("symbol", "")).strip()
            holding_qty = _to_int(holding.get("qty"))
            sellable_qty = _to_int(holding.get("sellable_qty", holding_qty))
            qty = min(holding_qty, sellable_qty) if holding_qty > 0 else 0
            if not symbol:
                continue
            if symbol in active_symbols:
                skipped.append({
                    "symbol": symbol,
                    "name": str(holding.get("name") or symbol),
                    "qty": holding_qty,
                    "sellable_qty": sellable_qty,
                    "reason": "active sell request already exists",
                })
                continue
            if qty <= 0:
                skipped.append({
                    "symbol": symbol,
                    "name": str(holding.get("name") or symbol),
                    "qty": holding_qty,
                    "sellable_qty": sellable_qty,
                    "reason": "sellable quantity is zero",
                })
                continue
            orders.append({
                "symbol": symbol,
                "name": str(holding.get("name") or symbol),
                "action": "sell",
                "qty": qty,
                "price": 0,
                "reason": "dashboard sell all holdings",
                "source": "dashboard_sell_all",
            })

        approval_ids = [_create_approval_row(order) for order in orders]

    if not approval_ids:
        return {
            "status": "empty",
            "created_count": 0,
            "new_buys_halted": halt_new_buys,
            "canceled_buy_orders": canceled_buy_orders,
            "skipped_count": len(skipped),
            "skipped": skipped,
            "orders": [],
        }

    created = [{"id": approval_id, "status": "pending"} for approval_id in approval_ids]
    auto_approval_queued = False
    if _auto_approval_enabled():
        _run_auto_approval_batch_async(approval_ids)
        auto_approval_queued = True
    _clear_balance_cache()

    return {
        "status": "created",
        "created_count": len(created),
        "pending_count": sum(1 for item in created if isinstance(item, dict) and item.get("status") == "pending"),
        "submitted_count": sum(1 for item in created if isinstance(item, dict) and item.get("status") == "executed"),
        "executed_count": sum(1 for item in created if isinstance(item, dict) and item.get("status") == "executed"),
        "failed_count": sum(1 for item in created if isinstance(item, dict) and item.get("status") == "failed"),
        "auto_approved": False,
        "auto_approval_queued": auto_approval_queued,
        "new_buys_halted": halt_new_buys,
        "canceled_buy_orders": canceled_buy_orders,
        "fill_status_note": "KIS 주문 접수 결과입니다. 실제 체결 여부는 주문내역 동기화 후 확정됩니다.",
        "skipped_count": len(skipped),
        "skipped": skipped,
        "orders": created,
    }




@router.post("/api/approvals/{approval_id}/approve")
def approve_order(approval_id: int):
    return _approve_pending_approval(approval_id, "수동승인")




@router.post("/api/approvals/{approval_id}/reject")
def reject_order(approval_id: int):
    item = _load_pending_approval(approval_id)
    if item.get("managed_order_id"):
        from src.strategy.autonomy.ai_stock_integration import (
            reject_managed_ai_stock_order,
        )

        try:
            return reject_managed_ai_stock_order(approval_id)
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail=f"managed AI-stock rejection failed closed: {exc}",
            ) from exc
    now = trader.datetime.now(trader.KST).strftime("%Y-%m-%d %H:%M:%S")
    with trader.connect_db() as conn:
        conn.execute(
            "UPDATE approvals SET status = 'rejected', response_msg = 'Rejected by dashboard', updated_at = ? WHERE id = ?",
            (now, approval_id),
        )
    return {"id": approval_id, "status": "rejected"}




@router.post("/api/trades/order-status/sync")
def sync_trade_order_status(days: int = 30):
    if trader.DRY_RUN:
        raise HTTPException(status_code=400, detail="Order status sync requires DRY_RUN=false")
    try:
        result = _sync_order_status_from_history(_get_api(), days=days)
        _clear_balance_cache()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _remove_non_broker_trade_rows() -> dict:
    """Remove local-only rows that must not survive a broker-authoritative sync."""
    removable_statuses = (
        "failed",
        "canceled",
        "rejected",
        "simulated",
    )
    placeholders = ",".join("?" for _ in removable_statuses)
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, ts, symbol, name, action, qty, price, order_status, response_msg
            FROM trades
            WHERE COALESCE(env, ?) = ?
              AND COALESCE(broker_order_id, '') = ''
              AND (
                    COALESCE(order_status, '') IN ({placeholders})
                    OR COALESCE(ok, 0) = 0
                  )
            ORDER BY ts ASC
            """,
            (trader.TRADING_ENV, trader.TRADING_ENV, *removable_statuses),
        ).fetchall()
        cursor = conn.execute(
            f"""
            DELETE FROM trades
            WHERE COALESCE(env, ?) = ?
              AND COALESCE(broker_order_id, '') = ''
              AND (
                    COALESCE(order_status, '') IN ({placeholders})
                    OR COALESCE(ok, 0) = 0
                  )
            """,
            (
                trader.TRADING_ENV,
                trader.TRADING_ENV,
                *removable_statuses,
            ),
        )
        removed_count = int(cursor.rowcount)
    return {
        "ok": True,
        "removed_count": removed_count,
        "scope": "local rows without broker order id",
        "items": [
            {
                **dict(row),
                "sync_type": "cleanup",
                "sync_result": "removed",
                "message": row["response_msg"] or "증권사 주문번호가 없는 불일치 기록 정리",
            }
            for row in rows
        ],
    }


def _save_trade_sync_result(result: dict) -> None:
    from src.db.trade_repository import save_trade_sync_run

    payload = {
        key: result.get(key)
        for key in (
            "ok",
            "synced_count",
            "balance_synced_count",
            "history_imported_count",
            "history_updated_count",
            "removed_mismatch_count",
            "history_error",
            "order_status_error",
            "sync_items",
            "status",
            "error",
            "started_at",
        )
    }
    now = trader.datetime.now(trader.KST).isoformat()
    run_id = str(result.get("run_id") or now)
    completed_at = result.get("completed_at")
    if not completed_at and result.get("status") != "running":
        completed_at = now
    payload.update({
        "run_id": run_id,
        "started_at": result.get("started_at") or now,
        "completed_at": completed_at,
    })
    save_trade_sync_run(payload)


def _migrate_trade_sync_file_to_db() -> None:
    """Import pre-DB runtime history once for backward compatibility."""
    from src.db.trade_repository import list_trade_sync_runs, save_trade_sync_run

    if list_trade_sync_runs(limit=1) or not TRADE_SYNC_RESULT_PATH.exists():
        return
    try:
        previous = json.loads(TRADE_SYNC_RESULT_PATH.read_text(encoding="utf-8"))
        file_runs = previous.get("runs") if isinstance(previous.get("runs"), list) else [previous]
        for index, item in enumerate(file_runs):
            if not isinstance(item, dict) or not item.get("completed_at"):
                continue
            completed_at = str(item["completed_at"])
            save_trade_sync_run({
                **item,
                "run_id": str(item.get("run_id") or f"legacy-{completed_at}-{index}"),
                "started_at": str(item.get("started_at") or completed_at),
                "status": str(item.get("status") or ("completed" if item.get("ok") else "failed")),
            })
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning(f"Failed to migrate trade sync runtime history: {exc}")


@router.get("/api/trades/sync/status")
def get_trade_sync_status():
    from src.db.trade_repository import list_trade_sync_runs

    _migrate_trade_sync_file_to_db()
    runs = list_trade_sync_runs(limit=50)
    if not runs:
        return {"ok": True, "available": False, "runs": []}
    if runs[0].get("status") == "running":
        with _trade_sync_lock:
            thread_alive = _trade_sync_thread is not None and _trade_sync_thread.is_alive()
        if not thread_alive:
            interrupted = {
                **runs[0],
                "status": "failed",
                "ok": False,
                "error": runs[0].get("error") or "서버 재기동으로 동기화가 중단되었습니다.",
                "completed_at": trader.datetime.now(trader.KST).isoformat(),
            }
            _save_trade_sync_result(interrupted)
            runs[0] = interrupted
    summaries = [
        {
            **run,
            "sync_item_count": len(run.get("sync_items") or []),
            "sync_items": [],
        }
        for run in runs
    ]
    return {"available": True, **summaries[0], "runs": summaries}


@router.get("/api/trades/sync/runs/{run_id}")
def get_trade_sync_run_detail(run_id: str):
    from src.db.trade_repository import get_trade_sync_run

    run = get_trade_sync_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="동기화 실행 기록을 찾을 수 없습니다.")
    return run




def _execute_trade_sync(*, days: int, run_id: str, started_at: str) -> dict:
    if trader.DRY_RUN:
        message = "모의 실행(DRY_RUN) 모드에서는 증권사 계좌 동기화를 사용할 수 없습니다."
        _save_trade_sync_result({
            "run_id": run_id,
            "started_at": started_at,
            "status": "failed",
            "ok": False,
            "error": message,
            "sync_items": [],
        })
        raise HTTPException(status_code=400, detail=message)
    _save_trade_sync_result({
        "run_id": run_id,
        "started_at": started_at,
        "status": "running",
        "ok": False,
        "sync_items": [],
    })
    try:
        api = _get_api()
        shared_history = None
        shared_history_error = None
        try:
            start_date, end_date = _order_history_window(days)
            shared_history = api.get_trade_history(start_date, end_date)
        except Exception as exc:
            shared_history_error = str(exc)

        order_status_sync = None
        order_status_error = None
        if shared_history is not None:
            try:
                order_status_sync = _sync_order_status_from_history(
                    api,
                    days=days,
                    history=shared_history,
                )
            except Exception as exc:
                order_status_error = str(exc)
        else:
            order_status_error = shared_history_error

        history_sync = None
        history_error = shared_history_error
        if shared_history is not None:
            try:
                history_sync = _sync_filled_trades_from_history(
                    api,
                    days=days,
                    history=shared_history,
                )
            except Exception as exc:
                history_error = str(exc)

        balance_data = _get_balance_data(api, allow_cache=False)
        parsed_balance = _parse_balance(balance_data)
        current_holdings = {h['symbol']: h for h in parsed_balance['holdings']}
        cleanup = _remove_non_broker_trade_rows()
        
        # Reconstruct current holdings from DB and Cloud
        cloud_trades = fetch_cloud_trades() or []
        local_trades = []
        with trader.connect_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM trades ORDER BY ts ASC").fetchall()
            local_trades = [dict(row) for row in rows]

        merged_trades = {}
        for t in cloud_trades + local_trades:
            ts = t.get("ts") or t.get("timestamp")
            if not ts: continue
            key = f"{ts}_{t.get('symbol')}_{t.get('action')}"
            merged_trades[key] = t
            
        trades = _account_trades(sorted(merged_trades.values(), key=lambda x: x.get("ts", "")))
        
        db_holdings = {}
        names = {}
        for t in trades:
            if not t.get("ok", False): continue
            sym = t["symbol"]
            qty = t["qty"]
            names[sym] = t.get("name", sym)
            if sym not in db_holdings:
                db_holdings[sym] = 0
            if t["action"] == "buy":
                db_holdings[sym] += qty
            elif t["action"] == "sell":
                db_holdings[sym] = max(0, db_holdings[sym] - qty)
                
        synced_count = 0
        balance_sync_items = []
        
        # 1. Sync missing buys (broker has more)
        for sym, ch in current_holdings.items():
            broker_qty = ch["qty"]
            db_qty = db_holdings.get(sym, 0)
            diff = broker_qty - db_qty
            
            if diff != 0:
                action = "buy" if diff > 0 else "sell"
                raw_stock = ch.get("_raw", {})
                price = int(float(raw_stock.get("pchs_avg_pric", ch["price"])))
                
                trader.save_trade(
                    symbol=sym,
                    name=ch["name"],
                    action=action,
                    qty=abs(diff),
                    price=price,
                    reason="증권사 잔고 강제 동기화(수동/누락분 보정)",
                    ok=True,
                    order_submission_enabled=False,
                    order_status="reconciled",
                    filled_qty=abs(diff),
                    filled_price=price,
                )
                synced_count += 1
                balance_sync_items.append({
                    "sync_type": "balance",
                    "sync_result": "reconciled",
                    "ts": trader.datetime.now(trader.KST).strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": sym,
                    "name": ch["name"],
                    "action": action,
                    "qty": abs(diff),
                    "price": price,
                    "broker_order_id": "",
                    "order_status": "reconciled",
                    "message": f"증권사 잔고 {broker_qty}주 / 기록 잔고 {db_qty}주 차이 보정",
                })
                
        # Calculate db average costs to use for selling missing items without affecting PnL
        db_costs = {}
        for t in trades:
            if not t.get("ok", False): continue
            sym = t["symbol"]
            qty = t["qty"]
            price = t["price"]
            if sym not in db_costs: db_costs[sym] = {"qty": 0, "cost": 0.0}
            if t["action"] == "buy":
                total_qty = db_costs[sym]["qty"] + qty
                total_cost = (db_costs[sym]["qty"] * db_costs[sym]["cost"]) + (qty * price)
                db_costs[sym]["qty"] = total_qty
                db_costs[sym]["cost"] = total_cost / total_qty if total_qty > 0 else 0
            elif t["action"] == "sell":
                db_costs[sym]["qty"] = max(0, db_costs[sym]["qty"] - qty)
                if db_costs[sym]["qty"] <= 0: db_costs[sym]["cost"] = 0

        # 2. Sync missing sells (broker has less or none)
        for sym, db_qty in db_holdings.items():
            if db_qty > 0 and sym not in current_holdings:
                avg_cost = int(db_costs.get(sym, {}).get("cost", 0))
                trader.save_trade(
                    symbol=sym,
                    name=names.get(sym, sym),
                    action="sell",
                    qty=db_qty,
                    price=avg_cost,  # Use avg_cost to avoid distorting Realized PnL

                    reason="증권사 잔고 강제 동기화(수량 매도 보정)",
                    ok=True,
                    order_submission_enabled=False,
                    order_status="reconciled",
                    filled_qty=db_qty,
                    filled_price=avg_cost,
                )
                synced_count += 1
                balance_sync_items.append({
                    "sync_type": "balance",
                    "sync_result": "reconciled",
                    "ts": trader.datetime.now(trader.KST).strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": sym,
                    "name": names.get(sym, sym),
                    "action": "sell",
                    "qty": db_qty,
                    "price": avg_cost,
                    "broker_order_id": "",
                    "order_status": "reconciled",
                    "message": "증권사에 없는 로컬 보유수량 전량 보정",
                })
                
        imported_count = _to_int(history_sync.get("imported_count")) if history_sync else 0
        updated_count = _to_int(history_sync.get("updated_count")) if history_sync else 0
        # 동기화로 보유/거래가 바뀌었으니 잔고·파생 보유탭 스냅샷을 무효화해 현행화한다.
        _clear_balance_cache()
        history_items = list((history_sync or {}).get("items") or [])
        order_status_items = [
            {
                "sync_type": "order_status",
                "sync_result": "updated" if item.get("balance_confirmed", True) else "checked",
                "ts": "",
                "symbol": item.get("symbol", ""),
                "name": item.get("name", ""),
                "action": item.get("action", ""),
                "qty": item.get("filled_qty", 0),
                "price": item.get("filled_price", 0),
                "broker_order_id": item.get("broker_order_id", ""),
                "order_status": item.get("order_status", ""),
                "message": "주문 상태 확인",
            }
            for item in ((order_status_sync or {}).get("orders") or [])
        ]
        sync_items = history_items + order_status_items + balance_sync_items + list(cleanup.get("items") or [])
        response = {
            "run_id": run_id,
            "started_at": started_at,
            "status": "completed",
            "ok": True,
            "synced_count": synced_count + imported_count,
            "balance_synced_count": synced_count,
            "history_imported_count": imported_count,
            "history_updated_count": updated_count,
            "history_sync": history_sync,
            "history_error": history_error,
            "order_status_sync": order_status_sync,
            "order_status_error": order_status_error,
            "removed_mismatch_count": cleanup["removed_count"],
            "cleanup": cleanup,
            "sync_items": sync_items,
        }
        _save_trade_sync_result(response)
        return response
    except Exception as e:
        _save_trade_sync_result({
            "run_id": run_id,
            "started_at": started_at,
            "status": "failed",
            "ok": False,
            "error": str(e),
            "sync_items": [],
        })
        logger.exception("Broker trade synchronization failed")
        return {
            "run_id": run_id,
            "started_at": started_at,
            "status": "failed",
            "ok": False,
            "error": str(e),
            "sync_items": [],
        }


def _run_trade_sync_background(*, days: int, run_id: str, started_at: str) -> None:
    global _trade_sync_thread
    logger.info(f"[TRADE_SYNC] started run_id={run_id} days={days}")
    try:
        result = _execute_trade_sync(days=days, run_id=run_id, started_at=started_at)
        logger.info(
            f"[TRADE_SYNC] finished run_id={run_id} "
            f"status={result.get('status')} synced={result.get('synced_count', 0)} "
            f"error={result.get('error') or '-'}"
        )
    finally:
        with _trade_sync_lock:
            _trade_sync_thread = None


@router.post("/api/trades/sync")
def sync_trades(days: int = 30):
    global _trade_sync_thread
    started_at = trader.datetime.now(trader.KST).isoformat()
    run_id = started_at
    if trader.DRY_RUN:
        message = "DRY_RUN 모드에서는 증권사 계좌 동기화를 실행할 수 없습니다."
        _save_trade_sync_result({
            "run_id": run_id,
            "started_at": started_at,
            "status": "failed",
            "ok": False,
            "error": message,
            "sync_items": [],
        })
        raise HTTPException(status_code=400, detail=message)

    with _trade_sync_lock:
        if _trade_sync_thread is not None and _trade_sync_thread.is_alive():
            raise HTTPException(status_code=409, detail="증권사 기록 동기화가 이미 실행 중입니다.")
        _save_trade_sync_result({
            "run_id": run_id,
            "started_at": started_at,
            "status": "running",
            "ok": False,
            "sync_items": [],
        })
        thread = threading.Thread(
            target=_run_trade_sync_background,
            kwargs={"days": days, "run_id": run_id, "started_at": started_at},
            name="broker-trade-sync",
            daemon=True,
        )
        _trade_sync_thread = thread
        thread.start()

    return {
        "run_id": run_id,
        "started_at": started_at,
        "status": "running",
        "ok": True,
    }



@router.get("/api/trades")
def get_trades(limit: int = 50, strategy_id: str | None = None):
    try:
        cloud_trades = fetch_cloud_trades() or []
        local_trades = []
        with trader.connect_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM trades ORDER BY ts ASC").fetchall()
            local_trades = [dict(row) for row in rows]
            for trade in local_trades:
                trade["_local_id"] = trade.get("id")

        from src.db.trade_repository import list_local_trade_cleanup_candidates

        cleanup_by_id = {
            int(item["id"]): item
            for item in list_local_trade_cleanup_candidates(max(limit, 200))
        }

        merged_trades = {}
        for t in cloud_trades + local_trades:
            ts = t.get("ts") or t.get("timestamp")
            if not ts: continue
            raw_local_id = t.get("_local_id")
            local_id = int(raw_local_id) if str(raw_local_id or "").isdigit() else None
            cleanup_item = cleanup_by_id.get(local_id) or {}
            key = f"{ts}_{t.get('symbol')}_{t.get('action')}"
            merged_trades[key] = {
                "local_id": local_id,
                "ts": ts,
                "symbol": t.get("symbol"),
                "name": t.get("name", t.get("symbol")),
                "action": t.get("action"),
                "qty": t.get("qty", 0),
                "price": t.get("price", 0),
                "reason": t.get("reason", ""),
                "ok": t.get("ok", 1),
                "env": t.get("env", "demo"),
                "dry_run": t.get("dry_run", 0),
                "broker_order_id": t.get("broker_order_id", ""),
                "order_status": t.get("order_status", ""),
                "filled_qty": _to_int(t.get("filled_qty")),
                "filled_price": _to_int(t.get("filled_price")),
                "response_msg": t.get("response_msg", ""),
                "strategy_id": t.get("strategy_id", ""),
                "strategy_version": t.get("strategy_version"),
                "profile_hash": t.get("profile_hash", ""),
                "source_approval_id": t.get("source_approval_id"),
                "cleanup_reason": cleanup_item.get("cleanup_reason"),
                "cleanup_risk": cleanup_item.get("cleanup_risk"),
            }
            
        trades = _account_trades(list(merged_trades.values()))
        if strategy_id:
            trades = [trade for trade in trades if str(trade.get("strategy_id") or "") == strategy_id]
        trades = sorted(trades, key=lambda x: x["ts"], reverse=True)
        return {"trades": trades[:limit]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/trades/local-cleanup")
def get_local_trade_cleanup_candidates(limit: int = 200):
    from src.db.trade_repository import list_local_trade_cleanup_candidates

    return {"trades": list_local_trade_cleanup_candidates(limit)}


@router.delete("/api/trades/local/{trade_id}")
def delete_local_trade(trade_id: int, confirm: bool = False):
    if not confirm:
        raise HTTPException(status_code=400, detail="confirm=true is required")

    from src.db.trade_repository import delete_local_trade_record

    try:
        deleted = delete_local_trade_record(trade_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "deleted_id": trade_id,
        "scope": "local_only",
        "symbol": deleted.get("symbol"),
        "order_status": deleted.get("order_status"),
    }




@router.get("/api/performance/periodic")
def get_periodic_performance(response: Response, strategy_id: str | None = None):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    try:
        trades = _load_merged_trades()
        if strategy_id:
            trades = [trade for trade in trades if str(trade.get("strategy_id") or "") == strategy_id]
        return _build_periodic_performance(trades)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/performance/forward")
def get_forward_performance(response: Response, strategy_id: str | None = None):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    try:
        trades = _load_merged_trades()
        strategies = _build_forward_strategy_performance(
            trades, strategy_id=strategy_id
        )
        account = _build_forward_account_performance(trades) if not strategy_id else None
        for row in strategies:
            row.pop("daily_nav", None)
        if account:
            account.pop("daily_nav", None)
        return {
            "schema_version": 2,
            "strategies": strategies,
            "account": account,
            "method": "cash_flow_matched_forward_ledger",
            "methodology": {
                "capital_basis": "synthetic_buy_shortfall",
                "return_method": "unitized_daily_nav",
                "benchmark_price": "previous_finalized_session_close",
                "costs": "excluded",
            },
            "manual_review_only": True,
        }
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/performance/forward/{strategy_id}/nav")
def get_forward_performance_nav(response: Response, strategy_id: str):
    from src.db.performance_repository import list_daily_nav
    response.headers["Cache-Control"] = "no-store"
    scope_type = "account" if strategy_id == "__account__" else "strategy"
    return {"strategy_id": strategy_id, "daily_nav": list_daily_nav(strategy_id, scope_type=scope_type)}


@router.patch("/api/performance/forward/{strategy_id}/review")
def update_forward_performance_review(
    strategy_id: str,
    payload: StrategyPerformanceReviewPayload,
):
    from src.db.performance_repository import save_strategy_performance_review
    from src.db.strategy_repository import load_ai_strategies
    from src.strategy_ids import AI_REBALANCE_STRATEGY_ID

    try:
        known_ids = {
            str(item.get("id")) for item in load_ai_strategies() if item.get("id")
        }
        known_ids.add(AI_REBALANCE_STRATEGY_ID)
        if strategy_id not in known_ids and strategy_id != "unattributed":
            raise ValueError(f"strategy not found: {strategy_id}")
        review = save_strategy_performance_review(
            strategy_id, payload.decision, payload.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "review": review, "trading_state_changed": False}


@router.get("/api/performance/account-cashflows")
def get_performance_account_cashflows(response: Response):
    from src.db.performance_repository import list_account_cashflows
    response.headers["Cache-Control"] = "no-store"
    return {"cashflows": list_account_cashflows(), "manual_confirmation_required": True}


@router.post("/api/performance/account-cashflows")
def save_performance_account_cashflow(payload: AccountCashflowPayload):
    from src.db.performance_repository import record_account_cashflow
    try:
        row = record_account_cashflow(
            external_ref=payload.external_ref,
            occurred_at=payload.occurred_at,
            amount=payload.amount,
            kind=payload.kind,
            confirmed=payload.confirmed,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "cashflow": row, "performance_recalculated": False}




@router.get("/api/performance")
def get_performance(response: Response, strategy_id: str | None = None):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    try:
        trades = _account_trades(_load_merged_trades())
        if strategy_id:
            trades = [trade for trade in trades if str(trade.get("strategy_id") or "") == strategy_id]
        record_started_at = min((str(t.get("ts") or "") for t in trades if t.get("ts")), default="")
        
        total_trades = len(trades)
        success_count = sum(1 for t in trades if t.get("ok", False))
        success_rate = (success_count / total_trades * 100) if total_trades > 0 else 0
        
        holdings = {}
        realized_pnl = 0
        names = {}
        
        for t in trades:
            if not t.get("ok", False): continue
            sym = t["symbol"]
            qty = t["qty"]
            price = t["price"]
            
            # Skip invalid qty or price <= 0 trades to avoid avg_cost and realized_pnl distortion
            if qty <= 0 or price <= 0:
                continue
                
            names[sym] = t.get("name", sym)
            
            if sym not in holdings:
                holdings[sym] = {"qty": 0, "cost": 0.0}
                
            if t["action"] == "buy":
                total_qty = holdings[sym]["qty"] + qty
                total_cost = (holdings[sym]["qty"] * holdings[sym]["cost"]) + (qty * price)
                holdings[sym]["qty"] = total_qty
                holdings[sym]["cost"] = total_cost / total_qty if total_qty > 0 else 0
            elif t["action"] == "sell":
                sell_qty = min(qty, holdings[sym]["qty"])
                profit = (price - holdings[sym]["cost"]) * sell_qty
                realized_pnl += profit
                holdings[sym]["qty"] -= sell_qty
                if holdings[sym]["qty"] <= 0:
                    holdings[sym]["qty"] = 0
                    holdings[sym]["cost"] = 0
                    
        # Explicitly calculate realized_pnl by summing daily periodic performance values to match daily performance view exactly
        try:
            periodic_perf = _build_periodic_performance(trades)
            realized_pnl = sum(day["realized_pnl"] for day in periodic_perf.get("daily", []))
        except Exception:
            pass
                    
        # Fetch current prices to calculate evaluation PnL
        current_holdings = {}
        total_broker_pnl = 0
        try:
            api = _get_api()
            balance_data = _get_balance_data(api)
            parsed_balance = _parse_balance(balance_data)
            current_holdings = {h['symbol']: h for h in parsed_balance['holdings']}
            total_broker_pnl = parsed_balance.get("pnl", 0)
        except Exception:
            pass

        # 사용자 요청: 불일치가 발생하면 증권사 잔고 정보에 맞춰 보정한다.
        # 자동매매 기록으로 추적한 보유량보다 증권사 실제 잔고를 우선한다.
        eval_details = []
        total_eval_pnl = total_broker_pnl
        
        if trader.DRY_RUN:
            total_eval_pnl = 0
            for sym, data in holdings.items():
                if data["qty"] > 0:
                    current_price = data["cost"]
                    if sym in current_holdings:
                        current_price = current_holdings[sym]["price"]
                    else:
                        try:
                            q = api.get_quote(sym)
                            current_price = q["current"]
                        except Exception:
                            pass
                    
                    eval_pnl = (current_price - data["cost"]) * data["qty"]
                    return_rate = ((current_price / data["cost"]) - 1) * 100 if data["cost"] > 0 else 0
                    total_eval_pnl += eval_pnl
                    
                    eval_details.append({
                        "symbol": sym,
                        "name": names.get(sym, sym),
                        "qty": data["qty"],
                        "avg_cost": data["cost"],
                        "current_price": current_price,
                        "eval_pnl": int(eval_pnl),
                        "return_rate": round(return_rate, 2),
                        "broker_qty": current_holdings.get(sym, {}).get("qty", 0),
                        "broker_pnl": int(current_holdings.get(sym, {}).get("pnl", 0)),
                        "diff_reason": "DRY_RUN"
                    })
        else:
            for sym, ch in current_holdings.items():
                raw_stock = ch.get("_raw", {})
                avg_cost = float(raw_stock.get("pchs_avg_pric", 0)) if raw_stock.get("pchs_avg_pric") else 0
                
                if avg_cost == 0 and ch["qty"] > 0:
                    avg_cost = ch["price"] - (ch["pnl"] / ch["qty"])
                    
                recorded_qty = holdings.get(sym, {}).get("qty", 0)
                diff_reason = ""
                if recorded_qty == 0:
                    diff_reason = "수동매수/기록누락 보정 완료"
                elif recorded_qty != ch["qty"]:
                    diff_reason = f"수량 불일치 {recorded_qty}주->{ch['qty']}주 보정 완료"
                    
                return_rate = ((ch["price"] / avg_cost) - 1) * 100 if avg_cost > 0 else 0.0
                eval_details.append({
                    "symbol": sym,
                    "name": ch["name"],
                    "qty": ch["qty"],
                    "avg_cost": avg_cost,
                    "current_price": ch["price"],
                    "eval_pnl": int(ch["pnl"]),
                    "return_rate": round(return_rate, 2),
                    "broker_qty": ch["qty"],
                    "broker_pnl": int(ch["pnl"]),
                    "diff_reason": diff_reason
                })

        untracked_details = []  # 호환성을 위해 유지하며 상세 내용은 eval_details에 수집한다.
                    
        return {
            "total_trades": total_trades,
            "success_rate": round(success_rate, 2),
            "realized_pnl": int(realized_pnl),
            "total_eval_pnl": int(total_eval_pnl),
            "total_broker_pnl": int(total_broker_pnl),
            "eval_details": eval_details,
            "untracked_details": untracked_details,
            "record_started_at": record_started_at,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/api/risk/status")
def get_risk_status():
    def _build():
        api = _get_api()
        balance_data = _get_balance_data(api, allow_cache=True)
        parsed = _parse_balance(balance_data)

        total_capital = trader.TOTAL_CAPITAL
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



@router.get("/api/decisions/history")
def get_decision_history(limit: int = 50):
    try:
        with trader.connect_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM decision_logs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
            logs = [dict(row) for row in rows]
            for log in logs:
                if isinstance(log.get("indicators"), str):
                    try:
                        log["indicators"] = json.loads(log["indicators"])
                    except:
                        pass
            return {"decisions": logs}
    except Exception as e:
        return {"decisions": []}



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
def get_scheduler_status(strategy_id: str | None = None, compact: bool = True):
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
        "trading_env": trader.TRADING_ENV,
        "dry_run": trader.DRY_RUN,
        "order_submission": trader.ORDER_SUBMISSION_ENABLED,
    }
    
    last_result = None
    try:
        from src.db.repository import load_recent_scheduler_results, load_latest_scheduler_result
        last_result = load_recent_scheduler_results(days=30)
        if last_result is None:
            last_result = load_latest_scheduler_result()
    except Exception:
        pass
        
    if last_result is None:
        path = Path(config["result_path"])
        if path.exists():
            try:
                last_result = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass

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
                    from src.db.ai_stock_repository import get_policy

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

    return {
        "config": config,
        "last_result": last_result,
        "run_state": run_state,
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
    if not _dashboard_scheduler_service.claim(
        mode=mode,
        strategy_id=",".join(strategy_ids),
    ):
        raise HTTPException(status_code=409, detail="스케줄러가 이미 실행 중입니다.")

    t = threading.Thread(
        target=_bg_run_multiple_scheduled_cycles,
        args=(mode, include_ai_rebalance, auto_approve, strategy_ids, allowed_categories),
        daemon=True
    )
    t.start()
    return {
        "status": "started",
        "mode": mode,
        "strategy_id": strategy_ids[0] if len(strategy_ids) == 1 else None,
        "strategy_ids": strategy_ids,
        "allowed_categories": sorted(allowed_categories) if allowed_categories else None,
    }
