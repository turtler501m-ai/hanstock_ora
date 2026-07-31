"""Run unittest profiles in visible, timeout-bounded batches."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


PROFILES = {
    "quick": [
        "tests.test_execution_policy",
        "tests.test_dashboard_auth",
        "tests.test_db_migration",
    ],
    "dashboard": [
        "tests.test_dashboard_core",
        "tests.test_dashboard_auth",
        "tests.test_dashboard_execution_plan",
        "tests.test_dashboard_helpers",
        "tests.test_dashboard_periodic_performance",
        "tests.test_dashboard_plan_views",
        "tests.test_dashboard_signal_candidate_alignment",
        "tests.test_runtime_dashboard_alignment",
        "tests.test_scheduler_api",
    ],
    "trading": [
        "tests.test_trader_core",
        "tests.test_trader_kis_integration",
        "tests.test_runtime_plan",
        "tests.test_order_router",
        "tests.test_execution_policy",
        "tests.test_kis_api",
        "tests.test_kis_client",
    ],
    "ai": [
        "tests.test_ai_stock_core",
        "tests.test_ai_stock_api",
        "tests.test_ai_stock_automation",
        "tests.test_ai_strategy_lifecycle",
        "tests.test_ai_strategy_presets",
        "tests.test_autonomy_ai_stock_integration",
    ],
    "futures": [
        "tests.test_futures_signal_parser",
        "tests.test_futures_signal_verifier",
        "tests.test_futures_signals_dashboard",
        "tests.test_kis_futures_api",
        "tests.test_quantconnect_api",
        "tests.test_quantconnect_mnq_algorithm",
    ],
    "mistock": [
        "tests.test_mistock_dashboard",
        "tests.test_mistock_indicator_strategy",
        "tests.test_mistock_monitor",
    ],
}


def all_test_modules() -> list[str]:
    return [
        ".".join(path.with_suffix("").parts)
        for path in sorted(Path("tests").glob("test_*.py"))
    ]


def batches(items: list[str], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=[*PROFILES, "autonomy", "all"], default="quick")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    modules = all_test_modules()
    if args.profile in PROFILES:
        modules = PROFILES[args.profile]
    elif args.profile == "autonomy":
        modules = [
            module
            for module in modules
            if module.rsplit(".", 1)[-1].startswith("test_autonomy_")
        ]

    env = os.environ.copy()
    env.update(
        {
            "HANSTOCK_TESTING": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HANSTOCK_LOG_LEVEL": "DEBUG" if args.verbose else "WARNING",
        }
    )
    started = time.monotonic()
    groups = list(batches(modules, max(1, args.batch_size)))
    for number, group in enumerate(groups, start=1):
        label = f"[{args.profile} {number}/{len(groups)}]"
        print(f"{label} running {len(group)} modules", flush=True)
        batch_started = time.monotonic()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    *(["-v"] if args.verbose else []),
                    *group,
                ],
                env=env,
                timeout=args.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"{label} timed out after {args.timeout}s", file=sys.stderr, flush=True)
            return 124
        elapsed = time.monotonic() - batch_started
        print(f"{label} finished in {elapsed:.1f}s", flush=True)
        if result.returncode:
            return result.returncode

    print(
        f"{args.profile}: {len(modules)} modules passed in "
        f"{time.monotonic() - started:.1f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
