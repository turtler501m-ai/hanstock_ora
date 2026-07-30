# -*- coding: utf-8 -*-
"""자동화 엔진/정책 (§5.12·§6.6).

automation_level 게이트를 서버에서만 판정한다. 정책 변경 시 검증·승인 상태를
무효화하고, Level 5/6(자동 승인·주문)은 안전 가드가 모두 열려야만 허용한다.
신규 브로커 우회 경로를 만들지 않고 기존 시장별 경로만 사용한다.
"""
from __future__ import annotations

from typing import Any

from src.ai_stock.constants import (
    AUTOMATION_APPROVE,
    AUTOMATION_EXECUTE,
    AUTOMATION_PLAN,
)
from src.ai_stock.markets import require_storable_market
from src.ai_stock.freshness import is_stale
from src.ai_stock.safety import live_trading_allowed
from src.db import ai_stock_repository as repo


def _daily_completed_orders(market: str | None, strategy_id: str | None) -> int:
    from src.ai_stock.freshness import now, parse_ts

    today = now().date()
    count = 0
    for run in repo.list_execution_runs(market=market, strategy_id=strategy_id, limit=500):
        ts = parse_ts(run.get("started_at"))
        if ts and ts.date() == today and run.get("status") == "completed" and run.get("candidate_id"):
            count += 1
    return count


def set_policy(strategy_id: str, market: str, fields: dict[str, Any]) -> dict[str, Any]:
    market = require_storable_market(market)
    # 안전 가드: Level 5/6 요청이어도 환경 가드가 닫혀 있으면 자동 집행 플래그를 강제로 끈다.
    fields = dict(fields or {})
    level = _clamp_level(fields.get("automation_level", AUTOMATION_PLAN))
    fields["automation_level"] = level
    for key in ("enabled", "auto_approve", "auto_execute", "allow_fallback_trade", "allow_stale_data_trade"):
        if key in fields:
            fields[key] = 1 if _truthy(fields[key]) else 0
    if fields.get("auto_approve") and level < AUTOMATION_APPROVE:
        fields["auto_approve"] = 0
    if fields.get("auto_execute") and level < AUTOMATION_EXECUTE:
        fields["auto_execute"] = 0
    if level >= AUTOMATION_EXECUTE and not live_trading_allowed():
        fields["auto_execute"] = 0
    policy = repo.upsert_policy(strategy_id, market, fields)
    # 정책 변경 → 검증·승인 무효화 기록 (§5.8). 기존 전략 lifecycle 이벤트 재사용.
    try:
        from src.db.strategy_repository import record_ai_strategy_event

        record_ai_strategy_event(
            strategy_id,
            "ai_stock_policy_changed",
            {"market": market, "fields": list(fields.keys())},
            1,
        )
    except Exception:
        pass
    return policy


def _clamp_level(value: Any) -> int:
    try:
        level = int(value)
    except (TypeError, ValueError):
        level = AUTOMATION_PLAN
    return max(0, min(AUTOMATION_EXECUTE, level))


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def evaluate_gate(*, policy: dict[str, Any] | None, candidate: dict[str, Any],
                  stage: str) -> dict[str, Any]:
    """단계별 자동 진행 가능 여부 (§5.12.4).

    stage: 'plan' | 'approve' | 'execute'. 정책·안전·점수·리스크를 평가해
    proceed/blocked와 사유를 반환한다.
    """
    pol = policy or {}
    level = int(pol.get("automation_level", AUTOMATION_PLAN) or AUTOMATION_PLAN)
    blocked: list[str] = []

    stage_level = {"plan": AUTOMATION_PLAN, "approve": AUTOMATION_APPROVE, "execute": AUTOMATION_EXECUTE}[stage]
    if level < stage_level:
        blocked.append(f"automation_level<{stage_level}")

    if (candidate.get("final_score") or 0) < float(pol.get("min_final_score") or 65.0):
        blocked.append("final_score")
    if (candidate.get("rule_score") or 0) < float(pol.get("min_rule_score") or 40.0):
        blocked.append("rule_score")
    if (candidate.get("risk_score") or 0) > float(pol.get("max_risk_score") or 60.0):
        blocked.append("risk_score")
    if candidate.get("fallback_used") and not int(pol.get("allow_fallback_trade") or 0):
        blocked.append("fallback_not_allowed")
    if is_stale(candidate.get("data_as_of"), "ai_eval") and not int(pol.get("allow_stale_data_trade") or 0):
        blocked.append("stale_data")
    if not int(pol.get("enabled", 1)):
        blocked.append("policy_disabled")

    if stage == "approve" and not int(pol.get("auto_approve") or 0):
        blocked.append("auto_approve_off")
    if stage in {"approve", "execute"}:
        strategy_block = _strategy_gate(candidate)
        if strategy_block:
            blocked.append(strategy_block)

    if stage == "execute":
        if not int(pol.get("auto_execute") or 0):
            blocked.append("auto_execute_off")
        if not live_trading_allowed():
            blocked.append("live_trading_guarded")
        max_daily = int(pol.get("max_daily_orders") or 0)
        if max_daily > 0 and _daily_completed_orders(candidate.get("market"), candidate.get("strategy_id")) >= max_daily:
            blocked.append("max_daily_orders")

    return {"stage": stage, "proceed": len(blocked) == 0, "blocked_reason": blocked, "automation_level": level}


def evaluate_manual_approval_gate(
    *,
    policy: dict[str, Any] | None,
    candidate: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Gate a user-triggered approval queue request without requiring auto_approve."""
    pol = dict(policy or {})
    pol["auto_approve"] = 1
    gate = evaluate_gate(policy=pol, candidate=candidate, stage="approve")
    blocked = list(gate["blocked_reason"])

    if plan.get("status") not in {"planned", "approval_queued"}:
        blocked.append("plan_status")
    if plan.get("approval_id"):
        blocked.append("approval_already_queued")
    if str(plan.get("market") or "") != str(candidate.get("market") or ""):
        blocked.append("market_mismatch")
    if int(plan.get("candidate_id") or 0) != int(candidate.get("candidate_id") or 0):
        blocked.append("candidate_mismatch")
    if int(plan.get("quantity") or 0) <= 0 or float(plan.get("entry_price") or 0) <= 0:
        blocked.append("invalid_plan_order_fields")

    return {
        **gate,
        "stage": "manual_approval",
        "proceed": len(blocked) == 0,
        "blocked_reason": blocked,
    }


def _strategy_gate(candidate: dict[str, Any]) -> str | None:
    strategy_id = str(candidate.get("strategy_id") or "").strip()
    if not strategy_id:
        return None
    try:
        from src.db.strategy_repository import load_ai_strategies

        strategies = load_ai_strategies()
    except Exception:
        return "strategy_lookup_failed"
    found = next((s for s in strategies if str(s.get("id")) == strategy_id), None)
    if not found:
        return "strategy_not_found"
    if str(found.get("status") or "") in {"review_required", "suspended", "retired"}:
        return "strategy_not_operational"
    expected_hash = candidate.get("profile_hash")
    actual_hash = found.get("profile_hash")
    if expected_hash and actual_hash and str(expected_hash) != str(actual_hash):
        return "profile_hash_mismatch"
    expected_version = candidate.get("strategy_version")
    actual_version = found.get("strategy_version")
    if expected_version and actual_version and int(expected_version) != int(actual_version):
        return "strategy_version_mismatch"
    return None


def _queue_approval(market: str, candidate: dict[str, Any], plan: dict[str, Any], strategy_id: str) -> int:
    """승인 대기열 등록(pending). 기존 시장별 경로만 사용, 브로커 직접 호출 없음(§5.9)."""
    from datetime import datetime, timezone, timedelta

    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S")
    qty = int(plan.get("quantity") or 0)
    price = int(plan.get("entry_price") or 0)
    if qty <= 0 or price <= 0:
        raise ValueError("invalid qty/price for approval")
    reason = f"AI스톡 자동후보 final={candidate.get('final_score')} ({candidate.get('decision')})"

    if market == "KR":
        from src.db.repository import init_db, connect_db

        init_db()
        with connect_db() as conn:
            cur = conn.execute(
                "INSERT INTO approvals (created_at, updated_at, symbol, name, action, qty, price, "
                "reason, source, status, response_msg, strategy_id) "
                "VALUES (?, ?, ?, ?, 'buy', ?, ?, ?, 'ai_stock', 'pending', '', ?)",
                (now, now, candidate.get("symbol"), candidate.get("name") or candidate.get("symbol"),
                 qty, price, reason, strategy_id),
            )
            conn.commit()
            return int(cur.lastrowid)
    # US: 미스톡 기존 approvals 경로로만 등록(§5.9, 신규 우회 없음).
    from src.mistock import db as mistock_db

    mistock_db.init_db()
    conn = mistock_db.connect_db()
    try:
        cur = conn.execute(
            "INSERT INTO approvals (created_at, updated_at, symbol, name, action, qty, price, "
            "reason, source, status, response_msg) "
            "VALUES (?, ?, ?, ?, 'buy', ?, ?, ?, 'ai_stock', 'pending', '')",
            (now, now, candidate.get("symbol"), candidate.get("name") or candidate.get("symbol"),
             float(qty), float(price), reason),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _execute_order(
    market: str,
    candidate: dict[str, Any],
    plan: dict[str, Any],
    strategy_id: str,
    approval_id: int | None,
) -> dict[str, Any]:
    """Compatibility symbol: legacy direct broker execution is disabled."""
    return {
        "ok": False,
        "status": "blocked",
        "message": "legacy direct execution disabled; use autonomy runtime",
    }



def _approval_db(market: str) -> str:
    return "main" if market == "KR" else "mistock"


def _extract_order_refs(order_result: dict[str, Any] | None) -> dict[str, Any]:
    result = order_result or {}
    nested = result.get("res") if isinstance(result.get("res"), dict) else {}
    output = nested.get("output") if isinstance(nested.get("output"), dict) else {}
    broker_order_id = (
        result.get("broker_order_id")
        or result.get("order_no")
        or nested.get("order_no")
        or nested.get("ODNO")
        or output.get("ODNO")
        or output.get("odno")
    )
    order_id = result.get("order_id") or result.get("approval_id") or result.get("id")
    return {
        "order_id": int(order_id) if str(order_id or "").isdigit() else None,
        "broker_order_id": str(broker_order_id) if broker_order_id else None,
    }


def run_strategy(*, market: str, strategy_id: str = "ai_stock_default_v1",
                 run_type: str = "scheduled") -> dict[str, Any]:
    """스케줄/수동 자동화 진입점 (§5.12.2).

    1차 스캔 → 정책 automation_level에 따라 관찰/확인/계획까지 자동 진행한다.
    승인·주문(Level 5/6)은 gate를 평가하되, 안전 가드가 닫혀 있으면 blocked로 기록만
    하고 실제 집행은 기존 시장별 경로에서만 수행한다(여기서 브로커를 직접 호출하지 않음).
    """
    from src.ai_stock import discovery_service, watchlist_service, execution_plan_service
    from src.ai_stock.constants import (
        AUTOMATION_WATCH, AUTOMATION_CONFIRM, AUTOMATION_PLAN, AUTOMATION_APPROVE,
        DECISION_STRONG_WATCH, DECISION_WATCH, WATCH_WATCHING,
    )

    market = require_storable_market(market)
    scan = discovery_service.run_scan(market=market, strategy_id=strategy_id, options={})
    from src.config import config

    policy = repo.get_policy(strategy_id, market) or {}
    if bool(getattr(config, "autonomy_enabled", False)) and not policy:
        policy = repo.upsert_policy(
            strategy_id,
            market,
            {
                "enabled": 1,
                "automation_level": AUTOMATION_APPROVE,
                "auto_approve": 1,
                "auto_execute": 0,
            },
        )
    level = int(policy.get("automation_level", AUTOMATION_PLAN) or AUTOMATION_PLAN)

    summary = {"scan_id": scan["scan_id"], "automation_level": level,
               "registered": 0, "confirmed": 0, "planned": 0, "approved": 0, "blocked": []}

    if not int(policy.get("enabled", 1)):
        # 정책 비활성화는 1차 스캔은 남기되 관찰/확인/계획/승인/주문 전 과정을 건너뛴다.
        summary["blocked"].append("policy_disabled")
        return {"scan": scan["summary"], "automation": summary}

    if (
        bool(getattr(config, "autonomy_enabled", False))
        and level >= AUTOMATION_PLAN
    ):
        try:
            from src.strategy.autonomy.ai_stock_integration import (
                run_ai_stock_autonomy_cycle,
            )

            autonomy = run_ai_stock_autonomy_cycle(
                market=market,
                strategy_id=strategy_id,
                scan_id=int(scan["scan_id"]),
                run_type=run_type,
            )
            summary["planned"] = len(autonomy["managed_orders"])
            summary["approved"] = len(autonomy["approvals"])
            repo.log_execution_run({
                "strategy_id": strategy_id,
                "market": market,
                "scan_id": scan["scan_id"],
                "run_type": run_type,
                "automation_level": level,
                "status": "completed",
                "policy_snapshot": {
                    **policy,
                    "execution_path": "autonomy",
                    "cycle_key": autonomy["cycle_key"],
                },
            })
            return {
                "scan": scan["summary"],
                "automation": summary,
                "autonomy": autonomy,
            }
        except Exception as exc:
            reason = f"autonomy:{type(exc).__name__}:{exc}"
            summary["blocked"].append(reason)
            repo.log_execution_run({
                "strategy_id": strategy_id,
                "market": market,
                "scan_id": scan["scan_id"],
                "run_type": run_type,
                "automation_level": level,
                "status": "blocked",
                "blocked_stage": "autonomy",
                "blocked_reason": reason,
                "policy_snapshot": {
                    **policy,
                    "execution_path": "autonomy",
                },
            })
            return {
                "scan": scan["summary"],
                "automation": summary,
                "autonomy": {"enabled": True, "error": str(exc)},
            }

    if level < AUTOMATION_WATCH:
        return {"scan": scan["summary"], "automation": summary}

    candidates = repo.list_candidates(market=market, scan_id=scan["scan_id"])
    for cand in candidates:
        if cand.get("decision") not in (DECISION_STRONG_WATCH, DECISION_WATCH):
            continue
        cid = cand["candidate_id"]
        try:
            watchlist_service.register(cand)
            watchlist_service.transition(cid, WATCH_WATCHING, reason="auto")
            summary["registered"] += 1
        except Exception as exc:
            summary["blocked"].append(f"{cid}:register:{exc}")
            continue
        if level < AUTOMATION_CONFIRM:
            continue
        try:
            watchlist_service.transition(cid, "confirmed", reason="auto-confirm")
            summary["confirmed"] += 1
        except ValueError:
            continue  # 결정적 검증 미충족 → confirmed 안 됨(정상)
        if level < AUTOMATION_PLAN:
            continue
        try:
            stop = float(cand.get("current_price") or 0) * 0.95  # 참고 손절(ATR 대체 전 기본)
            plan = execution_plan_service.create_plan(cid, options={
                "entry_price": cand.get("current_price"), "stop_price": stop,
            })
            summary["planned"] += 1
        except ValueError as exc:
            summary["blocked"].append(f"{cid}:plan:{exc}")
            continue
        # Level 5: 자동 승인 대기열 등록(주문 아님, 기존 시장별 경로 §5.9)
        approval_id = None
        if level >= AUTOMATION_APPROVE and int(policy.get("auto_approve") or 0):
            gate = evaluate_gate(policy=policy, candidate=cand, stage="approve")
            if gate["proceed"]:
                try:
                    approval_id = _queue_approval(market, cand, plan, strategy_id)
                    repo.update_execution_plan_approval(
                        int(plan["id"]),
                        approval_market=market,
                        approval_db=_approval_db(market),
                        approval_id=approval_id,
                        approval_status="pending",
                    )
                    summary["approved"] += 1
                except Exception as exc:
                    summary["blocked"].append(f"{cid}:approve:{exc}")
            else:
                summary["blocked"].append(f"{cid}:approve:{','.join(gate['blocked_reason'])}")
        # Level 6: 자동 주문 실행. 안전 게이트(live_trading_allowed 등)가 모두 열려야만 진행.
        # AI스톡은 브로커를 직접 호출하지 않고, 기존 승인 큐 + 기존 실행 인프라에 위임한다(§5.9).
        if level >= AUTOMATION_EXECUTE and int(policy.get("auto_execute") or 0):
            gate = evaluate_gate(policy=policy, candidate=cand, stage="execute")
            order_result = None
            order_ok = False
            if gate["proceed"]:
                gate = {
                    **gate,
                    "proceed": False,
                    "blocked_reason": [
                        *gate["blocked_reason"],
                        "legacy_auto_execute_disabled_use_autonomy_runtime",
                    ],
                }
            repo.update_execution_plan_status(
                int(plan["id"]),
                status="submitted" if order_ok else "blocked",
                approval_status="executed" if order_ok and approval_id is not None else None,
            )
            order_refs = _extract_order_refs(order_result)
            repo.log_execution_run({
                "strategy_id": strategy_id, "market": market, "scan_id": scan["scan_id"],
                "candidate_id": cid, "plan_id": plan.get("id"), "run_type": run_type,
                "automation_level": level,
                "status": "completed" if order_ok else "blocked",
                "blocked_stage": None if gate["proceed"] else "execute",
                "blocked_reason": None if gate["proceed"] else ",".join(gate["blocked_reason"]),
                "approval_market": market, "approval_id": approval_id,
                "order_id": order_refs["order_id"],
                "broker_order_id": order_refs["broker_order_id"],
                "policy_snapshot": {"automation_level": level, "auto_execute": 1, "order_result": order_result},
            })
            if order_ok:
                summary["executed"] = summary.get("executed", 0) + 1
            else:
                summary["blocked"].append(f"{cid}:execute:{','.join(gate['blocked_reason'])}")

    repo.log_execution_run({
        "strategy_id": strategy_id, "market": market, "scan_id": scan["scan_id"],
        "run_type": run_type, "automation_level": level, "status": "completed",
        "policy_snapshot": policy,
    })
    return {"scan": scan["summary"], "automation": summary}
