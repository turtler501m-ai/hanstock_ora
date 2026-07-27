"""Safe process entrypoint for the continuous autonomous strategy service."""
from __future__ import annotations

import importlib
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.config import config
from src.strategy.autonomy.continuous_service import ContinuousStrategyService
from src.utils.processes import process_exists


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProcessLease:
    """Small single-host PID lease; a live owner can never be displaced."""

    def __init__(self, path: Path):
        self.path = path
        self.owned = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                try:
                    owner = int(self.path.read_text(encoding="utf-8").strip())
                    if process_exists(owner):
                        return False
                    raise ProcessLookupError(owner)
                except PermissionError:
                    return False
                except (ValueError, ProcessLookupError):
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(str(os.getpid()))
                self.owned = True
                return True
        return False

    def release(self) -> None:
        if self.owned:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.owned = False


def _write_heartbeat(path: Path, *, state: str, detail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "state": state,
        "detail": detail,
        "updated_at": _utc_now(),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _load_factory(spec: str) -> Callable[[], ContinuousStrategyService]:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("factory must use module.path:function format")
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise TypeError("configured autonomy service factory is not callable")
    return factory


def run(
    *,
    environ: dict[str, str] | None = None,
    sleeper: Callable[[float], Any] = time.sleep,
) -> int:
    env = os.environ if environ is None else environ
    runtime = Path(env.get("AUTONOMY_RUNTIME_DIR", ".runtime/autonomy"))
    lease = ProcessLease(runtime / "service.lock")
    heartbeat = runtime / "heartbeat.json"
    if not lease.acquire():
        _write_heartbeat(heartbeat, state="duplicate_rejected")
        return 3

    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, request_stop)
    try:
        enabled = bool(getattr(config, "autonomy_enabled", False))
        poll_seconds = max(1.0, float(env.get("AUTONOMY_HEARTBEAT_SECONDS", "30")))
        if not enabled:
            while not stop_event.is_set():
                _write_heartbeat(
                    heartbeat,
                    state="disabled",
                    detail="AUTONOMY_ENABLED is false",
                )
                stop_event.wait(poll_seconds)
            return 0

        factory_spec = env.get("AUTONOMY_SERVICE_FACTORY", "").strip()
        if not factory_spec:
            _write_heartbeat(
                heartbeat,
                state="configuration_error",
                detail="AUTONOMY_SERVICE_FACTORY is required when enabled",
            )
            return 2
        service = _load_factory(factory_spec)()
        if not isinstance(service, ContinuousStrategyService):
            raise TypeError("factory must return ContinuousStrategyService")

        def monitor() -> None:
            while not stop_event.is_set():
                _write_heartbeat(heartbeat, state="running")
                stop_event.wait(poll_seconds)

        service.stop_event = stop_event
        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        _write_heartbeat(heartbeat, state="starting")
        service.run_forever()
        _write_heartbeat(heartbeat, state="stopped")
        return 0
    except Exception as exc:
        _write_heartbeat(
            heartbeat,
            state="failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return 1
    finally:
        lease.release()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
