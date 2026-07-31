"""Process entrypoint for continuous read-only condition monitoring."""

from __future__ import annotations

import os
import signal
import threading

from src.strategy.condition_monitor import run_forever


def main() -> None:
    stop_event = threading.Event()

    def stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    run_forever(
        interval_seconds=float(os.environ.get("CONDITION_MONITOR_INTERVAL_SECONDS", "60")),
        stop_requested=stop_event.is_set,
    )


if __name__ == "__main__":
    main()
