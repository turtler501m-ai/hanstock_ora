import tempfile
import unittest
from pathlib import Path

from src.config import config
from src.db import repository as repository_facade

trade_repository = repository_facade


class LocalTradeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = config.trade_db_path
        self.temp_dir = tempfile.TemporaryDirectory()
        config.trade_db_path = str(Path(self.temp_dir.name) / "trades.sqlite")
        trade_repository.init_db()

    def tearDown(self):
        config.trade_db_path = self.original_db_path
        self.temp_dir.cleanup()

    def _insert_trade(self, *, status: str, filled_qty: int = 0, symbol: str = "005930") -> int:
        with trade_repository.connect_db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trades (
                    ts, symbol, name, action, qty, price, reason, ok, env, dry_run,
                    broker_order_id, order_status, filled_qty, filled_price,
                    pre_order_qty, response_msg, broker_result
                )
                VALUES (
                    '2026-07-29 09:00:00', ?, ?, 'buy', 10, 1000, 'test', 1, 'demo', 0,
                    '1234', ?, ?, 0, 0, '', '{}'
                )
                """,
                (symbol, symbol, status, filled_qty),
            )
            return int(cursor.lastrowid)

    def test_lists_unresolved_alias_with_reason(self):
        trade_id = self._insert_trade(status="submitted", symbol="Q530107")

        rows = trade_repository.list_local_trade_cleanup_candidates()

        row = next(item for item in rows if item["id"] == trade_id)
        self.assertEqual(row["cleanup_risk"], "high")
        self.assertIn("530107", row["cleanup_reason"])

    def test_deletes_unfilled_submitted_local_record(self):
        trade_id = self._insert_trade(status="submitted")

        deleted = trade_repository.delete_local_trade_record(trade_id)

        self.assertEqual(deleted["id"], trade_id)
        with trade_repository.connect_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM trades WHERE id = ?", (trade_id,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_protects_filled_or_open_record(self):
        filled_id = self._insert_trade(status="filled", filled_qty=10)
        open_id = self._insert_trade(status="open")

        with self.assertRaises(ValueError):
            trade_repository.delete_local_trade_record(filled_id)
        with self.assertRaises(ValueError):
            trade_repository.delete_local_trade_record(open_id)


if __name__ == "__main__":
    unittest.main()
