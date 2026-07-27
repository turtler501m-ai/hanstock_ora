from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from src.repositories import ApprovalRecord, ApprovalRepository


class ApprovalError(Exception):
    pass


class ApprovalNotFoundError(ApprovalError):
    pass


class ApprovalStatusError(ApprovalError):
    pass


@dataclass(frozen=True)
class ApprovalCreateRequest:
    symbol: str
    name: str
    action: str
    qty: int
    price: float
    reason: str = ""
    source: str = ""
    strategy_id: str = ""
    strategy_version: int | None = None
    profile_hash: str = ""
    source_candidate_id: int | None = None
    managed_order_id: int | None = None
    decision_id: int | None = None
    position_id: int | None = None
    client_order_key: str = ""


def _default_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ApprovalService:
    def __init__(
        self,
        repository: ApprovalRepository,
        *,
        now_fn: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._now_fn = now_fn or _default_now

    def init_db(self) -> None:
        self._repository.init_db()

    def create_approval(self, request: ApprovalCreateRequest) -> int:
        self.init_db()
        now = self._now_fn()
        return self._repository.create_approval(
            created_at=now,
            updated_at=now,
            symbol=request.symbol,
            name=request.name,
            action=request.action,
            qty=request.qty,
            price=request.price,
            reason=request.reason,
            source=request.source,
            strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            profile_hash=request.profile_hash,
            source_candidate_id=request.source_candidate_id,
            managed_order_id=request.managed_order_id,
            decision_id=request.decision_id,
            position_id=request.position_id,
            client_order_key=request.client_order_key,
        )

    def queue_approval(
        self,
        symbol: str,
        name: str,
        action: str,
        qty: int,
        price: float,
        reason: str,
        source: str = "scheduler",
        strategy_id: str = "",
        strategy_version: int | None = None,
        profile_hash: str = "",
        source_candidate_id: int | None = None,
        managed_order_id: int | None = None,
        decision_id: int | None = None,
        position_id: int | None = None,
        client_order_key: str = "",
    ) -> int:
        return self.create_approval(
            ApprovalCreateRequest(
                symbol=symbol,
                name=name,
                action=action,
                qty=qty,
                price=price,
                reason=reason,
                source=source,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                profile_hash=profile_hash,
                source_candidate_id=source_candidate_id,
                managed_order_id=managed_order_id,
                decision_id=decision_id,
                position_id=position_id,
                client_order_key=client_order_key,
            )
        )

    def get_approval(self, approval_id: int) -> ApprovalRecord:
        self.init_db()
        approval = self._repository.get_approval(approval_id)
        if approval is None:
            raise ApprovalNotFoundError("approval not found")
        return approval

    def get_pending_approval(self, approval_id: int) -> ApprovalRecord:
        approval = self.get_approval(approval_id)
        if approval.status != "pending":
            raise ApprovalStatusError(f"approval is already {approval.status}")
        return approval

    def list_approvals(self, *, limit: int = 50) -> list[ApprovalRecord]:
        if limit < 1:
            raise ValueError("limit must be greater than 0")
        self.init_db()
        return self._repository.list_approvals(limit=min(limit, 200))

    def update_status(self, approval_id: int, *, status: str, response_msg: str) -> ApprovalRecord:
        self.get_approval(approval_id)
        updated = self._repository.update_approval_status(
            approval_id,
            status=status,
            response_msg=response_msg,
            updated_at=self._now_fn(),
        )
        if not updated:
            raise ApprovalNotFoundError("approval not found")
        return self.get_approval(approval_id)

    def reject_approval(
        self,
        approval_id: int,
        *,
        response_msg: str = "Rejected by dashboard",
    ) -> ApprovalRecord:
        return self.transition_pending(
            approval_id,
            status="rejected",
            response_msg=response_msg,
        )

    def transition_pending(
        self, approval_id: int, *, status: str, response_msg: str
    ) -> ApprovalRecord:
        self.get_pending_approval(approval_id)
        changed = self._repository.transition_approval_status(
            approval_id,
            expected_status="pending",
            status=status,
            response_msg=response_msg,
            updated_at=self._now_fn(),
        )
        if not changed:
            raise ApprovalStatusError("approval status changed concurrently")
        return self.get_approval(approval_id)
