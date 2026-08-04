import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.db.connection import DEFAULT_BUSY_TIMEOUT_MS, open_sqlite
from src.db.repository import DBWrapper
from src.runtime_state import PersistentRuntimeState, RuntimeStateStore


class SqliteConnectionPolicyTests(unittest.TestCase):
    def test_open_sqlite_applies_shared_connection_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.sqlite"
            conn = open_sqlite(path)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(
                    conn.execute("PRAGMA busy_timeout").fetchone()[0],
                    DEFAULT_BUSY_TIMEOUT_MS,
                )
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
            finally:
                conn.close()

    def test_db_wrapper_exposes_explicit_close(self):
        conn = sqlite3.connect(":memory:")
        wrapper = DBWrapper(conn, close_on_exit=True)
        wrapper.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_open_sqlite_closes_connection_when_pragma_initialization_fails(self):
        conn = Mock()
        conn.execute.side_effect = sqlite3.OperationalError("pragma failed")
        with patch("src.db.connection.sqlite3.connect", return_value=conn):
            with self.assertRaises(sqlite3.OperationalError):
                open_sqlite(":memory:")
        conn.close.assert_called_once_with()


class RuntimeStatePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "runtime.sqlite"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_state_is_restored_by_a_new_instance(self):
        defaults = {"is_running": False, "result": None}
        first = PersistentRuntimeState(
            "scheduler",
            defaults,
            store=RuntimeStateStore(self.path),
        )
        first.replace({"is_running": False, "result": {"ok": True}})

        restored = PersistentRuntimeState(
            "scheduler",
            defaults,
            store=RuntimeStateStore(self.path),
        )
        self.assertEqual(restored["result"], {"ok": True})

    def test_claim_is_exclusive_until_state_is_released(self):
        defaults = {"is_running": False, "owner_pid": None}
        first = PersistentRuntimeState(
            "scheduler",
            defaults,
            store=RuntimeStateStore(self.path),
        )
        second = PersistentRuntimeState(
            "scheduler",
            defaults,
            store=RuntimeStateStore(self.path),
        )
        payload = {"is_running": True, "owner_pid": os.getpid()}

        self.assertTrue(first.claim(payload))
        self.assertFalse(second.claim(payload))
        first.replace(defaults)
        self.assertTrue(second.claim(payload))


if __name__ == "__main__":
    unittest.main()
