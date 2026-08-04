"""Production assembly boundary for the autonomous strategy platform."""
from __future__ import annotations

from src.db import ai_watchlist_repository as watchlist_repository
from src.db import ai_execution_repository as execution_repository
from src.db import ai_risk_repository as risk_repository

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace
from typing import Any, Mapping


from src.db.strategy_repository import load_ai_strategies
from src.config import config

# Preserve the historical patch seam while sourcing every operation from its
# bounded repository. Production code no longer imports the monolithic facade.
ai_stock_repository = SimpleNamespace(
    get_policy=watchlist_repository.get_policy,
    list_strategy_positions=execution_repository.list_strategy_positions,
    count_daily_new_risk_managed_orders=execution_repository.count_daily_new_risk_managed_orders,
    list_active_reserved_exposures=risk_repository.list_active_reserved_exposures,
)

from .ai_planner import (
    AutonomousAIAdapter,
    DemoRulePlanner,
    OpenAIResponsesPlanner,
    PlannerProvider,
)
from .lifecycle import StrategyLifecycleGate
from .orchestrator import (
    AutonomousStrategyOrchestrator,
    CycleResult,
    MarketContext,
    PortfolioContext,
)
from .order_state import ManagedOrderService
from .protection import PaperProtectionBroker, UnavailableProtectionBroker
from .risk_envelope import RiskEnvelope, RiskLimits, RiskSnapshot


class RuntimeConfigurationError(RuntimeError):
    """Required trusted configuration or snapshot data is unavailable."""


@dataclass(frozen=True)
class RuntimeResult:
    cycle: CycleResult
    managed_orders: tuple[Mapping[str, Any], ...]


class AutonomyRuntime:
    """Build trusted snapshots and create risk-approved managed orders only."""

    def __init__(
        self,
        *,
        planner_provider: PlannerProvider | None = None,
        order_service: ManagedOrderService | None = None,
    ):
        self.planner_provider = planner_provider
        self.order_service = order_service or ManagedOrderService(
            protection_broker=_operational_protection_broker()
        )

    def run(
        self,
        *,
        cycle_key: str,
        strategy_id: str,
        market: str,
        account_snapshot: Mapping[str, Any],
        market_snapshot: Mapping[str, Any],
    ) -> RuntimeResult:
        _require_runtime_mode()
        strategy = _require_strategy(strategy_id)
        policy = ai_stock_repository.get_policy(strategy_id, market)
        if not policy or not int(policy.get("enabled", 0)):
            raise RuntimeConfigurationError("enabled automation policy is required")
        profile = strategy.get("profile")
        if not isinstance(profile, Mapping):
            raise RuntimeConfigurationError("strategy profile is required")
        risk_config = profile.get("risk")
        if not isinstance(risk_config, Mapping):
            raise RuntimeConfigurationError("strategy risk profile is required")

        market_context, portfolio_context = build_runtime_contexts(
            market=market,
            strategy_id=strategy_id,
            account_snapshot=account_snapshot,
            market_snapshot=market_snapshot,
        )
        limits = _risk_limits(policy, profile, risk_config)
        if self.planner_provider is not None:
            provider = self.planner_provider
        elif (
            str(strategy.get("model") or "none").lower() == "none"
        ):
            provider = DemoRulePlanner()
        else:
            provider = OpenAIResponsesPlanner(model=strategy.get("model"))
        adapter = AutonomousAIAdapter(
            strategy_id=strategy_id,
            strategy_version=int(strategy["strategy_version"]),
            profile_hash=str(strategy.get("profile_hash") or ""),
            provider=provider,
            strategy_instructions=str(strategy.get("description") or ""),
        )
        orchestrator = AutonomousStrategyOrchestrator(
            RiskEnvelope(limits),
            lifecycle_gate=StrategyLifecycleGate(),
        )
        cycle = orchestrator.run_cycle(
            cycle_key=cycle_key,
            adapter=adapter,
            market=market_context,
            portfolio=portfolio_context,
        )

        orders: list[Mapping[str, Any]] = []
        for result in cycle.results:
            if result.status != "managed_order_created" or result.order_id is None:
                continue
            # Risk has already approved the exact authoritative quantity.  The
            # state service records that fact; it never submits to a broker here.
            orders.append(
                MappingProxyType(
                    dict(self.order_service.mark_risk_approved(result.order_id))
                )
            )
        return RuntimeResult(cycle, tuple(orders))


def run_autonomous_strategy(**kwargs: Any) -> RuntimeResult:
    """Public replacement entrypoint for legacy level-6 automation."""
    return AutonomyRuntime().run(**kwargs)


def _operational_protection_broker():
    """Select an explicit protection boundary; never silently omit it."""
    autonomy_env = str(
        getattr(config, "autonomy_trading_env", "demo")
    ).lower()
    trading_env = str(getattr(config, "trading_env", "demo")).lower()
    if bool(config.dry_run) or trading_env in {"demo", "paper"}:
        return PaperProtectionBroker()
    if (
        autonomy_env == "real"
        and trading_env == "real"
        and bool(getattr(config, "enable_live_trading", False))
        and bool(getattr(config, "autonomy_enable_live_trading", False))
        and bool(getattr(config, "autonomy_live_opt_in", False))
    ):
        # Neither current market integration exposes a durable broker-native
        # hard-stop adapter.  Never substitute the in-memory paper broker in a
        # live process: it cannot observe the account and loses state on restart.
        return UnavailableProtectionBroker(
            "live autonomous hard-stop adapter is not implemented"
        )
    return UnavailableProtectionBroker(
        "autonomous protection requires matching environment and live opt-ins"
    )


def build_runtime_contexts(
    *,
    market: str,
    strategy_id: str,
    account_snapshot: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    exclude_reservation_id: int | None = None,
) -> tuple[MarketContext, PortfolioContext]:
    """Copy caller data into immutable, validated cycle snapshots."""
    market = str(market).strip().upper()
    if market not in {"KR", "US"}:
        raise RuntimeConfigurationError("market must be KR or US")
    account = _freeze_mapping(account_snapshot, "account_snapshot")
    snapshot = _freeze_mapping(market_snapshot, "market_snapshot")
    if not bool(account.get("available", False)):
        raise RuntimeConfigurationError("trusted account snapshot is unavailable")

    evaluated_at = _aware_time(snapshot.get("evaluated_at"), "evaluated_at")
    data_as_of = _aware_time(snapshot.get("data_as_of"), "market data_as_of")
    account_as_of = _aware_time(account.get("data_as_of"), "account data_as_of")
    if account_as_of > evaluated_at or data_as_of > evaluated_at:
        raise RuntimeConfigurationError("snapshot timestamps cannot be in the future")

    candidates = snapshot.get("candidates")
    instruments = snapshot.get("instruments")
    if not isinstance(candidates, tuple) or not isinstance(instruments, Mapping):
        raise RuntimeConfigurationError("candidates and instruments are required")
    symbols = {
        str(item.get("symbol") or "")
        for item in candidates
        if isinstance(item, Mapping)
    }
    active = ai_stock_repository.list_strategy_positions(
        market=market,
        strategy_id=_required_text({"strategy_id": strategy_id}, "strategy_id"),
        active_only=True,
    )
    symbols.update(str(item.get("symbol") or "") for item in active)
    account_id = _required_text(account, "account_id")
    kst = timezone(timedelta(hours=9))
    trading_day = evaluated_at.astimezone(kst)
    day_start = trading_day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    daily_new_risk_orders = (
        ai_stock_repository.count_daily_new_risk_managed_orders(
            account_id=account_id,
            market=market,
            strategy_id=strategy_id,
            day_start=day_start.isoformat(),
            day_end=day_end.isoformat(),
        )
    )
    reservations = ai_stock_repository.list_active_reserved_exposures(
        account_id=account_id, market=market
    )
    if exclude_reservation_id is not None:
        reservations = [
            item
            for item in reservations
            if int(item.get("id") or 0) != int(exclude_reservation_id)
        ]
    symbols.update(str(item.get("symbol") or "") for item in reservations)
    if not symbols or "" in symbols:
        raise RuntimeConfigurationError("every candidate/position requires a symbol")

    total_equity = _positive(account, "total_equity")
    available_cash = _nonnegative(account, "available_cash")
    daily_pnl = _number(account, "daily_pnl")
    market_exposure = _nonnegative(account, "market_exposure_value")
    strategy_exposure = _nonnegative(account, "strategy_exposure_value")
    open_position_risk = _nonnegative(
        account, "open_position_risk_amount_excluding_reservations"
    )
    if "protection_global_block" not in account:
        raise RuntimeConfigurationError("protection_global_block is required")
    holdings = account.get("holdings")
    if not isinstance(holdings, Mapping):
        raise RuntimeConfigurationError("account holdings mapping is required")
    regime = str(snapshot.get("regime") or "").strip()
    if not regime:
        raise RuntimeConfigurationError("market regime is required")

    reservation_market = sum(
        _nonnegative(item, "pending_exposure_value") for item in reservations
    )
    reservation_by_symbol: dict[str, float] = {}
    reservation_by_strategy: dict[str, float] = {}
    reservation_by_sector: dict[str, float] = {}
    for item in reservations:
        reserved_symbol = _required_text(item, "symbol")
        value = _nonnegative(item, "pending_exposure_value")
        reservation_by_symbol[reserved_symbol] = (
            reservation_by_symbol.get(reserved_symbol, 0.0) + value
        )
        reserved_strategy = _required_text(item, "strategy_id")
        reservation_by_strategy[reserved_strategy] = (
            reservation_by_strategy.get(reserved_strategy, 0.0) + value
        )
        reserved_instrument = instruments.get(reserved_symbol)
        if not isinstance(reserved_instrument, Mapping):
            raise RuntimeConfigurationError(
                f"reserved instrument snapshot missing for {reserved_symbol}"
            )
        reserved_sector = _required_text(reserved_instrument, "sector")
        reservation_by_sector[reserved_sector] = (
            reservation_by_sector.get(reserved_sector, 0.0) + value
        )

    risk_snapshots: dict[str, RiskSnapshot] = {}
    for symbol in symbols:
        instrument = instruments.get(symbol)
        if not isinstance(instrument, Mapping):
            raise RuntimeConfigurationError(f"instrument snapshot missing for {symbol}")
        holding = holdings.get(symbol, {})
        if not isinstance(holding, Mapping):
            raise RuntimeConfigurationError(f"holding snapshot invalid for {symbol}")
        sector = _required_text(instrument, "sector")
        risk_snapshots[symbol] = RiskSnapshot(
            total_equity=total_equity,
            available_cash=available_cash,
            daily_pnl=daily_pnl,
            position_value=_nonnegative(holding, "value", default=0.0),
            market_exposure_value=market_exposure,
            sector_exposure_value=_nonnegative(
                instrument, "sector_exposure_value"
            ),
            strategy_exposure_value=strategy_exposure,
            reserved_symbol_exposure_value=reservation_by_symbol.get(symbol, 0.0),
            reserved_market_exposure_value=reservation_market,
            reserved_sector_exposure_value=reservation_by_sector.get(sector, 0.0),
            reserved_strategy_exposure_value=reservation_by_strategy.get(
                strategy_id, 0.0
            ),
            sector_key=sector,
            average_daily_trading_value=_positive(
                instrument, "average_daily_trading_value"
            ),
            open_position_risk_amount_excluding_reservations=open_position_risk,
            current_position_qty=int(_nonnegative(holding, "quantity", default=0)),
            market_regime=regime,
            data_as_of=_aware_time(instrument.get("data_as_of"), f"{symbol} data_as_of"),
            evaluated_at=evaluated_at,
            kill_switch_active=bool(account.get("kill_switch_active", False)),
            account_snapshot_available=True,
            current_price=_positive(instrument, "current_price"),
            protection_global_block=bool(account["protection_global_block"]),
            daily_new_risk_orders=daily_new_risk_orders,
        )

    market_context = MarketContext(
        market=market,
        regime=regime,
        data_as_of=data_as_of,
        evaluated_at=evaluated_at,
        snapshot_id=_required_text(snapshot, "snapshot_id"),
        features=MappingProxyType(
            {"candidates": candidates, "snapshot": snapshot}
        ),
    )
    portfolio_context = PortfolioContext(
        account_id=account_id,
        snapshot_id=_required_text(account, "snapshot_id"),
        risk_snapshots=MappingProxyType(risk_snapshots),
        position_quantities=MappingProxyType(
            {
                str(item["id"]): int(
                    _nonnegative(item, "remaining_qty", default=0)
                )
                for item in active
            }
        ),
    )
    return market_context, portfolio_context


def _require_strategy(strategy_id: str) -> Mapping[str, Any]:
    strategy = next(
        (
            item
            for item in load_ai_strategies()
            if str(item.get("id")) == str(strategy_id)
        ),
        None,
    )
    if not strategy:
        raise RuntimeConfigurationError("registered strategy is required")
    for field in ("strategy_version", "profile_hash", "model"):
        if strategy.get(field) in (None, ""):
            raise RuntimeConfigurationError(f"strategy {field} is required")
    return strategy


def _require_runtime_mode() -> None:
    if not bool(getattr(config, "autonomy_enabled", False)):
        raise RuntimeConfigurationError("AUTONOMY_ENABLED=true is required")
    environment = str(
        getattr(config, "autonomy_trading_env", "demo") or ""
    ).strip().lower()
    if environment not in {"demo", "real"}:
        raise RuntimeConfigurationError("AUTONOMY_TRADING_ENV must be demo or real")
    if environment == "real":
        live_guards = (
            bool(getattr(config, "autonomy_enable_live_trading", False)),
            bool(getattr(config, "autonomy_live_opt_in", False)),
            bool(getattr(config, "enable_live_trading", False)),
            str(getattr(config, "trading_env", "demo")).lower() == "real",
            not bool(getattr(config, "dry_run", True)),
        )
        if not all(live_guards):
            raise RuntimeConfigurationError(
                "real autonomy requires explicit local and global live-trading opt-in"
            )


def _risk_limits(policy, profile, risk) -> RiskLimits:
    policy_required = (
        "max_risk_per_trade_pct",
        "max_daily_loss_pct",
        "max_daily_orders",
        "max_position_pct",
        "max_market_exposure_pct",
    )
    required = (
        "max_sector_exposure_pct",
        "max_liquidity_participation_pct",
        "max_strategy_exposure_pct",
        "max_total_open_risk_pct",
        "max_data_age_seconds",
        "min_cash_reserve_pct",
    )
    missing = [key for key in policy_required if policy.get(key) is None]
    missing.extend(key for key in required if risk.get(key) is None)
    regimes = profile.get("market_regime_filter")
    if missing or not isinstance(regimes, (list, tuple)) or not regimes:
        raise RuntimeConfigurationError(
            "complete risk limits and market_regime_filter are required"
        )
    return RiskLimits(
        max_risk_per_trade_pct=float(policy["max_risk_per_trade_pct"]),
        max_daily_loss_pct=float(policy["max_daily_loss_pct"]),
        max_daily_orders=int(policy["max_daily_orders"]),
        max_position_pct=float(policy["max_position_pct"]),
        max_market_exposure_pct=float(policy["max_market_exposure_pct"]),
        max_sector_exposure_pct=float(risk["max_sector_exposure_pct"]),
        max_liquidity_participation_pct=float(
            risk["max_liquidity_participation_pct"]
        ),
        max_strategy_exposure_pct=float(risk["max_strategy_exposure_pct"]),
        max_total_open_risk_pct=float(risk["max_total_open_risk_pct"]),
        max_data_age_seconds=int(risk["max_data_age_seconds"]),
        allowed_regimes=frozenset(str(item) for item in regimes),
        min_cash_reserve_pct=float(risk["min_cash_reserve_pct"]),
    )


def _freeze_mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeConfigurationError(f"{name} must be a mapping")
    return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _aware_time(value: Any, name: str) -> datetime:
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigurationError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeConfigurationError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _number(mapping, key):
    try:
        value = float(mapping[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeConfigurationError(f"{key} must be numeric") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise RuntimeConfigurationError(f"{key} must be finite")
    return value


def _positive(mapping, key):
    value = _number(mapping, key)
    if value <= 0:
        raise RuntimeConfigurationError(f"{key} must be positive")
    return value


def _nonnegative(mapping, key, default=None):
    if key not in mapping and default is not None:
        return default
    value = _number(mapping, key)
    if value < 0:
        raise RuntimeConfigurationError(f"{key} must be nonnegative")
    return value


def _required_text(mapping, key):
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise RuntimeConfigurationError(f"{key} is required")
    return value
