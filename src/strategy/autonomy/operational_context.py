"""Read-only KR/US operational snapshots for explicit autonomous run-once use.

This module deliberately has no broker order capability.  It converts trusted
KIS account reads and persisted AI-stock candidates into the immutable runtime
snapshot contract.  Missing, stale, or synthetic data raises instead of
silently falling back to configured capital.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from src.ai_stock.market_data import MarketDataProvider, get_provider
from src.config import config
from src.db import ai_stock_repository

from .runtime import AutonomyRuntime, RuntimeConfigurationError, RuntimeResult
from .daily_equity import DailyEquityService


class ReadOnlyBroker(Protocol):
    def get_balance(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class OperationalSnapshot:
    account: Mapping[str, Any]
    market: Mapping[str, Any]


class OperationalSnapshotProvider:
    """Build fail-closed snapshots using only broker reads and persisted scans."""

    def __init__(
        self,
        *,
        kr_broker: ReadOnlyBroker | None = None,
        us_balance_reader: Callable[[], Mapping[str, Any]] | None = None,
        market_data: MarketDataProvider | None = None,
        candidate_repository: Any = ai_stock_repository,
        daily_equity: DailyEquityService | None = None,
        account_id: str | None = None,
        kill_switch_reader: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        max_age_seconds: int = 300,
    ):
        self.kr_broker = kr_broker
        self.us_balance_reader = us_balance_reader
        self.market_data = market_data or get_provider()
        self.repository = candidate_repository
        self.account_id = str(
            account_id
            if account_id is not None
            else getattr(config, "kistock_account", "")
        ).strip()
        self.kill_switch_reader = kill_switch_reader or _default_kill_switch_reader
        self.daily_equity = daily_equity or DailyEquityService(
            repo=candidate_repository,
            external_reconciliation=lambda _account, _market, _date: (
                str(getattr(config, "autonomy_trading_env", "demo")).lower() == "demo"
            ),
        )
        self.clock = clock
        self.max_age_seconds = int(max_age_seconds)
        if self.max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")

    def snapshot(self, market: str, strategy_id: str) -> OperationalSnapshot:
        market = str(market).upper()
        if market not in {"KR", "US"}:
            raise RuntimeConfigurationError("market must be KR or US")
        now = _aware(self.clock(), "clock")
        candidates = self._latest_candidates(market, strategy_id, now)
        symbols = {str(row["symbol"]) for row in candidates}
        active_positions = tuple(
            row for row in self.repository.list_strategy_positions(
                market=market, active_only=True
            )
            if str(row.get("account_id") or "") == self.account_id
        )
        symbols.update(str(row["symbol"]) for row in active_positions)
        if not symbols:
            raise RuntimeConfigurationError("no candidate or active position symbols")
        instruments = self._instruments(market, symbols, candidates, now)
        account = (
            self._kr_account(instruments, active_positions, strategy_id, now)
            if market == "KR"
            else self._us_account(instruments, active_positions, strategy_id, now)
        )
        market_snapshot = self._market_snapshot(
            market, strategy_id, candidates, instruments, now
        )
        return OperationalSnapshot(account, market_snapshot)

    def _latest_candidates(
        self, market: str, strategy_id: str, now: datetime
    ) -> tuple[Mapping[str, Any], ...]:
        scans = self.repository.list_scans(market=market, limit=50)
        scan = next(
            (
                row for row in scans
                if str(row.get("strategy_id")) == str(strategy_id)
                and str(row.get("status")) == "completed"
            ),
            None,
        )
        if not scan:
            raise RuntimeConfigurationError("completed strategy scan is required")
        as_of = _parse_time(scan.get("data_as_of"), "scan data_as_of")
        _require_fresh(as_of, now, self.max_age_seconds, "candidate scan")
        rows = self.repository.list_candidates(
            market=market, scan_id=int(scan["id"]), limit=500
        )
        accepted = tuple(
            row for row in rows
            if str(row.get("strategy_id")) == str(strategy_id)
            and str(row.get("decision") or "").lower() not in {"reject", "excluded"}
        )
        if not accepted:
            raise RuntimeConfigurationError("latest scan has no usable candidates")
        for row in accepted:
            _positive(row.get("current_price"), f"{row.get('symbol')} current_price")
            _require_fresh(
                _parse_time(row.get("data_as_of"), "candidate data_as_of"),
                now, self.max_age_seconds, "candidate",
            )
        return accepted

    def _instruments(
        self,
        market: str,
        symbols: set[str],
        candidates: Sequence[Mapping[str, Any]],
        now: datetime,
    ) -> dict[str, dict[str, Any]]:
        by_symbol = {str(row["symbol"]): row for row in candidates}
        result: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            quote = self.market_data.quote(market, symbol)
            series = self.market_data.daily_series(market, symbol)
            if not isinstance(quote, Mapping) or not series or len(series) < 20:
                raise RuntimeConfigurationError(f"market data missing for {symbol}")
            price = _positive(
                quote.get("price", quote.get("current")), f"{symbol} quote"
            )
            data_as_of = _parse_time(quote.get("data_as_of"), f"{symbol} quote time")
            _require_fresh(data_as_of, now, self.max_age_seconds, f"{symbol} quote")
            row = by_symbol.get(symbol, {})
            adv = row.get("avg_trading_value")
            if adv is None:
                adv = row.get("average_daily_trading_value")
            if adv is None and (
                str(getattr(config, "autonomy_trading_env", "demo")).lower()
                == "demo"
                and str(getattr(config, "trading_env", "demo")).lower()
                == "demo"
                and not bool(getattr(config, "enable_live_trading", False))
            ):
                # The legacy Hanstock candidate table does not persist volume.
                # Use the universe admission floor only for guarded demo sizing.
                adv = 5_000_000_000.0 if market == "KR" else 20_000_000.0
            adv = _positive(adv, f"{symbol} average_daily_trading_value")
            result[symbol] = {
                "current_price": price,
                "data_as_of": data_as_of.isoformat(),
                "sector": str(row.get("sector") or row.get("instrument_type") or "").strip(),
                "average_daily_trading_value": adv,
                "sector_exposure_value": 0.0,
            }
            if not result[symbol]["sector"]:
                raise RuntimeConfigurationError(f"{symbol} sector is required")
        return result

    def _market_snapshot(self, market, strategy_id, candidates, instruments, now):
        index_map = self.market_data.index_series(market)
        if not index_map:
            raise RuntimeConfigurationError("market index series is required")
        closes = next(iter(index_map.values()))
        if len(closes) < 200:
            raise RuntimeConfigurationError("at least 200 index observations required")
        values = [_positive(item, "index close") for item in closes]
        returns = [
            values[i] / values[i - 1] - 1.0 for i in range(1, len(values))
        ]
        recent = returns[-20:]
        baseline = returns[-120:]
        realized = statistics.pstdev(recent) * math.sqrt(252)
        base_vol = statistics.pstdev(baseline) * math.sqrt(252)
        breadth_values = []
        for symbol in instruments:
            series = self.market_data.daily_series(market, symbol)
            if not series or len(series) < 2:
                raise RuntimeConfigurationError(f"breadth series missing for {symbol}")
            breadth_values.append(float(series[-1]) > float(series[-2]))
        regime = _classify(values, realized, base_vol, sum(breadth_values) / len(breadth_values))
        if regime == "unknown":
            raise RuntimeConfigurationError("market regime is unknown")
        data_as_of = min(
            _parse_time(item["data_as_of"], "instrument data_as_of")
            for item in instruments.values()
        )
        return {
            "snapshot_id": f"operational:{market}:{strategy_id}:{now.isoformat()}",
            "evaluated_at": now.isoformat(),
            "data_as_of": data_as_of.isoformat(),
            "regime": regime,
            "candidates": tuple(dict(row) for row in candidates),
            "instruments": instruments,
        }

    def _kr_account(self, instruments, active_positions, strategy_id, now):
        broker = self.kr_broker or _default_kr_broker()
        raw = broker.get_balance()
        if not isinstance(raw, Mapping) or raw.get("_error") or raw.get("rt_cd") not in (None, "0"):
            raise RuntimeConfigurationError("trusted KR account query failed")
        holdings: dict[str, dict[str, float]] = {}
        for row in raw.get("output1") or ():
            symbol = str(row.get("pdno") or "").strip()
            if not symbol:
                continue
            qty = _nonnegative(row.get("hldg_qty"), f"{symbol} quantity")
            value = _nonnegative(
                row.get("evlu_amt", qty * float(instruments.get(symbol, {}).get("current_price", 0))),
                f"{symbol} value",
            )
            holdings[symbol] = {"quantity": qty, "value": value}
        summary = next(iter(raw.get("output2") or ()), {})
        total = _positive(summary.get("tot_evlu_amt"), "KR total_equity")
        cash = _nonnegative(
            summary.get("dnca_tot_amt", summary.get("prvs_rcdl_excc_amt")),
            "KR available_cash",
        )
        return self._account_payload(
            "KR", total, cash, holdings, instruments, active_positions,
            strategy_id, now,
        )

    def _us_account(self, instruments, active_positions, strategy_id, now):
        reader = self.us_balance_reader or _default_us_balance_reader
        raw = reader()
        if not isinstance(raw, Mapping) or raw.get("_error"):
            raise RuntimeConfigurationError("trusted US account query failed")
        # A normalized reader is intentional: production default rejects all
        # demo/config fallback sources exposed by mistock.trader.get_balance().
        if raw.get("balance_source") not in {"kis"}:
            raise RuntimeConfigurationError("synthetic or capped US balance rejected")
        total = _positive(raw.get("total_eval"), "US total_equity")
        cash = _nonnegative(raw.get("cash"), "US available_cash")
        holdings = {
            str(row["symbol"]): {
                "quantity": _nonnegative(row.get("qty"), "US quantity"),
                "value": _nonnegative(row.get("value"), "US holding value"),
            }
            for row in raw.get("holdings") or ()
            if row.get("symbol")
        }
        return self._account_payload(
            "US", total, cash, holdings, instruments, active_positions,
            strategy_id, now,
        )

    def _account_payload(
        self, market, total, cash, holdings, instruments, active_positions,
        strategy_id, now,
    ):
        unknown = sorted(set(holdings) - set(instruments))
        if unknown:
            raise RuntimeConfigurationError(
                "holding instrument metadata missing: " + ",".join(unknown)
            )
        exposure = sum(float(item["value"]) for item in holdings.values())
        sector_exposure: dict[str, float] = {}
        for symbol, holding in holdings.items():
            sector = str(instruments[symbol]["sector"])
            sector_exposure[sector] = (
                sector_exposure.get(sector, 0.0) + float(holding["value"])
            )
        for instrument in instruments.values():
            instrument["sector_exposure_value"] = sector_exposure.get(
                str(instrument["sector"]), 0.0
            )
        strategy_exposure = 0.0
        open_risk = 0.0
        owned_qty: dict[str, float] = {}
        for position in active_positions:
            symbol = str(position.get("symbol") or "").strip()
            side = str(position.get("side") or "").lower()
            if side != "long":
                raise RuntimeConfigurationError(
                    f"unsupported active position side for {symbol}"
                )
            qty = _positive(position.get("remaining_qty"), f"{symbol} remaining_qty")
            owned_qty[symbol] = owned_qty.get(symbol, 0.0) + qty
            average = _positive(position.get("average_price"), f"{symbol} average_price")
            stop = _positive(
                position.get("current_stop_price"), f"{symbol} current_stop_price"
            )
            if stop >= average:
                # A stop above breakeven has no remaining downside risk, but is
                # still a valid protected position.
                position_risk = 0.0
            else:
                position_risk = (average - stop) * qty
            open_risk += position_risk
            if str(position.get("strategy_id")) == str(strategy_id):
                strategy_exposure += (
                    float(instruments[symbol]["current_price"]) * qty
                )
        for symbol, quantity in owned_qty.items():
            broker_quantity = float(holdings.get(symbol, {}).get("quantity", 0))
            if quantity > broker_quantity:
                raise RuntimeConfigurationError(
                    f"strategy position exceeds broker holding for {symbol}"
                )
        try:
            kill_switch_active = bool(self.kill_switch_reader())
        except Exception:
            kill_switch_active = True
        account_id = self.account_id
        snapshot_id = f"kis-read:{market}:{now.isoformat()}"
        equity = self.daily_equity.evaluate(
            account_id=account_id,
            market=market,
            current_total_equity=total,
            snapshot_id=snapshot_id,
            data_as_of=now,
        )
        return {
            "available": True,
            "account_id": account_id,
            "snapshot_id": snapshot_id,
            "data_as_of": now.isoformat(),
            "total_equity": total,
            "available_cash": cash,
            "daily_pnl": equity.daily_pnl,
            "market_exposure_value": exposure,
            "strategy_exposure_value": strategy_exposure,
            "open_position_risk_amount_excluding_reservations": open_risk,
            "protection_global_block": equity.block_new_risk,
            "kill_switch_active": kill_switch_active,
            "holdings": holdings,
        }


def assemble_operational_run_once(
    *, snapshot_provider: OperationalSnapshotProvider | None = None,
    runtime: AutonomyRuntime | None = None,
) -> Callable[..., RuntimeResult]:
    """Return an explicit callable; importing/scheduling never starts autonomy."""
    provider = snapshot_provider or OperationalSnapshotProvider()
    engine = runtime or AutonomyRuntime()

    def run_once(*, market: str, strategy_id: str, cycle_key: str) -> RuntimeResult:
        snapshots = provider.snapshot(market, strategy_id)
        return engine.run(
            cycle_key=cycle_key,
            strategy_id=strategy_id,
            market=market,
            account_snapshot=snapshots.account,
            market_snapshot=snapshots.market,
        )

    return run_once


def _default_kr_broker() -> ReadOnlyBroker:
    from src.api.kis_api import KIStockAPI
    return KIStockAPI()


def _default_us_balance_reader() -> Mapping[str, Any]:
    from src.mistock.trader import get_balance
    return get_balance()


def _default_kill_switch_reader() -> bool:
    from src.strategy.risk import RiskEngine
    return bool(RiskEngine().check_kill_switch())


def _classify(values, realized, baseline, breadth):
    price = values[-1]
    sma20 = sum(values[-20:]) / 20
    sma60 = sum(values[-60:]) / 60
    sma200 = sum(values[-200:]) / 200
    r5, r20 = price / values[-6] - 1, price / values[-21] - 1
    drawdown = price / max(values[-20:]) - 1
    ratio = realized / baseline if baseline > 0 else math.inf
    if drawdown <= -0.12 or r5 <= -0.08 or (ratio >= 2.5 and r5 < -0.04):
        return "crash"
    if price > sma20 > sma60 > sma200 and r20 > 0 and breadth >= .55 and ratio < 1.3:
        return "bull"
    if price > sma60 > sma200 and price <= sma20 and r20 > 0 and r5 <= 0:
        return "bull_pullback"
    if price < sma20 < sma60 < sma200 and r20 < 0 and breadth <= .45:
        return "bear"
    if price < sma60 < sma200 and price > sma20 and r5 > 0:
        return "bear_rally"
    if abs(price / sma60 - 1) <= .05:
        return "sideways_high_vol" if ratio >= 1.3 else "sideways_low_vol"
    return "unknown"


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeConfigurationError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_time(value, name):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigurationError(f"{name} must be ISO-8601") from exc
    return _aware(parsed, name)


def _require_fresh(data_as_of, now, max_age, name):
    age = (now - data_as_of).total_seconds()
    if age < 0 or age > max_age:
        raise RuntimeConfigurationError(f"{name} is stale")


def _positive(value, name):
    number = _finite(value, name)
    if number <= 0:
        raise RuntimeConfigurationError(f"{name} must be positive")
    return number


def _nonnegative(value, name):
    number = _finite(value, name)
    if number < 0:
        raise RuntimeConfigurationError(f"{name} must be nonnegative")
    return number


def _finite(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigurationError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise RuntimeConfigurationError(f"{name} must be finite")
    return number
