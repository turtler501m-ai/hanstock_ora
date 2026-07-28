# -*- coding: utf-8 -*-
from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import src.dashboard.core as _core
from src.dashboard.core import *
from src.utils.logger import logger
globals().update({k: v for k, v in _core.__dict__.items() if not k.startswith('__')})

router = APIRouter(tags=["stock"])
TRADE_SYNC_RESULT_PATH = Path(".runtime/trade_sync_last_result.json")

class NewStrategyPayload(BaseModel):
    name: str = Field(..., min_length=1)
    model: str = "none"
    weight: float = 0.0
    description: str = ""
    profile: dict | None = None
    status: str | None = None


class UpdateStrategyPayload(BaseModel):
    name: str | None = None
    model: str | None = None
    weight: float | None = None
    description: str | None = None
    profile: dict | None = None
    status: str | None = None


class SelectStrategyPayload(BaseModel):
    selected: bool = True


class PaperCompletePayload(BaseModel):
    days: int = 20
    observations: int = 20
    return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    pass_result: bool | None = None
    notes: str | None = None


def _now_kst_text() -> str:
    return trader.datetime.now(trader.KST).strftime("%Y-%m-%d %H:%M:%S")


STRATEGY_DISPLAY_NAMES = {
    "seven_split": "기본 분할매매",
    "rule_only_default": "기본 기술룰",
    "gpt_5_mini_default": "GPT-5 미니 기본 전략",
    "ai_stock_default_v1": "AI 기본 종목발굴",
    "narrative_momentum_strategy": "내러티브 모멘텀",
    "plunge_bounce_strategy": "급락 반등",
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


def _strategy_display_name(strategy_id: str | None, fallback: str | None = None) -> str:
    sid = str(strategy_id or "").strip()
    text = str(fallback or "").strip()
    if text:
        return text
    return STRATEGY_DISPLAY_NAMES.get(sid, sid or "-")


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


def _json_safe(value):
    import math

    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _trim_text(value, limit: int = 500):
    if value is None:
        return value
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _tail_items(items, limit: int):
    if not isinstance(items, list):
        return []
    if len(items) <= limit:
        return list(items)
    return list(items[-limit:])


def _compact_scheduler_item(item, allowed_keys: set[str]) -> dict:
    if not isinstance(item, dict):
        return {"value": _trim_text(item)}
    compact = {key: item.get(key) for key in allowed_keys if key in item}
    for key in ("reason", "response_msg", "message"):
        if key in compact:
            compact[key] = _trim_text(compact[key])
    return compact


def _compact_scheduler_candidate_scan(candidate_scan) -> dict:
    if not isinstance(candidate_scan, dict):
        return {}
    candidates = candidate_scan.get("candidates")
    scan_summary = candidate_scan.get("scan_summary")
    scanned = candidate_scan.get("scanned", candidate_scan.get("scanned_count"))
    candidates_count = candidate_scan.get("candidates_count")
    if candidates_count is None and isinstance(candidates, list):
        candidates_count = len(candidates)
    candidate_keys = {"symbol", "name", "score", "price", "reasons", "reason"}
    return {
        "scanned": scanned,
        "scanned_count": scanned,
        "candidates_count": candidates_count,
        "candidates": [
            _compact_scheduler_item(item, candidate_keys)
            for item in _tail_items(candidates, 20)
        ],
        "scan_error": _trim_text(candidate_scan.get("scan_error")),
        "summary_count": len(scan_summary) if isinstance(scan_summary, list) else candidate_scan.get("summary_count"),
    }


def _compact_scheduler_status_result(last_result: dict | None, item_limit: int = 100) -> dict | None:
    if not isinstance(last_result, dict):
        return last_result
    result = last_result.get("result")
    if not isinstance(result, dict):
        return last_result

    plan_items = result.get("results") or []
    approved_items = result.get("auto_approved") or []
    approval_errors = result.get("auto_approval_errors") or []
    run_errors = result.get("errors") or result.get("retry_errors") or []

    if not isinstance(plan_items, list):
        plan_items = []
    if not isinstance(approved_items, list):
        approved_items = []
    if not isinstance(approval_errors, list):
        approval_errors = []
    if not isinstance(run_errors, list):
        run_errors = [run_errors] if run_errors else []

    queued_created = sum(1 for item in plan_items if isinstance(item, dict) and item.get("decision") == "queue")
    approved_executed = sum(1 for item in approved_items if isinstance(item, dict) and item.get("status") == "executed")
    approved_failed = sum(1 for item in approved_items if isinstance(item, dict) and item.get("status") == "failed")

    plan_keys = {
        "symbol", "name", "category", "decision", "approval_id", "action",
        "qty", "signal_qty", "price", "signal_price", "reason", "skip_reason",
        "time", "run_date", "run_recorded_at", "round", "strategy_id", "strategy_name",
    }
    approved_keys = {
        "id", "approval_id", "symbol", "name", "action", "qty", "price",
        "status", "response_msg", "message", "time", "run_date", "run_recorded_at",
        "round", "strategy_id", "strategy_name",
    }
    error_keys = {"approval_id", "message", "time", "run_date", "run_recorded_at", "round", "strategy_id", "strategy_name"}

    compact_result = {
        "results": [
            _compact_scheduler_item(item, plan_keys)
            for item in _tail_items(plan_items, item_limit)
        ],
        "auto_approved": [
            _compact_scheduler_item(item, approved_keys)
            for item in _tail_items(approved_items, item_limit)
        ],
        "auto_approval_errors": [
            _compact_scheduler_item(item, error_keys)
            for item in _tail_items(approval_errors, 50)
        ],
        "errors": [_trim_text(item) for item in _tail_items(run_errors, 50)],
        "status": result.get("status"),
        "ok": result.get("ok"),
        "summary_counts": {
            "plan_count": len(plan_items),
            "queue_count": max(0, queued_created - len(approved_items) - len(approval_errors)),
            "approved_count": approved_executed,
            "failed_count": approved_failed + len(approval_errors) + len(run_errors),
            "shown_plan_count": min(len(plan_items), item_limit),
            "shown_approved_count": min(len(approved_items), item_limit),
            "shown_approval_error_count": min(len(approval_errors), 50),
            "shown_error_count": min(len(run_errors), 50),
        },
    }

    if "candidate_scan" in result:
        compact_result["candidate_scan"] = _compact_scheduler_candidate_scan(result.get("candidate_scan"))

    for key in (
        "remaining_cash",
        "daily_loss_halt",
        "cash",
        "buying_cash",
        "buying_cash_info",
        "locked_holding_symbols",
        "retryable_sell_symbols",
        "strategy_id",
        "order_status_sync",
    ):
        if key in result and key not in compact_result:
            compact_result[key] = _json_safe(result.get(key))

    compact = {key: value for key, value in last_result.items() if key != "result"}
    compact["result"] = compact_result
    compact["compact"] = True
    return compact


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
    import json
    from src.config import config

    payload = _json_safe(dict(strategy))
    raw_validation = strategy.get("last_validation_result")
    if raw_validation:
        payload["last_validation_result"] = json.dumps(
            _validation_payload(strategy),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    payload["approval_gate"] = _approval_gate(strategy)
    payload["operation_status"] = _operation_status(strategy)
    payload["display_name"] = _strategy_display_name(strategy.get("id"), strategy.get("name"))
    payload["status_label"] = _strategy_status_label(strategy.get("status"))
    payload["selected_label"] = "현재 사용" if strategy.get("selected") else "대기"
    payload["approval_gate"]["label"] = _approval_gate_label(payload["approval_gate"])
    payload["operation_status"]["label"] = _operation_status_label(payload["operation_status"])
    payload["operation_status"]["reason_label"] = _operation_reason_label(payload["operation_status"])
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
    profile = strategy.get("profile") or {}
    risk = profile.get("risk") if isinstance(profile.get("risk"), dict) else {}
    require_paper = int(risk.get("paper_trading_required_days") or 0) > 0
    require_backtest = bool(getattr(trader.config, "ai_require_backtest_pass", True))
    missing = []
    if not _check_passed(strategy, "static"):
        missing.append("static verification")
    if strategy.get("provider") != "none" and not _check_passed(strategy, "api"):
        missing.append("api verification")
    if require_backtest and not _check_passed(strategy, "backtest"):
        missing.append("backtest")
    if require_paper and not _check_passed(strategy, "paper"):
        missing.append("paper trading")
    return {"ok": not missing, "missing": missing}


def _operation_status(strategy: dict) -> dict:
    gate = _approval_gate(strategy)
    status = str(strategy.get("status") or "")
    selected = bool(strategy.get("selected"))
    approved = status == "approved"
    ready = bool(selected and approved and gate.get("ok"))
    if ready:
        if bool(trader.DRY_RUN):
            mode = "dry_run"
        elif bool(trader.ENABLE_LIVE_TRADING) and str(trader.TRADING_ENV).lower() == "real":
            mode = "live"
        else:
            mode = "demo"
        reason = "selected, approved, and validation gate passed"
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
    qualification = None
    if not bool(getattr(config, "autonomy_require_approval", True)):
        qualification = _qualify_demo_strategy_one_click(id)
    from src.ai_stock.automation_service import run_strategy

    result = run_strategy(
        market=market,
        strategy_id=id,
        run_type="dashboard_manual",
    )
    return {
        "ok": not bool(result.get("autonomy", {}).get("error")),
        "qualification": qualification,
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
def get_strategy_context():
    from src.db.repository import load_ai_strategies

    strategies = load_ai_strategies()
    active = next((strategy for strategy in strategies if strategy.get("selected")), None)
    if active is None and strategies:
        active = strategies[0]
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
    return {
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
            "last_verified_at": active.get("last_verified_at") if active else None,
            "last_backtested_at": active.get("last_backtested_at") if active else None,
            "last_paper_started_at": active.get("last_paper_started_at") if active else None,
            "last_paper_completed_at": active.get("last_paper_completed_at") if active else None,
            "last_used_at": active.get("last_used_at") if active else None,
            "validation": _validation_payload(active) if active else {"checks": {}},
            "approval_gate": active_gate,
            "operation_status": active_operation,
        },
        "safety": {
            "trading_env": trader.TRADING_ENV,
            "dry_run": bool(trader.DRY_RUN),
            "enable_live_trading": bool(trader.ENABLE_LIVE_TRADING),
            "require_approval": bool(trader.REQUIRE_APPROVAL),
            "require_backtest_pass": bool(getattr(trader.config, "ai_require_backtest_pass", True)),
        },
        "fallback": {
            "mode": "rule_based" if not bool(getattr(trader.config, "ai_strategy_enabled", False)) else "",
            "openai_configured": bool(str(getattr(trader.config, "openai_api_key", "") or "").strip()),
        },
    }




@router.post("/api/ai-strategies")
def create_ai_strategy(payload: NewStrategyPayload):
    from src.db.repository import load_ai_strategies, normalize_ai_strategy, record_ai_strategy_event, save_ai_strategies
    import time
    import uuid

    strategies = load_ai_strategies()
    new_id = f"strategy_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    new_strat = normalize_ai_strategy({
        "id": new_id,
        "name": payload.name,
        "provider": "openai" if payload.model != "none" else "none",
        "model": payload.model,
        "weight": payload.weight,
        "description": payload.description,
        "selected": False,
        "status": payload.status or "draft",
        "profile": payload.profile,
        "strategy_version": 1,
    })
    strategies.append(new_strat)
    save_ai_strategies(strategies)
    record_ai_strategy_event(new_id, "created", {"name": payload.name, "model": payload.model}, 1)
    return {"ok": True, "strategy": new_strat}


@router.patch("/api/ai-strategies/{id}")
def update_ai_strategy(id: str, payload: UpdateStrategyPayload):
    from src.db.repository import load_ai_strategies, normalize_ai_strategy, record_ai_strategy_event, save_ai_strategies

    strategies = load_ai_strategies()
    found = None
    for idx, strategy in enumerate(strategies):
        if strategy["id"] != id:
            continue
        updated = dict(strategy)
        changes = payload.model_dump(exclude_unset=True)
        if "profile" in changes and changes["profile"] is not None:
            updated["profile"] = changes.pop("profile")
        updated.update({key: value for key, value in changes.items() if value is not None})
        updated["strategy_version"] = int(updated.get("strategy_version") or 1) + 1
        found = normalize_ai_strategy(updated)
        strategies[idx] = found
        break

    if not found:
        raise HTTPException(status_code=404, detail="Strategy not found")

    save_ai_strategies(strategies)
    record_ai_strategy_event(id, "updated", payload.model_dump(exclude_unset=True), found.get("strategy_version"))
    return {"ok": True, "strategy": found}


@router.delete("/api/ai-strategies/{id}")
def delete_ai_strategy(id: str):
    from src.db.repository import load_ai_strategies, record_ai_strategy_event, save_ai_strategies

    strategies = load_ai_strategies()
    target = next((strategy for strategy in strategies if strategy["id"] == id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if id in {"gpt_5_mini_default", "rule_only_default"}:
        raise HTTPException(status_code=409, detail="Built-in strategy cannot be deleted")

    save_ai_strategies([strategy for strategy in strategies if strategy["id"] != id])
    record_ai_strategy_event(id, "deleted", {"name": target.get("name")}, target.get("strategy_version"))
    return {"ok": True}




@router.post("/api/ai-strategies/{id}/select")
def select_ai_strategy(id: str, payload: SelectStrategyPayload):
    from src.db.repository import load_ai_strategies, record_ai_strategy_event, save_ai_strategies

    strategies = load_ai_strategies()
    found = None
    for strategy in strategies:
        if strategy["id"] == id:
            strategy["selected"] = payload.selected
            found = strategy

    if not found:
        raise HTTPException(status_code=404, detail="Strategy not found")

    save_ai_strategies(strategies)
    record_ai_strategy_event(id, "selected", {"selected": payload.selected}, found.get("strategy_version"))
    return {"ok": True}




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
            "paper_trading_required_days": 0,
        },
        "market_regime_filter": ["neutral", "bull", "low_volatility"],
        "backtest": {
            "commission_bps": 3,
            "slippage_bps": 5,
            "market_impact_bps": 2,
        },
        "allow_candidate_promotion": item["allow_candidate_promotion"],
        "preset": preset,
    }
    return item


@router.post("/api/ai-strategy-presets/{preset}/apply")
def apply_ai_strategy_preset(preset: str):
    from src.db.repository import load_ai_strategies, normalize_ai_strategy, record_ai_strategy_event, save_ai_strategies
    import json
    import time
    import uuid

    preset_data = _easy_strategy_preset(preset)
    now = _now_kst_text()
    strategy_id = f"easy_{preset}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    strategy = normalize_ai_strategy({
        "id": strategy_id,
        "name": preset_data["name"],
        "provider": "none",
        "model": "none",
        "weight": preset_data["weight"],
        "description": preset_data["description"],
        "selected": True,
        "status": "paper_passed",
        "profile": preset_data["profile"],
        "strategy_version": 1,
        "last_verified_at": now,
        "last_backtested_at": now,
        "last_used_at": now,
    })
    static_result = _static_validate_strategy(strategy)
    static_result["success"] = bool(static_result.get("ok"))
    backtest_result = _build_strategy_backtest(strategy)
    strategy["last_validation_result"] = json.dumps(
        {
            "checks": {
                "static": static_result,
                "backtest": backtest_result,
            },
            "latest": {"check": "preset_apply", "result": {"ok": True, "preset": preset}},
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    strategies = load_ai_strategies()
    for item in strategies:
        if item.get("name") == preset_data["name"]:
            item["status"] = "retired"
        item["selected"] = False
    strategies.append(strategy)
    save_ai_strategies(strategies)
    record_ai_strategy_event(
        strategy_id,
        "preset_applied",
        {"preset": preset, "label": preset_data["label"], "static": static_result, "backtest": backtest_result},
        1,
    )
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
    from src.strategy.seven_split import STOCK_NAMES, STOCK_SECTORS
    from src.market_metadata import resolve_stock_name, resolve_stock_sector
    data = load_watchlist_data()
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
    for code in symbols:
        extra = get_watchlist_extra_info(code)
        stored_name = str(names_by_symbol.get(code) or "").strip()
        static_name = STOCK_NAMES.get(code)
        symbols_detail.append({
            "symbol": code,
            "name": resolve_stock_name(code, stored_name or static_name),
            "sector": resolve_stock_sector(code, STOCK_SECTORS.get(code)),
            "price": extra["price"],
            "score": extra["score"],
            "reason": extra["reason"],
            "change_rate": extra["change_rate"],
            "rsi": extra["rsi"],
            "updated_at": extra["updated_at"]
        })
    return {
        "strategy_id": strategy_id,
        "inherited": inherited,
        "universe_source": "shared" if inherited or not strategy_id else "strategy",
        "symbols": symbols_detail,
        "ai_auto_add": data.get("ai_auto_add", False),
        "ai_auto_add_threshold": data.get("ai_auto_add_threshold", 3.0)
    }



@router.post("/api/watchlist")
def add_to_watchlist(payload: WatchlistAddPayload):
    from src.db.repository import load_watchlist_data, save_watchlist_data
    from src.strategy.seven_split import sync_watchlist_runtime, STOCK_NAMES
    from src.market_metadata import resolve_stock_name
    
    code = payload.symbol.strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail="유효하지 않은 종목코드 형식입니다. (6자리 숫자)")
        
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
def get_approvals(limit: int = 50):
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be greater than 0")
    limit = min(limit, 200)
    auto_approval_enabled = _auto_approval_enabled()
    _reclaim_stale_executing_approvals()

    _init_approval_db()
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM approvals ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    approvals = []
    latest_trades = _latest_trades_by_approval_ids([int(row["id"]) for row in rows])
    for row in rows:
        item = _approval_row(row)
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
        item["auto_approval_in_progress"] = (
            auto_approval_enabled
            and item.get("status") == "pending"
            and item.get("source") in {"dashboard_sell_all", "dashboard_holding_sell"}
        )
        approvals.append(item)
    return {"approvals": approvals}


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

    retry_qty = min(remaining_qty, _to_int(item.get("qty")) or remaining_qty)
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
    approval_id = _create_approval_row(payload)
    if _auto_approval_enabled():
        source = str(payload.get("source") or "")
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




@router.post("/api/holdings/sell-all")
def sell_all_holdings(payload: dict | None = Body(default=None)):
    missing = _required_env_missing()
    if missing:
        raise HTTPException(status_code=503, detail=f"Missing environment variables: {', '.join(missing)}")

    try:
        api = _get_api()
        parsed = _parse_balance(_get_balance_data(api, allow_cache=False))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"KIS balance API request failed: {e}") from e

    orders = []
    skipped = []
    for holding in parsed.get("holdings", []):
        symbol = str(holding.get("symbol", "")).strip()
        holding_qty = _to_int(holding.get("qty"))
        sellable_qty = _to_int(holding.get("sellable_qty", holding_qty))
        qty = min(holding_qty, sellable_qty) if holding_qty > 0 else 0
        if not symbol:
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

    if not orders:
        return {
            "status": "empty",
            "created_count": 0,
            "skipped_count": len(skipped),
            "skipped": skipped,
            "orders": [],
        }

    approval_ids = [_create_approval_row(order) for order in orders]
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
        "reconciled",
        "failed",
        "canceled",
        "rejected",
        "simulated",
    )
    placeholders = ",".join("?" for _ in removable_statuses)
    with trader.connect_db() as conn:
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
    }


def _save_trade_sync_result(result: dict) -> None:
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
        )
    }
    payload.update({
        "completed_at": trader.datetime.now(trader.KST).isoformat(),
    })
    TRADE_SYNC_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = TRADE_SYNC_RESULT_PATH.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temp_path.replace(TRADE_SYNC_RESULT_PATH)


@router.get("/api/trades/sync/status")
def get_trade_sync_status():
    if not TRADE_SYNC_RESULT_PATH.exists():
        return {"ok": True, "available": False}
    try:
        payload = json.loads(TRADE_SYNC_RESULT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read trade sync status: {exc}") from exc
    return {"available": True, **payload}




@router.post("/api/trades/sync")
def sync_trades(days: int = 90):
    if trader.DRY_RUN:
        raise HTTPException(status_code=400, detail="紐⑥쓽 ?ㅽ뻾(DRY_RUN) 紐⑤뱶?먯꽌??利앷텒??怨꾩쥖 ?숆린?붾? ?ъ슜?????놁뒿?덈떎.")
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
                    reason="利앷텒???붽퀬 媛뺤젣 ?숆린??(?섎룞/?꾨씫遺?蹂댁젙)",
                    ok=True,
                    order_submission_enabled=False,
                    order_status="reconciled",
                    filled_qty=abs(diff),
                    filled_price=price,
                )
                synced_count += 1
                
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

                    reason="利앷텒???붽퀬 媛뺤젣 ?숆린??(?꾨웾留ㅻ룄 蹂댁젙)",
                    ok=True,
                    order_submission_enabled=False,
                    order_status="reconciled",
                    filled_qty=db_qty,
                    filled_price=avg_cost,
                )
                synced_count += 1
                
        imported_count = _to_int(history_sync.get("imported_count")) if history_sync else 0
        updated_count = _to_int(history_sync.get("updated_count")) if history_sync else 0
        # 동기화로 보유/거래가 바뀌었으니 잔고·파생 보유탭 스냅샷을 무효화해 현행화한다.
        _clear_balance_cache()
        response = {
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
        }
        _save_trade_sync_result(response)
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/api/trades")
def get_trades(limit: int = 50):
    try:
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
            merged_trades[key] = {
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
            }
            
        trades = sorted(_account_trades(list(merged_trades.values())), key=lambda x: x["ts"], reverse=True)
        return {"trades": trades[:limit]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




@router.get("/api/performance/periodic")
def get_periodic_performance(response: Response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    try:
        trades = _load_merged_trades()
        return _build_periodic_performance(trades)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




@router.get("/api/performance")
def get_performance(response: Response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    try:
        trades = _account_trades(_load_merged_trades())
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

        # ?ъ슜???붿껌: 遺덉씪移섍? 諛쒖깮?섎㈃ 利앷텒???뺣낫濡?留욎떠??蹂댁젙
        # ?먮룞留ㅻℓ 湲곕줉(trades.json)?쇰줈 異붿쟻??蹂댁쑀????? 利앷텒???ㅼ젣 ?붽퀬瑜?媛뺤젣濡???뼱?뚯? (?? DRY_RUN???뚮뒗 DB ?곗꽑)
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

        untracked_details = [] # ???댁긽 ?ъ슜?섏? ?딆쓬 (紐⑤몢 eval_details濡??≪닔)
                    
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
    active_strategy_name = "기본 룰베이스 (Seven Split)"
    try:
        from src.db.repository import load_ai_strategies
        strategies = load_ai_strategies()
        strategy_name_by_id = {
            str(strategy.get("id") or ""): _strategy_display_name(strategy.get("id"), strategy.get("name"))
            for strategy in strategies
            if strategy.get("id")
        }
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

        schedules = list_strategy_schedules(enabled_only=False)
        schedule_items = []
        total_universe_count = 0
        for schedule in schedules:
            sid = schedule.get("strategy_id")
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
    if force_strategy_id is None and not strategy_ids:
        try:
            from src.db.repository import load_ai_strategies
            strategy_ids = [
                str(s.get("id")) for s in load_ai_strategies()
                if s.get("selected") and s.get("id")
            ]
        except Exception:
            strategy_ids = []

    if force_strategy_id and not strategy_ids:
        strategy_ids = [force_strategy_id]
    if not strategy_ids:
        strategy_ids = ["seven_split"]

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
