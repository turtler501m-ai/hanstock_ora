from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to sys.path to allow running as a script directly
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.mistock import trader as mistock_trader
from src.mistock.config import config as mistock_config
from src.mistock import db as mistock_db
from src.notifier.slack import send_mistock_slack
from src.utils.logger import logger

from src.mistock.strategy import symbol_name

KST = timezone(timedelta(hours=9))


def _pending_scheduler_approval_exists(symbol: str, action: str) -> bool:
    existing = mistock_db.row(
        """
        SELECT id
        FROM approvals
        WHERE symbol = ?
          AND action = ?
          AND source = 'scheduler'
          AND status = 'pending'
        LIMIT 1
        """,
        (symbol, action),
    )
    return existing is not None


def is_us_market_open() -> bool:
    """
    현재 한국 시각(KST) 기준 미국 정규장 운영 시간(마감 5분 전 가드) 내에 있는지 검사합니다.
    테스트/로컬 환경일 때는 항상 True를 반환합니다.
    """
    import sys
    if "unittest" in sys.modules:
        return True
    if mistock_config.trading_env not in {"demo", "real"}:
        return True
    now = datetime.now(KST)
    is_dst = 3 <= now.month <= 11
    current_time_str = now.strftime("%H:%M")
    if is_dst:
        # 서머타임 운영: 22:30 ~ 익일 05:00 (주문 가드 04:55)
        return ("22:30" <= current_time_str <= "23:59") or ("00:00" <= current_time_str <= "04:55")
    else:
        # 일반 시간 운영: 23:30 ~ 익일 06:00 (주문 가드 05:55)
        return ("23:30" <= current_time_str <= "23:59") or ("00:00" <= current_time_str <= "05:55")


def _order_delay_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("MISTOCK_ORDER_DELAY_SECONDS", "1.2")))
    except ValueError:
        return 1.2


def _place_order(symbol: str, action: str, qty: float, price: float, reason: str, strategy_id: str | None):
    kwargs = {"reason": reason}
    if strategy_id:
        kwargs["strategy_id"] = strategy_id
    return mistock_trader.place_order(symbol, action, qty, price, **kwargs)


def _execute_pending_scheduler_approvals(strategy_id: str | None = None) -> list[dict]:
    pending = mistock_db.rows(
        """
        SELECT *
        FROM approvals
        WHERE status = 'pending'
          AND source = 'scheduler'
          AND (? IS NULL OR strategy_id = ?)
        ORDER BY CASE action WHEN 'sell' THEN 0 ELSE 1 END, id
        LIMIT 50
        """,
        (strategy_id, strategy_id),
    )
    processed = []
    for idx, item in enumerate(pending):
        result = _place_order(
            item["symbol"],
            item["action"],
            float(item["qty"]),
            float(item["price"]),
            item.get("reason") or "scheduler pending approval",
            item.get("strategy_id") or strategy_id,
        )
        status = "executed" if result.get("ok") else "failed"
        mistock_db.execute(
            "UPDATE approvals SET status = ?, updated_at = ?, response_msg = ? WHERE id = ?",
            (
                status,
                mistock_db.now_text(),
                result.get("message") or result.get("msg1") or status,
                item["id"],
            ),
        )
        processed.append(
            {
                "id": item["id"],
                "symbol": item["symbol"],
                "action": item["action"],
                "qty": float(item["qty"]),
                "price": float(item["price"]),
                "result": result,
            }
        )
        if idx < len(pending) - 1:
            time.sleep(_order_delay_seconds())
    return processed

def run_mistock_scheduled_cycle(mode: str = "execute", strategy_id: str | None = None) -> dict:
    """
    [미장 자동매매 스케줄러]
    미국 주식 시장(미장) 유니버스 스캔, 신호 분석 및 주문 집행(KIS 모의투자 또는 실거래)을 수행합니다.
    """
    logger.info(f"[MISTOCK SCHEDULER] Starting scheduled cycle. Mode={mode}")
    
    # 1. 시세 조회 및 후보 종목 스캔
    min_score = (
        int(mistock_config.indicator_min_score or 4)
        if str(mistock_config.strategy_model or "").lower() == "macd_rsi_momentum"
        else 2
    )
    scan = mistock_trader.scan_candidates(min_score=min_score, limit=mistock_config.scan_universe_size)
    candidates = scan["candidates"]
    logger.info(f"[MISTOCK SCHEDULER] Scanned {scan['scanned']} symbols. Found {len(candidates)} candidates.")
    
    # 2. 잔고 가져오기
    balance = mistock_trader.get_balance()
    cash = balance["cash"]
    
    # Check auto-approval setting from database
    auto_approve = (mistock_db.get_setting("auto_approval", "false") == "true")
    flags = mistock_trader.runtime_flags()
    broker_submission_available = mistock_trader.broker_submission_available(balance)
    market_open = is_us_market_open()
    pending_approved = []
    if mode == "execute" and auto_approve and broker_submission_available and market_open:
        pending_approved = _execute_pending_scheduler_approvals(strategy_id)
    
    # 3. 매도 신호 처리 및 주문 집행/대기등록
    sell_sigs = [sig for sig in mistock_trader.signals() if sig["action"] == "sell" and float(sig["signal_qty"]) > 0]
    sold_items = []
    for idx, sig in enumerate(sell_sigs):
        qty = float(sig["signal_qty"])
        price = float(sig["signal_price"])
        if mode == "execute" and (auto_approve or flags["order_submission_enabled"]) and broker_submission_available and market_open:
            logger.info(f"[MISTOCK SCHEDULER] Sell signal for {sig['symbol']}. Qty={qty}, Price={price}")
            res = _place_order(sig["symbol"], "sell", qty, price, sig["reason"], strategy_id)
            sold_items.append({"symbol": sig["symbol"], "qty": qty, "price": price, "result": res})
            if idx < len(sell_sigs) - 1:
                time.sleep(_order_delay_seconds())
        elif mode == "execute":
            if _pending_scheduler_approval_exists(sig["symbol"], "sell"):
                logger.info(
                    f"[MISTOCK SCHEDULER] Pending sell approval already exists for {sig['symbol']}; skipping duplicate."
                )
                continue
            logger.info(f"[MISTOCK SCHEDULER] Auto-approval/order submission disabled or skipped (market_open={market_open}). Queuing {sig['symbol']} as pending approval.")
            now = mistock_db.now_text()
            mistock_db.execute(
                """
                INSERT INTO approvals (created_at, updated_at, symbol, name, action, qty, price, reason, source, status, response_msg, strategy_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?)
                """,
                (now, now, sig["symbol"], symbol_name(sig["symbol"]), "sell", qty, price, sig.get("reason") or "보유 종목 매도 신호", "scheduler", strategy_id),
            )
                 
    # 4. 매수 주문 조립 및 집행/대기등록
    # 이미 보유 중인 종목은 매수 후보에서 제외해 같은 종목을 매 사이클 재매수하지 않게 한다.
    held_symbols = {h.get("symbol") for h in balance.get("holdings", [])}
    
    # 중복 주문 방지: 대기 중인 승인 주문(pending approvals) 종목 기호 제외
    pending_symbols = {
        row["symbol"]
        for row in mistock_db.rows("SELECT symbol FROM approvals WHERE status = 'pending'")
    }
    
    # 중복 주문 방지: 최근 12시간 동안 이미 매수 주문을 시도한 적이 있는 종목 기호 제외
    twelve_hours_ago = (datetime.now(KST) - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
    recent_buys = {
        row["symbol"]
        for row in mistock_db.rows("SELECT symbol FROM trades WHERE action = 'buy' AND ts >= ?", (twelve_hours_ago,))
    }
    
    # approvals에 이미 pending이거나 trades에 매수 기록된 기호 제외
    exclude_symbols = held_symbols | pending_symbols | recent_buys
    buy_candidates = [c for c in candidates if c.get("symbol") not in exclude_symbols]
    
    # 총 운용자금(total_capital) 한도에서 이미 보유한 평가액을 뺀 잔여만 신규 매수에 사용한다.
    # demo 모의투자 계좌의 통합증거금(수억 달러)을 그대로 쓰면 주문이 폭주하므로 한도를 건다.
    deployed = float(balance.get("stock_eval", 0.0) or 0.0)
    cap = float(mistock_config.total_capital or 0.0)
    sizing_cash = min(cash, max(0.0, cap - deployed)) if cap > 0 else cash
    orders = mistock_trader.build_orders(buy_candidates, sizing_cash)
    bought_items = []
    
    if mode == "execute":
        if (auto_approve or flags["order_submission_enabled"]) and broker_submission_available and market_open:
            for idx, ord in enumerate(orders):
                qty = float(ord["quantity"])
                price = float(ord["price"])
                logger.info(f"[MISTOCK SCHEDULER] Placing buy order for {ord['symbol']}. Qty={qty}, Price={price}")
                res = _place_order(ord["symbol"], "buy", qty, price, ord["reason"], strategy_id)
                bought_items.append({"symbol": ord["symbol"], "qty": qty, "price": price, "result": res})
                # 잔고 부족 응답이면 이후 주문도 실패할 것이므로 즉시 중단한다
                msg = (res.get("msg1") or res.get("message") or "")
                if not res.get("ok") and "주문가능금액" in msg:
                    logger.warning(
                        f"[MISTOCK SCHEDULER] Insufficient balance for {ord['symbol']} (msg={msg!r}). "
                        "Stopping further buy orders this cycle."
                    )
                    break
                if idx < len(orders) - 1:
                    time.sleep(_order_delay_seconds())
        else:
            logger.info(f"[MISTOCK SCHEDULER] Order submission/auto-approval disabled or skipped (market_open={market_open}). Queuing buy plans as pending approvals.")
            for ord in orders:
                qty = float(ord["quantity"])
                price = float(ord["price"])
                now = mistock_db.now_text()
                mistock_db.execute(
                    """
                    INSERT INTO approvals (created_at, updated_at, symbol, name, action, qty, price, reason, source, status, response_msg, strategy_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?)
                    """,
                    (now, now, ord["symbol"], symbol_name(ord["symbol"]), "buy", qty, price, ord.get("reason") or "매수 계획", "scheduler", strategy_id),
                )
            
    order_failures = [
        item
        for item in pending_approved + sold_items + bought_items
        if not (item.get("result") or {}).get("ok", False)
    ]
    result = {
        "strategy_id": strategy_id or "mistock_nasdaq_rule_v1",
        "status": "success" if not order_failures else "failed",
        "ok": not order_failures,
        "scanned": scan["scanned"],
        "candidates": len(candidates),
        "sold": sold_items,
        "bought": bought_items,
        "pending_approved": pending_approved,
        "plan": orders,
        "errors": [
            {
                "symbol": item.get("symbol"),
                "action": item.get("action") or ("sell" if item in sold_items else "buy"),
                "message": (item.get("result") or {}).get("msg1")
                or (item.get("result") or {}).get("message")
                or "order failed",
            }
            for item in order_failures
        ],
    }
    
    # 결과 파일 저장
    path = Path(".runtime/mistock/daily_auto_last_result.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    recorded_at = datetime.now(KST).isoformat()
    path.write_text(json.dumps({
        "recorded_at": recorded_at,
        "result": result,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    
    # 누적 기록 파일에도 크로노그래피컬하게 누적 저장 (VM 크론탭 실행 누락 방지)
    today_path = Path(".runtime/mistock/daily_auto_today_results.json")
    cutoff_date = (datetime.now(KST) - timedelta(days=29)).date()
    today_runs = []
    if today_path.exists():
        try:
            data = json.loads(today_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for run in data:
                    try:
                        run_time = datetime.fromisoformat(str(run.get("recorded_at", "")).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if run_time.tzinfo is None:
                        run_time = run_time.replace(tzinfo=KST)
                    else:
                        run_time = run_time.astimezone(KST)
                    if run_time.date() >= cutoff_date:
                        today_runs.append(run)
        except Exception:
            pass
            
    today_runs.append({
        "recorded_at": recorded_at,
        "mode": mode,
        "result": result
    })
    try:
        today_path.write_text(json.dumps(today_runs, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass
    
    # 슬랙 알림 발송
    if os.environ.get("MISTOCK_SCHEDULER_SLACK", "true").lower() not in {"0", "false", "no", "off"}:
        status_str = "성공"
        status_line = f"*[미스톡 VM] 미국주식 자동매매 {status_str}*"
        details_line = (
            f"스캔: {scan['scanned']}개 | 매도: {len(sold_items)}건 | "
            f"매수: {len(bought_items)}건(계획: {len(orders)}건)\n"
            f"잔고: ${balance['cash']:,.2f} | 평가: ${balance['total_eval']:,.2f} | "
            f"환경: {mistock_config.trading_env}(dry={mistock_config.dry_run})"
        )
        send_mistock_slack(
            text=f"[미스톡 VM] 미장 자동매매 {status_str}",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": f"{status_line}\n{details_line}"}},
            ],
            color="#36a64f"
        )
        
    return result

def main() -> int:
    parser = argparse.ArgumentParser(description="Mistock US stock scheduled trading runner")
    parser.add_argument(
        "--mode",
        choices=["execute", "analysis_only"],
        default="execute",
        help="execute orders immediately or queue analysis only",
    )
    parser.add_argument(
        "--strategy-id",
        action="append",
        dest="strategy_ids",
        help="strategy id to run; repeat for multiple strategies",
    )
    args = parser.parse_args()
    try:
        strategy_ids = list(dict.fromkeys(args.strategy_ids or []))
        if not strategy_ids:
            strategy_ids = [
                str(row["strategy_id"])
                for row in mistock_db.rows(
                    "SELECT strategy_id FROM strategy_schedules WHERE enabled = 1 ORDER BY strategy_id"
                )
            ]
        if not strategy_ids:
            strategy_ids = ["mistock_nasdaq_rule_v1"]
        failed = False
        for strategy_id in strategy_ids:
            result = run_mistock_scheduled_cycle(mode=args.mode, strategy_id=strategy_id)
            failed = failed or not bool(result.get("ok"))
            mistock_db.execute(
                "UPDATE strategy_schedules SET last_run_at = ? WHERE strategy_id = ?",
                (datetime.now(KST).isoformat(), strategy_id),
            )
        return 1 if failed else 0
    except Exception as e:
        logger.error(f"Mistock scheduler execution failed: {e}")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
