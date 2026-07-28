from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from src.strategy.autonomy.operational_context import (
    OperationalSnapshot,
    OperationalSnapshotProvider,
    assemble_operational_run_once,
)
from src.strategy.autonomy.runtime import RuntimeConfigurationError


class _Repo:
    def __init__(self, now):
        self.now = now
        self.positions = []

    def list_scans(self, **_kwargs):
        return [{
            "id": 7, "strategy_id": "s1", "status": "completed",
            "data_as_of": self.now.isoformat(),
        }]

    def list_candidates(self, **_kwargs):
        return [{
            "symbol": "AAA", "strategy_id": "s1", "decision": "buy",
            "current_price": 100, "data_as_of": self.now.isoformat(),
            "avg_trading_value": 1_000_000, "sector": "technology",
        }]

    def list_strategy_positions(self, **_kwargs):
        return self.positions

    def get_or_create_daily_equity_baseline(self, **kwargs):
        return ({"baseline_equity": kwargs["baseline_equity"]}, False)

    def daily_cashflow_reconciliation(self, **_kwargs):
        return {"reconciled_amount": 0, "unresolved_count": 0}

    def list_unprotected_strategy_positions(self, **_kwargs):
        return []


class _Market:
    def __init__(self, now):
        self.now = now
        self.prices = [100 + index * .1 for index in range(220)]

    def quote(self, _market, _symbol):
        return {"price": self.prices[-1], "data_as_of": self.now.isoformat()}

    def daily_series(self, _market, _symbol):
        return self.prices

    def index_series(self, _market):
        return {"INDEX": self.prices}


class _KR:
    def get_balance(self):
        return {
            "rt_cd": "0", "output1": [],
            "output2": [{"tot_evlu_amt": "1000000", "dnca_tot_amt": "1000000"}],
        }


class _KRHolding:
    def get_balance(self):
        return {
            "rt_cd": "0",
            "output1": [{"pdno": "AAA", "hldg_qty": "2", "evlu_amt": "220"}],
            "output2": [{"tot_evlu_amt": "1000", "dnca_tot_amt": "780"}],
        }


class OperationalContextTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 23, 3, tzinfo=timezone.utc)

    def test_builds_trusted_kr_read_only_snapshot(self):
        provider = OperationalSnapshotProvider(
            kr_broker=_KR(), market_data=_Market(self.now),
            candidate_repository=_Repo(self.now), clock=lambda: self.now,
            account_id="acct",
        )
        result = provider.snapshot("KR", "s1")
        self.assertTrue(result.account["available"])
        self.assertEqual("AAA", result.market["candidates"][0]["symbol"])
        self.assertNotEqual("unknown", result.market["regime"])

    def test_stale_scan_fails_closed_before_account_use(self):
        stale = self.now - timedelta(minutes=10)
        provider = OperationalSnapshotProvider(
            kr_broker=_KR(), market_data=_Market(self.now),
            candidate_repository=_Repo(stale), clock=lambda: self.now,
            max_age_seconds=300, account_id="acct",
        )
        with self.assertRaisesRegex(RuntimeConfigurationError, "stale"):
            provider.snapshot("KR", "s1")

    def test_invalid_candidate_price_does_not_block_valid_candidates(self):
        repo = _Repo(self.now)
        original = repo.list_candidates

        def candidates(**kwargs):
            return [
                {
                    **original(**kwargs)[0],
                    "symbol": "BROKEN",
                    "current_price": None,
                },
                original(**kwargs)[0],
            ]

        repo.list_candidates = candidates
        provider = OperationalSnapshotProvider(
            kr_broker=_KR(),
            market_data=_Market(self.now),
            candidate_repository=repo,
            clock=lambda: self.now,
            account_id="acct",
        )
        result = provider.snapshot("KR", "s1")
        self.assertEqual(["AAA"], [row["symbol"] for row in result.market["candidates"]])

    def test_broker_error_fails_closed(self):
        class Broken:
            def get_balance(self):
                return {"_error": "network"}

        provider = OperationalSnapshotProvider(
            kr_broker=Broken(), market_data=_Market(self.now),
            candidate_repository=_Repo(self.now), clock=lambda: self.now,
            account_id="acct",
        )
        with self.assertRaisesRegex(RuntimeConfigurationError, "account query"):
            provider.snapshot("KR", "s1")

    def test_run_once_is_environment_neutral_and_delegates_to_runtime(self):
        class Provider:
            def snapshot(self, *_args):
                return OperationalSnapshot({}, {})

        runtime = Mock()
        runtime.run.return_value = "ok"
        runner = assemble_operational_run_once(
            snapshot_provider=Provider(), runtime=runtime
        )
        self.assertEqual(
            runner(market="KR", strategy_id="s1", cycle_key="c1"), "ok"
        )
        runtime.run.assert_called_once()

    def test_active_positions_supply_strategy_exposure_and_open_risk(self):
        repo = _Repo(self.now)
        repo.positions = [{
            "account_id": "acct", "market": "KR", "symbol": "AAA",
            "strategy_id": "s1", "side": "long", "remaining_qty": 2,
            "average_price": 100, "current_stop_price": 90,
        }]
        provider = OperationalSnapshotProvider(
            kr_broker=_KRHolding(), market_data=_Market(self.now),
            candidate_repository=repo, clock=lambda: self.now,
            account_id="acct", kill_switch_reader=lambda: False,
        )
        account = provider.snapshot("KR", "s1").account
        self.assertEqual(_Market(self.now).prices[-1] * 2, account["strategy_exposure_value"])
        self.assertEqual(20, account["open_position_risk_amount_excluding_reservations"])
        self.assertFalse(account["kill_switch_active"])

    def test_missing_position_stop_fails_closed(self):
        repo = _Repo(self.now)
        repo.positions = [{
            "account_id": "acct", "market": "KR", "symbol": "AAA",
            "strategy_id": "s1", "side": "long", "remaining_qty": 2,
            "average_price": 100, "current_stop_price": None,
        }]
        provider = OperationalSnapshotProvider(
            kr_broker=_KRHolding(), market_data=_Market(self.now),
            candidate_repository=repo, clock=lambda: self.now,
            account_id="acct",
        )
        with self.assertRaisesRegex(RuntimeConfigurationError, "current_stop_price"):
            provider.snapshot("KR", "s1")

    def test_kill_switch_read_failure_is_active(self):
        def broken():
            raise OSError("unreadable runtime directory")

        provider = OperationalSnapshotProvider(
            kr_broker=_KR(), market_data=_Market(self.now),
            candidate_repository=_Repo(self.now), clock=lambda: self.now,
            account_id="acct", kill_switch_reader=broken,
        )
        self.assertTrue(provider.snapshot("KR", "s1").account["kill_switch_active"])


if __name__ == "__main__":
    unittest.main()
