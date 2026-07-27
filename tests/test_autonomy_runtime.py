import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.ai_stock.automation_service import _execute_order
from src.strategy.autonomy.runtime import (
    RuntimeConfigurationError,
    build_runtime_contexts,
)


NOW = datetime(2026, 7, 23, tzinfo=timezone.utc)


def account():
    return {
        "available": True,
        "account_id": "account-1",
        "snapshot_id": "portfolio-1",
        "data_as_of": NOW.isoformat(),
        "total_equity": 1_000_000,
        "available_cash": 500_000,
        "daily_pnl": 0,
        "market_exposure_value": 100_000,
        "strategy_exposure_value": 40_000,
        "open_position_risk_amount_excluding_reservations": 20_000,
        "kill_switch_active": False,
        "protection_global_block": False,
        "holdings": {},
    }


def market():
    return {
        "snapshot_id": "market-1",
        "strategy_id": "s1",
        "data_as_of": NOW.isoformat(),
        "evaluated_at": NOW.isoformat(),
        "regime": "bull",
        "candidates": [{"symbol": "005930", "current_price": 1000}],
        "instruments": {
            "005930": {
                "current_price": 1000,
                "average_daily_trading_value": 10_000_000,
                "sector_exposure_value": 50_000,
                "sector": "semiconductor",
                "data_as_of": NOW.isoformat(),
            }
        },
    }


class AutonomyRuntimeTests(unittest.TestCase):
    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.count_daily_new_risk_managed_orders",
        return_value=0,
    )
    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.list_active_reserved_exposures",
        return_value=[],
    )
    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.list_strategy_positions",
        return_value=[],
    )
    def test_builds_immutable_trusted_risk_snapshots(
        self, positions, reservations, daily_orders
    ):
        market_context, portfolio_context = build_runtime_contexts(
            market="KR", strategy_id="s1",
            account_snapshot=account(), market_snapshot=market()
        )

        self.assertEqual(market_context.regime, "bull")
        self.assertEqual(
            portfolio_context.risk_snapshots["005930"].total_equity, 1_000_000
        )
        positions.assert_called_once_with(
            market="KR", strategy_id="s1", active_only=True
        )
        reservations.assert_called_once_with(account_id="account-1", market="KR")
        self.assertEqual(
            portfolio_context.risk_snapshots["005930"].daily_new_risk_orders, 0
        )
        with self.assertRaises(TypeError):
            market_context.features["new"] = "unsafe"

    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.count_daily_new_risk_managed_orders",
        return_value=0,
    )
    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.list_active_reserved_exposures",
        return_value=[],
    )
    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.list_strategy_positions",
        return_value=[],
    )
    def test_missing_account_or_instrument_data_fails_closed(
        self, positions, reservations, daily_orders
    ):
        unavailable = account()
        unavailable["available"] = False
        with self.assertRaises(RuntimeConfigurationError):
            build_runtime_contexts(
                market="KR",
                strategy_id="s1",
                account_snapshot=unavailable,
                market_snapshot=market(),
            )

        incomplete = market()
        incomplete["instruments"] = {}
        with self.assertRaises(RuntimeConfigurationError):
            build_runtime_contexts(
                market="KR",
                strategy_id="s1",
                account_snapshot=account(),
                market_snapshot=incomplete,
            )

    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.count_daily_new_risk_managed_orders",
        return_value=1,
    )
    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.list_active_reserved_exposures",
        return_value=[
            {
                "strategy_id": "s1",
                "symbol": "005930",
                "pending_exposure_value": 30_000,
            }
        ],
    )
    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.list_strategy_positions",
        return_value=[],
    )
    def test_runtime_adds_pending_reservation_to_all_matching_exposures(
        self, positions, reservations, daily_orders
    ):
        _, portfolio = build_runtime_contexts(
            market="KR",
            strategy_id="s1",
            account_snapshot=account(),
            market_snapshot=market(),
        )
        risk = portfolio.risk_snapshots["005930"]
        self.assertEqual(risk.reserved_symbol_exposure_value, 30_000)
        self.assertEqual(risk.reserved_market_exposure_value, 30_000)
        self.assertEqual(risk.reserved_sector_exposure_value, 30_000)
        self.assertEqual(risk.reserved_strategy_exposure_value, 30_000)
        self.assertEqual(risk.daily_new_risk_orders, 1)

    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.count_daily_new_risk_managed_orders",
        return_value=1,
    )
    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.list_active_reserved_exposures",
        return_value=[
            {
                "id": 9,
                "strategy_id": "s1",
                "symbol": "005930",
                "pending_exposure_value": 30_000,
            }
        ],
    )
    @patch(
        "src.strategy.autonomy.runtime.ai_stock_repository.list_strategy_positions",
        return_value=[],
    )
    def test_approval_revalidation_can_exclude_its_own_reservation(
        self, positions, reservations, daily_orders
    ):
        _, portfolio = build_runtime_contexts(
            market="KR",
            strategy_id="s1",
            account_snapshot=account(),
            market_snapshot=market(),
            exclude_reservation_id=9,
        )
        risk = portfolio.risk_snapshots["005930"]
        self.assertEqual(risk.reserved_symbol_exposure_value, 0)
        self.assertEqual(risk.reserved_market_exposure_value, 0)

    def test_legacy_execute_symbol_is_permanently_blocked(self):
        result = _execute_order(
            "KR",
            {"symbol": "005930"},
            {"quantity": 1, "entry_price": 1000},
            "s1",
            None,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("autonomy runtime", result["message"])


if __name__ == "__main__":
    unittest.main()
