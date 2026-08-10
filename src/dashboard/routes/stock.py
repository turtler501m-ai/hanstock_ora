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
    "ai_rebalance": "AI 리밸런싱",
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
    "dashboard_strategy_holding_sell": ("수동 종목별 전략귀속 매도", "manual"),
    "dashboard_strategy_sell_all": ("수동 전략귀속 전량매도", "manual"),
    "signal": ("수동 신호 주문", "manual"),
    "candidate": ("수동 후보 주문", "manual"),
    "execution_plan": ("수동 실행계획 주문", "manual"),
    "portfolio-optimizer": ("포트폴리오 최적화", "tool"),
    "ai-allocation": ("AI 자산배분", "tool"),
    "scheduler-test": ("테스트 주문", "test"),
    "auto_trader": ("자동매매 · 전략 미기록", "automation"),
    "autonomous_strategy": ("자율매매 · 전략 미기록", "automation"),
    "trader": ("자동매매 · 전략 미기록", "automation"),
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
        if bool(trader.config.dry_run):
            mode = "dry_run"
        elif bool(trader.config.enable_live_trading) and str(trader.config.trading_env).lower() == "real":
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
        "dry_run": bool(trader.config.dry_run),
        "live_enabled": bool(trader.config.enable_live_trading),
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


from src.dashboard.routes import stock_analysis as _stock_analysis
from src.dashboard.routes import stock_order as _stock_order
from src.dashboard.routes import stock_performance as _stock_performance
from src.dashboard.routes import stock_plan as _stock_plan

globals().update({
    name: value
    for module in (_stock_analysis, _stock_order, _stock_performance, _stock_plan)
    for name, value in vars(module).items()
    if name != "router" and not name.startswith("__")
})
for _bounded_router in (
    _stock_analysis.router,
    _stock_order.router,
    _stock_performance.router,
    _stock_plan.router,
):
    globals().update({
        route.endpoint.__name__: route.endpoint
        for route in _bounded_router.routes
        if hasattr(route, "endpoint")
    })
router.include_router(_stock_analysis.router)
router.include_router(_stock_order.router)
router.include_router(_stock_performance.router)
router.include_router(_stock_plan.router)
