"""Deterministic, freshness-aware market regime classification and contexts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from src.db import ai_stock_repository as repository

from .orchestrator import MarketContext, PortfolioContext
from .risk_envelope import RiskSnapshot


class MarketRegime(str, Enum):
    BULL = "bull"
    BULL_PULLBACK = "bull_pullback"
    SIDEWAYS_LOW_VOL = "sideways_low_vol"
    SIDEWAYS_HIGH_VOL = "sideways_high_vol"
    BEAR_RALLY = "bear_rally"
    BEAR = "bear"
    CRASH = "crash"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RegimeThresholds:
    max_data_age_seconds: int = 300
    crash_drawdown: float = -0.12
    crash_return_5d: float = -0.08
    crash_vol_ratio: float = 2.5
    high_vol_ratio: float = 1.3
    bull_breadth: float = 0.55
    bear_breadth: float = 0.45
    sideways_trend_gap: float = 0.05


@dataclass(frozen=True)
class MarketRegimeInput:
    market: str
    data_as_of: datetime
    evaluated_at: datetime
    index_price: float
    sma20: float
    sma60: float
    sma200: float
    return_5d: float
    return_20d: float
    realized_volatility: float
    baseline_volatility: float
    advance_ratio: float
    drawdown_20d: float
    source: str
    extra: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RegimeDecision:
    regime: MarketRegime
    reasons: tuple[str, ...]
    volatility_ratio: float | None
    fresh: bool


class MarketRegimeClassifier:
    def __init__(self, thresholds: RegimeThresholds | None = None):
        self.thresholds = thresholds or RegimeThresholds()

    def classify(self, value: MarketRegimeInput) -> RegimeDecision:
        invalid = self._invalid_reasons(value)
        if invalid:
            return RegimeDecision(MarketRegime.UNKNOWN, tuple(invalid), None, False)
        vol_ratio = value.realized_volatility / value.baseline_volatility
        price = value.index_price
        t = self.thresholds

        if (
            value.drawdown_20d <= t.crash_drawdown
            or value.return_5d <= t.crash_return_5d
            or (vol_ratio >= t.crash_vol_ratio and value.return_5d < -0.04)
        ):
            return RegimeDecision(
                MarketRegime.CRASH,
                ("crash_loss_or_volatility_threshold",),
                vol_ratio,
                True,
            )
        if (
            price > value.sma20 > value.sma60 > value.sma200
            and value.return_20d > 0
            and value.advance_ratio >= t.bull_breadth
            and vol_ratio < t.high_vol_ratio
        ):
            return RegimeDecision(
                MarketRegime.BULL, ("aligned_uptrend",), vol_ratio, True
            )
        if (
            price > value.sma60 > value.sma200
            and price <= value.sma20
            and value.return_20d > 0
            and value.return_5d <= 0
        ):
            return RegimeDecision(
                MarketRegime.BULL_PULLBACK,
                ("long_uptrend_short_pullback",),
                vol_ratio,
                True,
            )
        if (
            price < value.sma20 < value.sma60 < value.sma200
            and value.return_20d < 0
            and value.advance_ratio <= t.bear_breadth
        ):
            return RegimeDecision(
                MarketRegime.BEAR, ("aligned_downtrend",), vol_ratio, True
            )
        if (
            price < value.sma60 < value.sma200
            and price > value.sma20
            and value.return_5d > 0
        ):
            return RegimeDecision(
                MarketRegime.BEAR_RALLY,
                ("short_rebound_inside_long_downtrend",),
                vol_ratio,
                True,
            )
        trend_gap = abs(price / value.sma60 - 1.0)
        if trend_gap <= t.sideways_trend_gap:
            regime = (
                MarketRegime.SIDEWAYS_HIGH_VOL
                if vol_ratio >= t.high_vol_ratio
                else MarketRegime.SIDEWAYS_LOW_VOL
            )
            return RegimeDecision(regime, ("flat_medium_trend",), vol_ratio, True)
        return RegimeDecision(
            MarketRegime.UNKNOWN,
            ("quantitative_regime_conditions_not_met",),
            vol_ratio,
            True,
        )

    def _invalid_reasons(self, value: MarketRegimeInput) -> list[str]:
        reasons: list[str] = []
        if not str(value.market or "").strip():
            reasons.append("market_required")
        if not str(value.source or "").strip():
            reasons.append("source_required")
        for name in ("data_as_of", "evaluated_at"):
            dt = getattr(value, name)
            if not isinstance(dt, datetime) or dt.tzinfo is None:
                reasons.append(f"{name}_must_be_timezone_aware")
        positive = (
            "index_price", "sma20", "sma60", "sma200",
            "realized_volatility", "baseline_volatility",
        )
        for name in positive:
            number = _finite(getattr(value, name))
            if number is None or number <= 0:
                reasons.append(f"{name}_invalid")
        for name in ("return_5d", "return_20d", "drawdown_20d"):
            if _finite(getattr(value, name)) is None:
                reasons.append(f"{name}_invalid")
        breadth = _finite(value.advance_ratio)
        if breadth is None or not 0 <= breadth <= 1:
            reasons.append("advance_ratio_invalid")
        if not reasons:
            age = (
                value.evaluated_at.astimezone(timezone.utc)
                - value.data_as_of.astimezone(timezone.utc)
            ).total_seconds()
            if age < 0 or age > self.thresholds.max_data_age_seconds:
                reasons.append("market_data_stale")
        return reasons


@dataclass(frozen=True)
class PortfolioBuildInput:
    account_id: str
    market: str
    source: str
    data_as_of: datetime
    cash: float
    total_eval: float
    stock_eval: float
    risk_snapshots: Mapping[str, RiskSnapshot]
    payload: Mapping[str, Any]


class MarketSnapshotBuilder(Protocol):
    def build_market_input(self, market: str) -> MarketRegimeInput: ...


class PortfolioSnapshotBuilder(Protocol):
    def build_portfolio_input(self, market: str) -> PortfolioBuildInput: ...


class SnapshotPersistence(Protocol):
    def create_market_snapshot(self, data: dict[str, Any]) -> int: ...

    def create_portfolio_snapshot(self, data: dict[str, Any]) -> int: ...


class SnapshotBuildError(RuntimeError):
    pass


class TrustedSnapshotContextProvider:
    """ContextProvider implementation for ContinuousStrategyService."""

    def __init__(
        self,
        market_builder: MarketSnapshotBuilder,
        portfolio_builder: PortfolioSnapshotBuilder,
        *,
        classifier: MarketRegimeClassifier | None = None,
        persistence: SnapshotPersistence = repository,
    ):
        self.market_builder = market_builder
        self.portfolio_builder = portfolio_builder
        self.classifier = classifier or MarketRegimeClassifier()
        self.persistence = persistence
        self._market_contexts: dict[str, MarketContext] = {}

    def market_context(self, market: str) -> MarketContext:
        raw = self.market_builder.build_market_input(market)
        if str(raw.market).upper() != str(market).upper():
            raise SnapshotBuildError("market snapshot market mismatch")
        decision = self.classifier.classify(raw)
        if decision.regime is MarketRegime.UNKNOWN:
            raise SnapshotBuildError(
                "market regime unavailable: " + ",".join(decision.reasons)
            )
        payload = _jsonable(asdict(raw))
        key = _key("market", raw.market, raw.data_as_of.isoformat(), payload)
        snapshot_id = self.persistence.create_market_snapshot({
            "snapshot_key": key,
            "market": raw.market,
            "source": raw.source,
            "data_as_of": raw.data_as_of.isoformat(),
            "regime": decision.regime.value,
            "payload": {
                **payload,
                "decision": {
                    "regime": decision.regime.value,
                    "reasons": list(decision.reasons),
                    "volatility_ratio": decision.volatility_ratio,
                },
            },
        })
        context = MarketContext(
            market=raw.market,
            regime=decision.regime.value,
            data_as_of=raw.data_as_of,
            evaluated_at=raw.evaluated_at,
            snapshot_id=str(snapshot_id),
            features=payload,
        )
        self._market_contexts[str(market).upper()] = context
        return context

    def portfolio_context(self, market: str) -> PortfolioContext:
        market_key = str(market).upper()
        market_context = self._market_contexts.get(market_key)
        if market_context is None:
            raise SnapshotBuildError("market_context must be built first")
        raw = self.portfolio_builder.build_portfolio_input(market)
        if str(raw.market).upper() != market_key:
            raise SnapshotBuildError("portfolio market mismatch")
        if not raw.account_id or not raw.source or not raw.risk_snapshots:
            raise SnapshotBuildError("portfolio snapshot required fields missing")
        values = (raw.cash, raw.total_eval, raw.stock_eval)
        if any(_finite(item) is None or float(item) < 0 for item in values):
            raise SnapshotBuildError("portfolio values must be finite and non-negative")
        if not isinstance(raw.data_as_of, datetime) or raw.data_as_of.tzinfo is None:
            raise SnapshotBuildError("portfolio data_as_of must be timezone-aware")
        age = (
            market_context.evaluated_at.astimezone(timezone.utc)
            - raw.data_as_of.astimezone(timezone.utc)
        ).total_seconds()
        if age < 0 or age > self.classifier.thresholds.max_data_age_seconds:
            raise SnapshotBuildError("portfolio snapshot is stale")
        snapshots = {
            symbol: replace(
                snapshot,
                market_regime=market_context.regime,
            )
            for symbol, snapshot in raw.risk_snapshots.items()
        }
        payload = {
            **_jsonable(dict(raw.payload)),
            "risk_snapshots": {
                symbol: _jsonable(asdict(snapshot))
                for symbol, snapshot in snapshots.items()
            },
        }
        key = _key(
            "portfolio", raw.account_id, raw.market, raw.data_as_of.isoformat(), payload
        )
        snapshot_id = self.persistence.create_portfolio_snapshot({
            "snapshot_key": key,
            "account_id": raw.account_id,
            "market": raw.market,
            "source": raw.source,
            "data_as_of": raw.data_as_of.isoformat(),
            "cash": raw.cash,
            "total_eval": raw.total_eval,
            "stock_eval": raw.stock_eval,
            "payload": payload,
        })
        return PortfolioContext(raw.account_id, str(snapshot_id), snapshots)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _key(*parts: Any) -> str:
    encoded = json.dumps(_jsonable(parts), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
