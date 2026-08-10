from __future__ import annotations

import argparse
import json
import os
import sys
import sqlite3
import time
from datetime import datetime
from pathlib import Path

# Add project root to sys.path to allow running as a script directly
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import trader
from src.notifier.slack import send_slack
from src.utils.market_calendar import is_market_session


SchedulerOperationError = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    sqlite3.Error,
)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _error_record(exc: Exception, *, attempt: int | None = None, approval_id: int | None = None) -> dict:
    record = {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }
    if attempt is not None:
        record["attempt"] = attempt
    if approval_id is not None:
        record["approval_id"] = approval_id
    return record


def _run_trader_with_retries(*, attempts: int, delay_seconds: float, kwargs: dict) -> dict:
    errors = []
    for attempt in range(1, attempts + 1):
        try:
            result = trader.run(**kwargs)
            if errors:
                result = {**result, "retry_errors": errors, "retry_count": len(errors)}
            return result
        except SchedulerOperationError as exc:
            errors.append(_error_record(exc, attempt=attempt))
            if attempt >= attempts:
                return {
                    "status": "failed",
                    "ok": False,
                    "results": [],
                    "errors": errors,
                }
            time.sleep(delay_seconds)
    return {"status": "failed", "ok": False, "results": [], "errors": errors}


def _approve_one_with_retries(approval_id: int, *, attempts: int, delay_seconds: float) -> dict:
    from src.dashboard import _approval_by_id, _approve_pending_approval

    def already_processed_result() -> dict | None:
        current = _approval_by_id(int(approval_id))
        if not current or current.get("status") == "pending":
            return None
        return {
            "approved": {
                "id": approval_id,
                "status": current.get("status"),
                "response_msg": current.get("response_msg", ""),
                "already_processed": True,
            },
            "errors": [],
        }

    errors = []
    for attempt in range(1, attempts + 1):
        try:
            processed = already_processed_result()
            if processed is not None:
                return processed
            result = _approve_pending_approval(int(approval_id), "scheduled auto approval")
            if errors:
                result = {**result, "retry_errors": errors, "retry_count": len(errors)}
            return {"approved": result, "errors": []}
        except Exception as exc:
            if "approval is already" in str(exc):
                processed = already_processed_result()
                if processed is not None:
                    return processed
            # Risk guards persist a rejected approval before raising HTTP 409.
            # A rejection is an expected per-order outcome, so keep processing
            # the remaining approvals instead of failing the entire schedule.
            current = _approval_by_id(int(approval_id))
            if current and current.get("status") == "rejected":
                return {
                    "approved": {
                        "id": approval_id,
                        "status": "rejected",
                        "response_msg": current.get("response_msg", ""),
                        "already_processed": True,
                    },
                    "errors": [],
                }
            if not isinstance(exc, SchedulerOperationError):
                raise
            errors.append(_error_record(exc, attempt=attempt, approval_id=approval_id))
            if attempt >= attempts:
                return {"approved": None, "errors": errors}
            time.sleep(delay_seconds)
    return {"approved": None, "errors": errors}


def _approve_created_approvals(result: dict, *, allowed_categories: set[str] | None = None) -> dict:
    approved = []
    errors = []
    attempts = _env_int("HANSTOCK_APPROVAL_RETRIES", 2)
    delay_seconds = _env_float("HANSTOCK_APPROVAL_DELAY_SECONDS", 1.2)
    for row in result.get("results", []):
        if allowed_categories is not None and row.get("category") not in allowed_categories:
            continue
        approval_id = row.get("approval_id")
        if not approval_id:
            continue
        outcome = _approve_one_with_retries(int(approval_id), attempts=attempts, delay_seconds=delay_seconds)
        if outcome["approved"] is not None:
            approved.append(outcome["approved"])
        errors.extend(outcome["errors"])
        time.sleep(delay_seconds)
    return {"approved": approved, "errors": errors}


def _order_status_sync_enabled() -> bool:
    return os.environ.get("HANSTOCK_ORDER_STATUS_SYNC", "true").lower() not in {"0", "false", "no", "off"}


def _result_submitted_orders(result: dict) -> bool:
    if any(row.get("status") == "executed" for row in result.get("auto_approved", []) or []):
        return True
    return any(row.get("decision") == "execute" and row.get("ok") for row in result.get("results", []) or [])


def _sync_order_status_after_cycle(result: dict) -> dict:
    if not _order_status_sync_enabled():
        return result
    if trader.runtime_flags().dry_run or not trader.runtime_flags().order_submission_enabled:
        return result
    if not _result_submitted_orders(result):
        return result
    try:
        from src.dashboard import _get_api, _sync_order_status_from_history

        days = _env_int("HANSTOCK_ORDER_STATUS_SYNC_DAYS", 1)
        sync_result = _sync_order_status_from_history(_get_api(), days=days)
        return {**result, "order_status_sync": sync_result}
    except SchedulerOperationError as exc:
        return {**result, "order_status_sync_error": _error_record(exc)}


def _sync_order_status_before_cycle() -> dict | None:
    if not _order_status_sync_enabled():
        return None
    if trader.runtime_flags().dry_run or not trader.runtime_flags().order_submission_enabled:
        return None
    try:
        from src.dashboard import _get_api, _sync_order_status_from_history

        days = _env_int("HANSTOCK_ORDER_STATUS_SYNC_DAYS", 1)
        return _sync_order_status_from_history(_get_api(), days=days)
    except SchedulerOperationError as exc:
        return {"ok": False, "error": _error_record(exc)}


def _write_cycle_result(result: dict, *, mode: str, strategy_id: str | None = None) -> None:
    if mode == "daily_auto":
        path = Path(os.environ.get("HANSTOCK_SCHEDULER_RESULT_PATH", ".runtime/daily_auto_last_result.json"))
    elif strategy_id == "plunge_bounce_strategy":
        path = Path(".runtime/plunge_bounce_last_result.json")
    elif strategy_id == "heikin_ashi_scalping_strategy":
        path = Path(".runtime/heikin_ashi_scalping_last_result.json")
    elif strategy_id == "volatility_adaptive_momentum_strategy":
        path = Path(".runtime/volatility_adaptive_momentum_last_result.json")
    else:
        path = Path(os.environ.get("HANSTOCK_SCHEDULER_RESULT_PATH", ".runtime/daily_auto_last_result.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    recorded_at = datetime.now(trader.KST).isoformat()
    
    # ensure strategy_id is populated in result
    result["strategy_id"] = strategy_id or "seven_split"
    
    payload = {
        "mode": mode,
        "recorded_at": recorded_at,
        "result": result,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    
    # Save to database
    try:
        from src.db.repository import save_scheduler_result
        save_scheduler_result(mode, recorded_at, result)
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as exc:
        from src.utils.logger import logger

        logger.warning(f"Failed to persist scheduler result: {exc}")


def _slack_enabled() -> bool:
    return os.environ.get("HANSTOCK_SCHEDULER_SLACK", "true").lower() not in {"0", "false", "no", "off"}


def _slack_cycle_start(*, mode: str) -> None:
    # A single completion/error summary is enough for scheduled cycles.
    # Keeping this hook as a no-op preserves compatibility with older callers.
    return


def _slack_cycle_result(result: dict, *, mode: str) -> None:
    if mode != "daily_auto" or not _slack_enabled():
        return

    results = result.get("results", []) or []
    approved = result.get("auto_approved", []) or []
    approval_errors = result.get("auto_approval_errors", []) or []
    run_errors = result.get("errors", []) or result.get("retry_errors", []) or []
    failed = result.get("status") == "failed" or result.get("ok") is False or bool(approval_errors)
    color = "#e74c3c" if failed else "#36a64f"
    status = "문제 발생" if failed else "정상 완료"

    plan_count = len(result.get("plan", []) or [])
    queued_created_count = sum(1 for row in results if row.get("decision") == "queue")
    queued_count = max(0, queued_created_count - len(approved) - len(approval_errors))
    approved_count = sum(1 for row in approved if row.get("status") == "executed")
    failed_approval_count = sum(1 for row in approved if row.get("status") == "failed") + len(approval_errors)
    retry_count = int(result.get("retry_count", 0) or 0)

    status_line = f"*[한스톡 VM] AI 자동매매 {status}*"
    details_line = (
        f"계획/승인대기/완료: {plan_count} / {queued_count} / {approved_count} | "
        f"실패: {failed_approval_count} | 재시도: {retry_count}\n"
        f"환경: {trader.runtime_flags().trading_env}(dry={trader.runtime_flags().dry_run}, order_sub={trader.runtime_flags().order_submission_enabled})"
    )

    if approval_errors:
        first = approval_errors[0]
        details_line += f"\n*승인 오류*: approval={first.get('approval_id', '-')} {first.get('message', '')}"
    elif run_errors:
        first = run_errors[-1]
        details_line += f"\n*실행 오류*: {first.get('type', 'Error')} {first.get('message', '')}"

    send_slack(
        text=f"[한스톡 VM] AI 자동매매 {status}",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": f"{status_line}\n{details_line}"}},
        ],
        color=color,
    )


def run_scheduled_cycle(
    mode: str = "execute",
    *,
    include_ai_rebalance: bool = False,
    auto_approve: bool = False,
    force_strategy_id: str | None = None,
    allowed_categories: set[str] | None = None,
    persist_result: bool = True,
    pre_order_status_sync: dict | None = None,
) -> dict:
    # force_strategy_id가 명시되지 않은 경우(cron 등) 현재 선택된 전략을 사용해
    # trader 실행과 결과 기록의 strategy_id가 일치하도록 한다.
    if force_strategy_id is None:
        try:
            from src.db.repository import load_ai_strategies
            active = next((s for s in load_ai_strategies() if s.get("selected")), None)
            if active and active.get("model") and active.get("model") != "none":
                force_strategy_id = active.get("model")
        except SchedulerOperationError:
            force_strategy_id = None

    if mode == "daily_auto":
        include_ai_rebalance = True
        auto_approve = True
        run_mode = "analysis_only"
        execution_categories = allowed_categories or {"ai_rebalance"}
        approval_categories = allowed_categories or {"ai_rebalance"}
        run_attempts = _env_int("HANSTOCK_DAILY_AUTO_RETRIES", 3)
        retry_delay_seconds = _env_float("HANSTOCK_DAILY_AUTO_RETRY_DELAY_SECONDS", 10.0)
    else:
        run_mode = mode
        execution_categories = allowed_categories
        approval_categories = allowed_categories
        run_attempts = _env_int("HANSTOCK_SCHEDULER_RETRIES", 1)
        retry_delay_seconds = _env_float("HANSTOCK_SCHEDULER_RETRY_DELAY_SECONDS", 5.0)

    _slack_cycle_start(mode=mode)
    if include_ai_rebalance:
        trader_kwargs = {
            "mode": run_mode,
            "include_ai_rebalance": True,
            "execution_categories": execution_categories,
        }
        if force_strategy_id is not None:
            trader_kwargs["force_strategy_id"] = force_strategy_id
        result = _run_trader_with_retries(
            attempts=run_attempts,
            delay_seconds=retry_delay_seconds,
            kwargs=trader_kwargs,
        )
    else:
        trader_kwargs = {"mode": run_mode}
        if execution_categories is not None:
            trader_kwargs["execution_categories"] = execution_categories
        if force_strategy_id is not None:
            trader_kwargs["force_strategy_id"] = force_strategy_id
        result = _run_trader_with_retries(
            attempts=run_attempts,
            delay_seconds=retry_delay_seconds,
            kwargs=trader_kwargs,
        )

    approval_result = (
        _approve_created_approvals(result, allowed_categories=approval_categories)
        if auto_approve
        else {"approved": [], "errors": []}
    )
    if auto_approve:
        result = {
            **result,
            "auto_approved": approval_result["approved"],
            "auto_approval_errors": approval_result["errors"],
        }
    if pre_order_status_sync is not None:
        result["pre_order_status_sync"] = pre_order_status_sync
    result["strategy_id"] = force_strategy_id or "seven_split"
    if persist_result and (mode == "daily_auto" or force_strategy_id):
        _write_cycle_result(result, mode=mode, strategy_id=force_strategy_id)
        if mode == "daily_auto":
            _slack_cycle_result(result, mode=mode)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Seven Split scheduled trading runner")
    parser.add_argument(
        "--mode",
        choices=["execute", "analysis_only", "daily_auto"],
        default="execute",
        help=(
            "execute orders immediately when policy allows, queue analysis output only, "
            "or run daily AI rebalance with automatic approval"
        ),
    )
    parser.add_argument(
        "--include-ai-rebalance",
        action="store_true",
        help="include AI target-weight rebalance rows in the scheduled plan",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="approve only approvals created by this scheduler run",
    )
    parser.add_argument(
        "--force-strategy-id",
        type=str,
        default=None,
        help="force a specific active strategy model for this scheduler run",
    )
    args = parser.parse_args()
    force_run = os.environ.get("HANSTOCK_SCHEDULE_FORCE") == "1"
    if args.mode == "daily_auto" and not force_run:
        now = datetime.now(trader.KST)
        if not is_market_session("KR", now):
            print(f"[scheduler] {now.date()} is not a KRX trading session; skipped")
            return 0
    cycle_kwargs = {
        "mode": args.mode,
        "include_ai_rebalance": args.include_ai_rebalance,
        "auto_approve": args.auto_approve,
    }
    if args.force_strategy_id is not None:
        cycle_kwargs["force_strategy_id"] = args.force_strategy_id
    result = run_scheduled_cycle(**cycle_kwargs)
    if result.get("status") == "failed" or result.get("ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
