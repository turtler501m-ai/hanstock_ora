import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.db import ai_stock_repository as repo


class DailyManagedOrderCountTest(unittest.TestCase):
    def test_counts_only_broker_reached_buy_orders_for_owner_and_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite"

            def connect():
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                return closing(conn)

            with connect() as conn:
                repo.init_ai_stock_tables(conn)
                conn.execute(
                    """
                    INSERT INTO ai_strategy_positions
                    (id, market, account_id, symbol, strategy_id, status, side,
                     filled_qty, remaining_qty, created_at, updated_at)
                    VALUES
                    (1, 'KR', 'A1', '005930', 'alpha', 'open', 'long', 1, 1, 'x', 'x'),
                    (2, 'KR', 'A2', '005930', 'alpha', 'open', 'long', 1, 1, 'x', 'x')
                    """
                )
                rows = (
                    (1, "buy", "submitted", "2026-07-23T09:00:00+09:00"),
                    (1, "buy", "filled", "2026-07-23T10:00:00+09:00"),
                    (1, "buy", "approved", "2026-07-23T11:00:00+09:00"),
                    (1, "sell", "filled", "2026-07-23T12:00:00+09:00"),
                    (2, "buy", "filled", "2026-07-23T13:00:00+09:00"),
                    (1, "buy", "filled", "2026-07-24T09:00:00+09:00"),
                )
                for index, (position_id, action, status, created_at) in enumerate(rows, 1):
                    conn.execute(
                        """
                        INSERT INTO ai_managed_orders
                        (client_order_key, decision_id, position_id, market, symbol,
                         strategy_id, action, order_type, requested_qty, status,
                         created_at, updated_at)
                        VALUES (?, ?, ?, 'KR', '005930', 'alpha', ?, 'limit',
                                1, ?, ?, ?)
                        """,
                        (
                            f"key-{index}", index, position_id, action, status,
                            created_at, created_at,
                        ),
                    )
                conn.commit()
            with patch.object(repo, "_connect", side_effect=connect):
                count = repo.count_daily_new_risk_managed_orders(
                    account_id="A1",
                    market="KR",
                    strategy_id="alpha",
                    day_start="2026-07-23T00:00:00+09:00",
                    day_end="2026-07-24T00:00:00+09:00",
                )
            self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
