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

    strategy_runs = result.get("runs")
    if isinstance(strategy_runs, list):
        compact_runs = []
        blocked_count = 0
        for run in strategy_runs:
            if not isinstance(run, dict):
                continue
            run_result = run.get("result") if isinstance(run.get("result"), dict) else {}
            scan = run_result.get("scan") if isinstance(run_result.get("scan"), dict) else {}
            automation = (
                run_result.get("automation")
                if isinstance(run_result.get("automation"), dict)
                else {}
            )
            blocked = automation.get("blocked") if isinstance(automation.get("blocked"), list) else []
            blocked_count += len(blocked)
            compact_runs.append({
                "strategy_id": run.get("strategy_id"),
                "cycle_id": run.get("cycle_id"),
                "scan": _json_safe(scan),
                "automation": {
                    key: _json_safe(value)
                    for key, value in automation.items()
                    if key != "blocked"
                },
                "blocked": [_trim_text(item) for item in blocked],
                "autonomy_error": _trim_text(
                    (run_result.get("autonomy") or {}).get("error")
                    if isinstance(run_result.get("autonomy"), dict)
                    else None
                ),
            })
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        compact = {key: value for key, value in last_result.items() if key != "result"}
        compact["result"] = {
            "status": result.get("status"),
            "ok": result.get("ok"),
            "strategy_ids": result.get("strategy_ids") or [],
            "runs": compact_runs,
            "errors": [_trim_text(item) for item in errors],
            "summary_counts": {
                "run_count": len(compact_runs),
                "success_count": len(compact_runs),
                "blocked_count": blocked_count,
                "failed_count": len(errors),
            },
        }
        compact["compact"] = True
        return compact

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
        "execution_runs": _json_safe(_tail_items(result.get("execution_runs") or [], 200)),
        "summary_counts": {
            "plan_count": len(plan_items),
            "queue_count": max(0, queued_created - len(approved_items) - len(approval_errors)),
            "approved_count": len(approved_items) + len(approval_errors),
            "success_count": approved_executed,
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
