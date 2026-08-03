import unittest
from pathlib import Path
import json

from src.db.repository import (
    init_db,
    connect_db,
    load_ai_strategies,
    save_ai_strategies,
    get_ai_strategy_events,
    record_ai_strategy_event,
    _load_token_usage,
    update_token_usage,
    save_scheduler_result,
    load_latest_scheduler_result,
    load_auto_approval_state,
    save_auto_approval_state
)

class DbMigrationTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with connect_db() as conn:
            conn.execute("DELETE FROM ai_strategies")
            conn.execute("DELETE FROM token_usage")
            conn.execute("DELETE FROM auto_approval")
            conn.execute("DELETE FROM scheduler_results")
            conn.execute("DELETE FROM ai_strategy_events")
            conn.commit()
            
        from src.db.repository import TOKEN_USAGE_FILE, AI_STRATEGIES_FILE
        if TOKEN_USAGE_FILE.exists():
            try:
                TOKEN_USAGE_FILE.unlink()
            except Exception:
                pass
        if AI_STRATEGIES_FILE.exists():
            try:
                AI_STRATEGIES_FILE.unlink()
            except Exception:
                pass

    def test_ai_strategies_db_persistence(self):
        # Create a test strategy
        test_strat = [
            {
                "id": "test_strat_db",
                "name": "Test Strategy",
                "provider": "openai",
                "model": "gpt-5-mini",
                "weight": 0.5,
                "description": "Test description",
                "selected": True
            }
        ]
        
        # Save to DB
        save_ai_strategies(test_strat)
        
        # Load from DB
        loaded = load_ai_strategies()
        
        # Find our test strategy
        found = [s for s in loaded if s["id"] == "test_strat_db"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Test Strategy")
        self.assertEqual(found[0]["weight"], 0.5)
        self.assertTrue(found[0]["selected"])
        self.assertEqual(found[0]["status"], "approved")
        self.assertEqual(found[0]["strategy_version"], 1)
        self.assertIn("profile", found[0])
        self.assertIn("profile_hash", found[0])
        self.assertEqual(found[0]["profile"]["model"], "gpt-5-mini")

    def test_ai_strategy_extended_metadata_and_events_persist(self):
        test_strat = [
            {
                "id": "test_strategy_meta",
                "name": "Meta Strategy",
                "provider": "openai",
                "model": "gpt-5-mini",
                "weight": 0.35,
                "description": "Profile based strategy",
                "selected": False,
                "status": "draft",
                "strategy_version": 2,
                "profile": {
                    "strategy_type": "trend",
                    "risk_level": "balanced",
                    "model": "gpt-5-mini",
                    "ai_weight": 0.35,
                },
            }
        ]

        save_ai_strategies(test_strat)
        loaded = load_ai_strategies()
        found = next(s for s in loaded if s["id"] == "test_strategy_meta")
        self.assertEqual(found["status"], "approved")
        self.assertEqual(found["strategy_version"], 2)
        self.assertEqual(found["profile"]["strategy_type"], "trend")
        self.assertEqual(found["profile"]["ai_weight"], 0.35)
        self.assertTrue(found["profile_hash"])

        record_ai_strategy_event("test_strategy_meta", "static_verified", {"ok": True}, 2)
        events = get_ai_strategy_events("test_strategy_meta")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "static_verified")

    def test_token_usage_db_persistence(self):
        # Initial usage check
        initial = _load_token_usage()
        initial_calls = initial.get("api_calls", 0)

        # Update usage
        update_token_usage(prompt=100, completion=50, total=150)
        
        # Reload and check
        updated = _load_token_usage()
        self.assertEqual(updated["prompt_tokens"], 100)
        self.assertEqual(updated["completion_tokens"], 50)
        self.assertEqual(updated["total_tokens"], 150)
        self.assertEqual(updated["api_calls"], initial_calls + 1)

    def test_scheduler_result_db_persistence(self):
        test_result = {"results": [{"symbol": "005930", "decision": "buy"}]}
        recorded_at = "2026-05-29T10:00:00.000000+09:00"
        
        save_scheduler_result("daily_auto", recorded_at, test_result)
        
        loaded = load_latest_scheduler_result()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["mode"], "daily_auto")
        self.assertEqual(loaded["recorded_at"], recorded_at)
        self.assertEqual(loaded["result"]["results"][0]["symbol"], "005930")
        self.assertEqual(loaded["result"]["execution_status"], "success")

    def test_auto_approval_db_persistence(self):
        save_auto_approval_state(True)
        self.assertTrue(load_auto_approval_state())
        
        save_auto_approval_state(False)
        self.assertFalse(load_auto_approval_state())

if __name__ == "__main__":
    unittest.main()
