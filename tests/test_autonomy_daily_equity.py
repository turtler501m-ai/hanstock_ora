from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.strategy.autonomy.daily_equity import DailyEquityService
from src.strategy.autonomy.protection import ProtectionGateSignal
from src.strategy.autonomy.runtime import RuntimeConfigurationError


class _Repo:
    def __init__(self):
        self.baseline = None
        self.unresolved = 0
        self.cashflow = 0

    def get_or_create_daily_equity_baseline(self, **kwargs):
        if self.baseline is None:
            self.baseline = dict(kwargs)
            return {"baseline_equity": kwargs["baseline_equity"]}, True
        return {"baseline_equity": self.baseline["baseline_equity"]}, False

    def daily_cashflow_reconciliation(self, **_kwargs):
        return {
            "reconciled_amount": self.cashflow,
            "unresolved_count": self.unresolved,
        }


class _Protection:
    def __init__(self, blocked=False):
        self.blocked = blocked

    def global_gate_signal(self, *, market=None):
        return ProtectionGateSignal(self.blocked, "test")


class DailyEquityTest(unittest.TestCase):
    def setUp(self):
        self.repo = _Repo()
        self.service = DailyEquityService(
            repo=self.repo,
            protection=_Protection(),
            external_reconciliation=lambda *_args: True,
        )
        self.now = datetime(2026, 7, 23, 3, tzinfo=timezone.utc)

    def evaluate(self, equity):
        return self.service.evaluate(
            account_id="acct", market="KR", current_total_equity=equity,
            snapshot_id=f"s-{equity}", data_as_of=self.now,
        )

    def test_first_snapshot_is_warmup_then_daily_pnl_is_available(self):
        first = self.evaluate(1_000)
        second = self.evaluate(1_100)
        self.assertTrue(first.warmup)
        self.assertTrue(first.block_new_risk)
        self.assertFalse(second.warmup)
        self.assertEqual(100, second.daily_pnl)
        self.assertFalse(second.block_new_risk)

    def test_reconciled_deposit_is_removed_from_pnl(self):
        self.evaluate(1_000)
        self.repo.cashflow = 200
        self.assertEqual(50, self.evaluate(1_250).daily_pnl)

    def test_unreconciled_cashflow_fails_closed(self):
        self.repo.unresolved = 1
        with self.assertRaisesRegex(RuntimeConfigurationError, "cashflow"):
            self.evaluate(1_000)

    def test_unreconciled_external_fill_fails_closed(self):
        service = DailyEquityService(
            repo=self.repo, protection=_Protection(),
            external_reconciliation=lambda *_args: False,
        )
        with self.assertRaisesRegex(RuntimeConfigurationError, "external execution"):
            service.evaluate(
                account_id="acct", market="US", current_total_equity=1_000,
                snapshot_id="s", data_as_of=self.now,
            )

    def test_actual_protection_gate_blocks_new_risk(self):
        service = DailyEquityService(
            repo=self.repo, protection=_Protection(True),
            external_reconciliation=lambda *_args: True,
        )
        first = service.evaluate(
            account_id="acct", market="KR", current_total_equity=1_000,
            snapshot_id="s1", data_as_of=self.now,
        )
        second = service.evaluate(
            account_id="acct", market="KR", current_total_equity=1_010,
            snapshot_id="s2", data_as_of=self.now,
        )
        self.assertTrue(first.block_new_risk)
        self.assertTrue(second.block_new_risk)


if __name__ == "__main__":
    unittest.main()
