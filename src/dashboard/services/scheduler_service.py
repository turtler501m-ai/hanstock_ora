from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

from src.runtime_state import PersistentRuntimeState


DEFAULT_SCHEDULER_STATE = {
    "is_running": False,
    "mode": None,
    "strategy_id": None,
    "started_at": None,
    "completed_at": None,
    "result": None,
    "error": None,
    "owner_pid": None,
}

SchedulerExecutionError = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
)


class DashboardSchedulerService:
    def __init__(
        self,
        state_key: str,
        *,
        now_fn: Callable[[], str],
    ) -> None:
        self.state = PersistentRuntimeState(state_key, DEFAULT_SCHEDULER_STATE)
        self.lock = threading.Lock()
        self.now_fn = now_fn

    def refresh(self) -> dict[str, Any]:
        return dict(self.state.refresh())

    def claim(self, *, mode: str, strategy_id: str | None) -> bool:
        payload = {
            **DEFAULT_SCHEDULER_STATE,
            "is_running": True,
            "mode": mode,
            "strategy_id": strategy_id,
            "started_at": self.now_fn(),
            "owner_pid": os.getpid(),
        }
        with self.lock:
            return self.state.claim(payload)

    def complete(self, result: dict) -> None:
        with self.lock:
            self.state.replace({
                **self.state,
                "is_running": False,
                "completed_at": self.now_fn(),
                "result": result,
                "error": None,
                "owner_pid": None,
            })

    def fail(self, exc: Exception) -> None:
        with self.lock:
            self.state.replace({
                **self.state,
                "is_running": False,
                "completed_at": self.now_fn(),
                "result": None,
                "error": str(exc),
                "owner_pid": None,
            })

    def run(
        self,
        runner: Callable[..., dict],
        *,
        mode: str,
        include_ai_rebalance: bool,
        auto_approve: bool,
        strategy_id: str | None = None,
        allowed_categories: set[str] | None = None,
        **extra_kwargs: Any,
    ) -> None:
        try:
            kwargs = {
                "mode": mode,
                "include_ai_rebalance": include_ai_rebalance,
                "auto_approve": auto_approve,
                "force_strategy_id": strategy_id,
            }
            if allowed_categories is not None:
                kwargs["allowed_categories"] = allowed_categories
            kwargs.update(extra_kwargs)
            result = runner(**kwargs)
        except SchedulerExecutionError as exc:
            self.fail(exc)
            return
        self.complete(result)
