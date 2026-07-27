import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.db import ai_stock_repository as repo


class AiRiskReservationRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "risk-reservations.sqlite"

        def connect():
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            return closing(conn)

        self.connect_patch = patch.object(repo, "_connect", side_effect=connect)
        self.connect_patch.start()
        with connect() as conn:
            repo.init_ai_stock_tables(conn)
            conn.commit()

    def tearDown(self):
        self.connect_patch.stop()
        self.temp_dir.cleanup()

    def test_market_snapshot_is_idempotent_but_immutable(self):
        data = {
            "snapshot_key": "market:KR:20260723T090000",
            "market": "KR",
            "source": "test",
            "data_as_of": "2026-07-23T09:00:00+09:00",
            "regime": "bull_pullback",
            "payload": {"index": 3000.0, "breadth": 0.62},
        }
        first = repo.create_market_snapshot(data)
        second = repo.create_market_snapshot(dict(data))
        self.assertEqual(first, second)
        self.assertEqual(repo.get_market_snapshot(first)["payload"]["index"], 3000.0)

        changed = dict(data)
        changed["payload"] = {"index": 2800.0, "breadth": 0.62}
        with self.assertRaisesRegex(ValueError, "different market data"):
            repo.create_market_snapshot(changed)

    def test_portfolio_snapshot_persists_canonical_payload(self):
        snapshot_id = repo.create_portfolio_snapshot({
            "snapshot_key": "portfolio:A1:KR:20260723T090000",
            "account_id": "A1",
            "market": "KR",
            "source": "broker",
            "data_as_of": "2026-07-23T09:00:00+09:00",
            "cash": 1_000_000,
            "total_eval": 2_000_000,
            "stock_eval": 1_000_000,
            "payload": {"holdings": [{"symbol": "005930", "qty": 10}]},
        })
        stored = repo.get_portfolio_snapshot(snapshot_id)
        self.assertEqual(stored["account_id"], "A1")
        self.assertEqual(stored["payload"]["holdings"][0]["qty"], 10)
        self.assertEqual(
            len(repo.list_portfolio_snapshots(account_id="A1", market="KR")),
            1,
        )

    def test_active_key_is_idempotent(self):
        request = {
            "account_id": "A1",
            "market": "KR",
            "strategy_id": "swing-v1",
            "position_id": 11,
            "order_id": 21,
            "cash_amount": 300_000,
            "risk_amount": 20_000,
        }
        first = repo.reserve_risk_budget(
            request, available_cash=1_000_000, risk_budget_limit=50_000
        )
        second = repo.reserve_risk_budget(
            request, available_cash=1_000_000, risk_budget_limit=50_000
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(repo.list_risk_reservations(status="active")), 1)

    def test_active_reservations_share_cash_and_risk_across_strategies(self):
        repo.reserve_risk_budget(
            {
                "account_id": "A1",
                "market": "KR",
                "strategy_id": "strategy-a",
                "position_id": 1,
                "order_id": 1,
                "cash_amount": 600_000,
                "risk_amount": 30_000,
            },
            available_cash=1_000_000,
            risk_budget_limit=50_000,
        )
        with self.assertRaisesRegex(ValueError, "cash reservation"):
            repo.reserve_risk_budget(
                {
                    "account_id": "A1",
                    "market": "KR",
                    "strategy_id": "strategy-b",
                    "position_id": 2,
                    "order_id": 2,
                    "cash_amount": 500_000,
                    "risk_amount": 1_000,
                },
                available_cash=1_000_000,
                risk_budget_limit=50_000,
            )
        with self.assertRaisesRegex(ValueError, "risk reservation"):
            repo.reserve_risk_budget(
                {
                    "account_id": "A1",
                    "market": "KR",
                    "strategy_id": "strategy-b",
                    "position_id": 2,
                    "order_id": 3,
                    "cash_amount": 100_000,
                    "risk_amount": 25_000,
                },
                available_cash=1_000_000,
                risk_budget_limit=50_000,
            )

    def test_multiple_trade_risks_fit_under_distinct_total_account_limit(self):
        for index in (1, 2):
            reserved = repo.reserve_risk_budget(
                {
                    "account_id": "A1",
                    "market": "KR",
                    "strategy_id": f"strategy-{index}",
                    "position_id": index,
                    "order_id": index,
                    "cash_amount": 100_000,
                    "risk_amount": 9_000,
                },
                available_cash=1_000_000,
                risk_budget_limit=30_000,
            )
            self.assertTrue(reserved["created"])
        active = repo.list_risk_reservations(account_id="A1", status="active")
        self.assertEqual(len(active), 2)
        self.assertEqual(sum(item["risk_amount"] for item in active), 18_000)

    def test_parallel_pending_orders_share_atomic_position_exposure_limit(self):
        common = {
            "account_id": "A1",
            "market": "KR",
            "strategy_id": "strategy-a",
            "symbol": "005930",
            "sector_key": "semiconductor",
            "risk_amount": 100,
            "exposure_limits": {
                "position": 100_000,
                "market": 500_000,
                "sector": 200_000,
                "strategy": 300_000,
            },
        }
        repo.reserve_risk_budget(
            dict(
                common,
                position_id=1,
                cash_amount=60_000,
                exposure_amount=60_000,
            ),
            available_cash=1_000_000,
            risk_budget_limit=10_000,
        )
        with self.assertRaisesRegex(ValueError, "position exposure reservation"):
            repo.reserve_risk_budget(
                dict(
                    common,
                    position_id=2,
                    cash_amount=50_000,
                    exposure_amount=50_000,
                ),
                available_cash=1_000_000,
                risk_budget_limit=10_000,
            )

    def test_release_is_idempotent_and_frees_budget(self):
        first = repo.reserve_risk_budget(
            {
                "account_id": "A1",
                "market": "KR",
                "strategy_id": "strategy-a",
                "position_id": 1,
                "order_id": 1,
                "cash_amount": 800_000,
                "risk_amount": 40_000,
            },
            available_cash=1_000_000,
            risk_budget_limit=50_000,
        )
        released = repo.release_risk_reservation(first["id"], reason="order canceled")
        released_again = repo.release_risk_reservation(first["id"])
        self.assertEqual(released["status"], "released")
        self.assertEqual(released_again["status"], "released")

        replacement = repo.reserve_risk_budget(
            {
                "account_id": "A1",
                "market": "KR",
                "strategy_id": "strategy-b",
                "position_id": 2,
                "order_id": 2,
                "cash_amount": 800_000,
                "risk_amount": 40_000,
            },
            available_cash=1_000_000,
            risk_budget_limit=50_000,
        )
        self.assertTrue(replacement["created"])

    def test_reserved_exposure_counts_only_unfilled_order_quantity(self):
        position_id = repo.create_strategy_position(
            {
                "market": "KR",
                "account_id": "A1",
                "symbol": "005930",
                "strategy_id": "strategy-a",
                "strategy_version": 1,
                "profile_hash": "hash",
            }
        )
        repo.reserve_risk_budget(
            {
                "account_id": "A1",
                "market": "KR",
                "strategy_id": "strategy-a",
                "position_id": position_id,
                "cash_amount": 1_000,
                "risk_amount": 100,
            },
            available_cash=10_000,
            risk_budget_limit=1_000,
        )
        repo.create_managed_order(
            {
                "client_order_key": "pending-exposure",
                "decision_id": 1,
                "position_id": position_id,
                "market": "KR",
                "symbol": "005930",
                "strategy_id": "strategy-a",
                "action": "buy",
                "order_type": "limit",
                "requested_qty": 10,
                "requested_price": 100,
                "filled_qty": 4,
                "status": "partially_filled",
            }
        )
        rows = repo.list_active_reserved_exposures(account_id="A1", market="KR")
        self.assertEqual(rows[0]["pending_exposure_value"], 600)


if __name__ == "__main__":
    unittest.main()
