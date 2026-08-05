from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import datetime
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
    "run_id": None,
    "max_runtime_seconds": None,
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
        self.state.refresh()
        if self.state.get("is_running") and self.state.get("max_runtime_seconds"):
            try:
                started_at = datetime.fromisoformat(str(self.state.get("started_at")))
                now = datetime.fromisoformat(self.now_fn())
                elapsed = (now - started_at).total_seconds()
            except (TypeError, ValueError):
                elapsed = 0
            if elapsed > int(self.state["max_runtime_seconds"]):
                self.fail(
                    TimeoutError("scheduler run exceeded its maximum runtime"),
                    run_id=str(self.state.get("run_id") or ""),
                )
        return dict(self.state)

    def claim(
        self,
        *,
        mode: str,
        strategy_id: str | None,
        run_id: str | None = None,
        max_runtime_seconds: int | None = None,
    ) -> bool:
        payload = {
            **DEFAULT_SCHEDULER_STATE,
            "is_running": True,
            "mode": mode,
            "strategy_id": strategy_id,
            "started_at": self.now_fn(),
            "owner_pid": os.getpid(),
            "run_id": run_id,
            "max_runtime_seconds": max_runtime_seconds,
        }
        with self.lock:
            return self.state.claim(payload)

    def complete(self, result: dict, *, run_id: str | None = None) -> None:
        with self.lock:
            if run_id:
                self.state.refresh()
                if str(self.state.get("run_id") or "") != run_id:
                    return
                if not self.state.get("is_running"):
                    return
            self.state.replace({
                **self.state,
                "is_running": False,
                "completed_at": self.now_fn(),
                "result": result,
                "error": None,
                "owner_pid": None,
            })

    def fail(self, exc: Exception, *, run_id: str | None = None) -> None:
        with self.lock:
            if run_id:
                self.state.refresh()
                if str(self.state.get("run_id") or "") != run_id:
                    return
                if not self.state.get("is_running"):
                    return
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
        run_id: str | None = None,
        **extra_kwargs: Any,
    ) -> None:
        try:
            kwargs = {
                "mode": mode,
                "include_ai_rebalance": include_ai_rebalance,
                "auto_approve": auto_approve,
            }
            if strategy_id is not None:
                kwargs["force_strategy_id"] = strategy_id
            if allowed_categories is not None:
                kwargs["allowed_categories"] = allowed_categories
            kwargs.update(extra_kwargs)
            result = runner(**kwargs)
        except SchedulerExecutionError as exc:
            self.fail(exc, run_id=run_id)
            return
        self.complete(result, run_id=run_id)
