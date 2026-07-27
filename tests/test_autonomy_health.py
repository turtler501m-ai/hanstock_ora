import unittest
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.db import strategy_repository
from src.strategy.autonomy.health import (
    StrategyHealthPolicy,
    StrategyHealthService,
)


class Source:
    def __init__(self, **values):
        self.values = {
            "calls": 10,
            "errors": 0,
            "fallback_count": 0,
            "state_mismatches": 0,
            "broker_unknown_count": 0,
            "unprotected_count": 0,
            "filled_order_count": 5,
            "realized_pnl": 20_000,
            "realized_drawdown": 10_000,
            **values,
        }

    def aggregate(self, strategy_id):
        return dict(self.values)


class Lifecycle:
    def __init__(self):
        self.calls = []

    def halt(self, strategy_id, **kwargs):
        self.calls.append((strategy_id, kwargs))
        return {
            "strategy_id": strategy_id,
            "new_status": kwargs["target_status"],
            "selected": False,
        }


class StrategyHealthServiceTest(unittest.TestCase):
    def evaluate(self, source, policy=None):
        lifecycle = Lifecycle()
        report = StrategyHealthService(
            source=source,
            lifecycle=lifecycle,
            policy=policy or StrategyHealthPolicy(),
        ).evaluate_and_enforce("alpha")
        return report, lifecycle

    def test_healthy_strategy_is_not_changed(self):
        report, lifecycle = self.evaluate(Source())
        self.assertFalse(report.halt_required)
        self.assertIsNone(report.target_status)
        self.assertEqual(lifecycle.calls, [])

    def test_error_and_fallback_rates_require_review(self):
        report, lifecycle = self.evaluate(
            Source(errors=3, fallback_count=7)
        )
        self.assertTrue(report.halt_required)
        self.assertEqual(report.target_status, "review_required")
        self.assertIn("error_rate_limit", report.reasons)
        self.assertIn("fallback_rate_limit", report.reasons)
        self.assertEqual(lifecycle.calls[0][1]["target_status"], "review_required")

    def test_state_broker_or_protection_mismatch_suspends_immediately(self):
        for field in (
            "state_mismatches",
            "broker_unknown_count",
            "unprotected_count",
        ):
            with self.subTest(field=field):
                report, lifecycle = self.evaluate(Source(**{field: 1}))
                self.assertEqual(report.target_status, "suspended")
                self.assertFalse(report.transition["selected"])
                self.assertEqual(
                    lifecycle.calls[0][1]["target_status"], "suspended"
                )

    def test_realized_drawdown_or_loss_requires_review(self):
        policy = StrategyHealthPolicy(
            max_realized_drawdown=50_000,
            max_realized_loss=50_000,
        )
        report, _ = self.evaluate(
            Source(realized_pnl=-60_000, realized_drawdown=70_000),
            policy,
        )
        self.assertEqual(report.target_status, "review_required")
        self.assertIn("realized_drawdown_limit", report.reasons)
        self.assertIn("realized_loss_limit", report.reasons)

    def test_report_maps_to_action_aware_lifecycle_health(self):
        report, _ = self.evaluate(Source(unprotected_count=1))
        health = report.lifecycle_health()
        self.assertTrue(health.halt_required)
        self.assertEqual(health.unprotected_count, 1)


class AtomicStrategyHaltTest(unittest.TestCase):
    def test_status_deselect_and_event_commit_together(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sqlite"

            def connect():
                return closing(sqlite3.connect(path))

            with connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE ai_strategies (
                        id TEXT PRIMARY KEY, status TEXT, selected INTEGER,
                        strategy_version INTEGER
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE ai_strategy_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
                        strategy_id TEXT, strategy_version INTEGER,
                        event_type TEXT, payload TEXT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO ai_strategies VALUES ('alpha','approved',1,3)"
                )
                conn.commit()
            with (
                patch.object(strategy_repository, "init_db"),
                patch.object(strategy_repository, "connect_db", side_effect=connect),
            ):
                result = strategy_repository.halt_ai_strategy(
                    "alpha",
                    target_status="suspended",
                    reason="unprotected",
                    payload={"count": 1},
                )
            with connect() as conn:
                strategy = conn.execute(
                    "SELECT status, selected FROM ai_strategies WHERE id='alpha'"
                ).fetchone()
                event = conn.execute(
                    "SELECT event_type FROM ai_strategy_events"
                ).fetchone()
            self.assertEqual(strategy, ("suspended", 0))
            self.assertEqual(event[0], "autonomy_health_halt")
            self.assertEqual(result["new_status"], "suspended")


if __name__ == "__main__":
    unittest.main()
