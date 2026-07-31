import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import src.dashboard as dashboard
from src.db import repository


class AiStrategyLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = dashboard.trader.config.trade_db_path
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        dashboard.trader.config.trade_db_path = str(
            Path(self.temp_dir.name) / "trades.sqlite"
        )
        self.backup_patch = patch.object(
            repository,
            "AI_STRATEGIES_FILE",
            Path(self.temp_dir.name) / "ai_strategies.json",
        )
        self.backup_patch.start()
        repository.init_db()

    def tearDown(self):
        self.backup_patch.stop()
        dashboard.trader.config.trade_db_path = self.original_db_path
        self.temp_dir.cleanup()

    def _create(self, name: str, *, selected: bool = False) -> dict:
        return repository.create_ai_strategy_record(
            {
                "id": name.lower().replace(" ", "_"),
                "name": name,
                "provider": "none",
                "model": "none",
                "weight": 0.0,
                "selected": selected,
                "profile": {"model": "none", "ai_weight": 0.0},
            }
        )

    def test_created_strategy_is_immediately_usable_for_demo_trading(self):
        strategy = self._create("Demo Strategy", selected=True)

        self.assertEqual(strategy["status"], "approved")
        context = dashboard.get_strategy_context()
        self.assertTrue(context["active_strategy"]["approval_gate"]["ok"])
        self.assertTrue(context["active_strategy"]["operation_status"]["ready"])

    def test_selecting_multiple_strategies_is_supported(self):
        self._create("First", selected=True)
        second = self._create("Second")

        dashboard.select_ai_strategy(
            second["id"],
            dashboard.SelectStrategyPayload(selected=True),
        )

        selected = [
            item["id"]
            for item in repository.load_ai_strategies()
            if item.get("selected")
        ]
        self.assertEqual(set(selected), {"first", second["id"]})

        dashboard.select_ai_strategy(
            second["id"],
            dashboard.SelectStrategyPayload(selected=False),
        )
        selected = [
            item["id"]
            for item in repository.load_ai_strategies()
            if item.get("selected")
        ]
        self.assertEqual(selected, ["first"])

    def test_batch_selection_replaces_only_mutable_strategy_scope(self):
        first = self._create("First", selected=True)
        second = self._create("Second")
        independent = self._create("Independent", selected=True)

        repository.replace_ai_strategy_selection(
            [second["id"]],
            mutable_strategy_ids=[first["id"], second["id"]],
        )

        selected = {
            item["id"]
            for item in repository.load_ai_strategies()
            if item.get("selected")
        }
        self.assertEqual(selected, {second["id"], independent["id"]})

    def test_duplicate_strategy_name_is_rejected(self):
        self._create("Unique Name")

        with self.assertRaises(ValueError):
            self._create("unique name")

    def test_update_increments_version_without_validation_state(self):
        strategy = self._create("Editable")

        updated = repository.update_ai_strategy_record(
            strategy["id"],
            {"description": "changed", "weight": 0.4},
        )

        self.assertEqual(updated["strategy_version"], 2)
        self.assertEqual(updated["status"], "approved")
        self.assertIsNone(updated["last_validation_result"])

    def test_delete_preserves_other_strategy_and_removes_target(self):
        first = self._create("Delete Me")
        second = self._create("Keep Me", selected=True)

        dashboard.delete_ai_strategy(first["id"])

        strategies = repository.load_ai_strategies()
        ids = {item["id"] for item in strategies}
        self.assertNotIn(first["id"], ids)
        self.assertIn(second["id"], ids)
        selected = [item["id"] for item in strategies if item.get("selected")]
        self.assertEqual(selected, [second["id"]])

    def test_delete_is_blocked_for_active_position(self):
        strategy = self._create("Position Owner")
        with repository.connect_db() as conn:
            conn.execute(
                """
                INSERT INTO ai_strategy_positions (
                    market, account_id, symbol, strategy_id, strategy_version,
                    profile_hash, side, entry_thesis, invalidation_conditions,
                    entry_price, filled_qty, remaining_qty, status,
                    opened_at, created_at, updated_at
                ) VALUES (
                    'KR', 'demo', '005930', ?, 1, 'hash', 'long', '{}', '[]',
                    70000, 1, 1, 'open', '2026-07-30', '2026-07-30',
                    '2026-07-30'
                )
                """,
                (strategy["id"],),
            )
            conn.commit()

        with self.assertRaises(HTTPException) as blocked:
            dashboard.delete_ai_strategy(strategy["id"])

        self.assertEqual(blocked.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
