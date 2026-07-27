"""Authoritative daily PnL derived from an immutable trusted equity baseline."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.db import ai_stock_repository

from .protection import HardStopProtectionService, ProtectionGateSignal
from .runtime import RuntimeConfigurationError


@dataclass(frozen=True)
class DailyEquityResult:
    daily_pnl: float
    baseline_equity: float
    reconciled_cashflow: float
    trading_date: str
    warmup: bool
    protection: ProtectionGateSignal

    @property
    def block_new_risk(self) -> bool:
        return self.warmup or self.protection.block_new_risk


class DailyEquityService:
    """Compute PnL only when broker cashflows and external fills are reconciled."""

    def __init__(
        self,
        *,
        repo: Any = ai_stock_repository,
        protection: HardStopProtectionService | None = None,
        external_reconciliation: Callable[[str, str, str], bool] | None = None,
    ):
        self.repo = repo
        self.protection = protection or HardStopProtectionService(repo=repo)
        self.external_reconciliation = external_reconciliation

    def evaluate(
        self,
        *,
        account_id: str,
        market: str,
        current_total_equity: float,
        snapshot_id: str,
        data_as_of: datetime,
    ) -> DailyEquityResult:
        market = str(market).upper()
        if market not in {"KR", "US"}:
            raise RuntimeConfigurationError("market must be KR or US")
        if not account_id or not snapshot_id:
            raise RuntimeConfigurationError("account_id and snapshot_id are required")
        if not isinstance(data_as_of, datetime) or data_as_of.tzinfo is None:
            raise RuntimeConfigurationError("equity data_as_of must be timezone-aware")
        equity = _positive(current_total_equity, "current_total_equity")
        zone = ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")
        trading_date = data_as_of.astimezone(zone).date().isoformat()

        cashflow = self.repo.daily_cashflow_reconciliation(
            account_id=account_id, market=market, trading_date=trading_date
        )
        if int(cashflow.get("unresolved_count") or 0):
            raise RuntimeConfigurationError("unreconciled account cashflow")
        if self.external_reconciliation is None:
            raise RuntimeConfigurationError("external fill reconciliation is required")
        if not self.external_reconciliation(account_id, market, trading_date):
            raise RuntimeConfigurationError("unreconciled external execution")
        # Only a fully reconciled observation is allowed to become the
        # immutable first baseline for the trading day.
        baseline, created = self.repo.get_or_create_daily_equity_baseline(
            account_id=account_id,
            market=market,
            trading_date=trading_date,
            baseline_equity=equity,
            snapshot_id=snapshot_id,
            data_as_of=data_as_of.isoformat(),
        )

        # Deposits are positive and withdrawals negative.  Removing their net
        # effect leaves trading PnL, including realized and unrealized changes.
        adjustment = _finite(
            cashflow.get("reconciled_amount", 0), "reconciled_cashflow"
        )
        baseline_equity = _positive(
            baseline.get("baseline_equity"), "baseline_equity"
        )
        daily_pnl = equity - baseline_equity - adjustment
        protection = self.protection.global_gate_signal(market=market)
        return DailyEquityResult(
            daily_pnl=daily_pnl,
            baseline_equity=baseline_equity,
            reconciled_cashflow=adjustment,
            trading_date=trading_date,
            warmup=bool(created),
            protection=protection,
        )


def _finite(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigurationError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise RuntimeConfigurationError(f"{name} must be finite")
    return result


def _positive(value: Any, name: str) -> float:
    result = _finite(value, name)
    if result <= 0:
        raise RuntimeConfigurationError(f"{name} must be positive")
    return result
