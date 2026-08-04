import os
import time

from src.api.kis_api import KIStockAPI
from src.approval_service import ApprovalService
from src.config import config
from src.db.repository import connect_db, save_decision_log, save_trade
from src.execution_service import ExecutionContext, resolve_execution_decision
from src.repositories import ApprovalRepository
from src.utils.logger import logger

_RATE_LIMIT_BACKOFF_SECONDS = float(os.environ.get("KIS_ORDER_RATE_LIMIT_BACKOFF_SECONDS", "10.0"))
_RATE_LIMIT_MAX_RETRIES = int(os.environ.get("KIS_ORDER_RATE_LIMIT_RETRIES", "2"))
_ORDER_MIN_INTERVAL_SECONDS = float(os.environ.get("KIS_ORDER_MIN_INTERVAL_SECONDS", "0.0"))


def _is_kis_rate_limit_message(message: str) -> bool:
    text = str(message or "").lower()
    return "\ucd08\ub2f9 \uac70\ub798\uac74\uc218" in text or "rate limit" in text or "egw00201" in text


class OrderRouter:
    def __init__(
        self,
        api: KIStockAPI,
        approval_service: ApprovalService | None = None,
        execution_context=None,
    ):
        self.api = api
        source = execution_context or config
        self.dry_run = source.dry_run
        self.env = getattr(source, "trading_env")
        self.enable_live = source.enable_live_trading
        self.require_approval = source.require_approval
        self.online_access_blocked = bool(getattr(source, "online_access_blocked", False))
        self.approval_service = approval_service or ApprovalService(ApprovalRepository(connect_db))
        self._last_order_at = 0.0

    def _execution_context(self) -> ExecutionContext:
        return ExecutionContext(
            dry_run=self.dry_run,
            trading_env=self.env,
            enable_live_trading=self.enable_live,
            require_approval=self.require_approval,
            online_access_blocked=self.online_access_blocked,
        )

    def _current_holding_qty(self, symbol: str) -> int:
        try:
            balance = self.api.get_balance()
        except Exception:
            return 0
        for holding in balance.get("output1", []) or []:
            if str(holding.get("pdno") or "") == str(symbol):
                try:
                    return int(float(holding.get("hldg_qty") or 0))
                except (TypeError, ValueError):
                    return 0
        return 0

    def _place_order_with_rate_limit_retries(
        self,
        symbol: str,
        action: str,
        price: int,
        qty: int,
    ) -> dict:
        attempts = max(1, _RATE_LIMIT_MAX_RETRIES + 1)
        result: dict = {}
        for attempt in range(1, attempts + 1):
            if _ORDER_MIN_INTERVAL_SECONDS > 0 and self._last_order_at > 0:
                elapsed = time.monotonic() - self._last_order_at
                wait = _ORDER_MIN_INTERVAL_SECONDS - elapsed
                if wait > 0:
                    time.sleep(wait)
            result = self.api.place_order(symbol, action, price, qty)
            self._last_order_at = time.monotonic()
            ok = result.get("rt_cd") == "0"
            msg = str(result.get("msg1", ""))
            logger.info(f"[ROUTER] Live Execution {'OK' if ok else 'FAILED'}: {msg}")
            if ok or not _is_kis_rate_limit_message(msg) or attempt >= attempts:
                return result
            logger.warning(
                "[ROUTER] KIS rate limit response detected; "
                f"retrying after {_RATE_LIMIT_BACKOFF_SECONDS:.1f}s "
                f"({attempt}/{attempts - 1})"
            )
            if _RATE_LIMIT_BACKOFF_SECONDS > 0:
                time.sleep(_RATE_LIMIT_BACKOFF_SECONDS)
        return result

    def route(
        self,
        symbol: str,
        name: str,
        action: str,
        qty: int,
        price: int,
        reason: str,
        indicators: dict,
        strategy_id: str = None,
    ) -> dict:
        save_decision_log(symbol, name, action, qty, price, reason, indicators, True)

        decision = resolve_execution_decision(self._execution_context())
        if decision.decision == "reject":
            logger.warning(f"[ROUTER] Order Rejected: {decision.reason}")
            return {"ok": False, "msg": decision.reason, "status": "rejected"}

        if self.dry_run:
            logger.info(f"[ROUTER] Paper Trading: {action} {name} qty={qty}")
            save_trade(symbol, name, action, qty, price, reason, True, False, strategy_id=strategy_id)
            return {"ok": True, "msg": "Paper trading executed", "status": "paper"}

        if decision.decision == "queue":
            approval_id = self._insert_approval(
                symbol,
                name,
                action,
                qty,
                price,
                reason,
                strategy_id=strategy_id,
            )
            if approval_id is None:
                return {"ok": False, "msg": "Approval queue unavailable", "status": "failed"}
            logger.info(f"[ROUTER] Pending Approval: {action} {name} qty={qty}")
            return {
                "ok": True,
                "msg": "Added to approval queue",
                "status": "pending",
                "approval_id": approval_id,
            }

        pre_order_qty = self._current_holding_qty(symbol) if action == "sell" else 0
        result = self._place_order_with_rate_limit_retries(symbol, action, price, qty)
        ok = result.get("rt_cd") == "0"
        save_trade(
            symbol,
            name,
            action,
            qty,
            price,
            reason,
            ok,
            True,
            broker_result=result,
            order_status="submitted" if ok else "failed",
            response_msg=str(result.get("msg1", "")),
            filled_qty=0,
            filled_price=0,
            pre_order_qty=pre_order_qty,
            strategy_id=strategy_id,
        )
        return {"ok": ok, "msg": result.get("msg1", ""), "status": "live"}

    def _insert_approval(
        self,
        symbol: str,
        name: str,
        action: str,
        qty: int,
        price: int,
        reason: str,
        strategy_id: str = None,
    ) -> int | None:
        try:
            return self.approval_service.queue_approval(
                symbol,
                name,
                action,
                qty,
                price,
                reason,
                source="auto_trader",
                strategy_id=strategy_id or "",
            )
        except Exception as exc:
            logger.error(f"[ROUTER] Failed to insert approval: {exc}")
            return None
