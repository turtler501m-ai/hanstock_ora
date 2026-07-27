from datetime import datetime, timedelta, timezone
import unittest

from src.strategy.autonomy.regime import (
    MarketRegime,
    MarketRegimeClassifier,
    MarketRegimeInput,
    PortfolioBuildInput,
    SnapshotBuildError,
    TrustedSnapshotContextProvider,
)
from src.strategy.autonomy.risk_envelope import RiskSnapshot


NOW = datetime(2026, 7, 23, 6, 0, tzinfo=timezone.utc)


def indicators(**changes):
    values = {
        "market": "KR",
        "data_as_of": NOW,
        "evaluated_at": NOW,
        "index_price": 110,
        "sma20": 105,
        "sma60": 100,
        "sma200": 90,
        "return_5d": 0.02,
        "return_20d": 0.08,
        "realized_volatility": 0.12,
        "baseline_volatility": 0.12,
        "advance_ratio": 0.62,
        "drawdown_20d": -0.03,
        "source": "exchange",
    }
    values.update(changes)
    return MarketRegimeInput(**values)


class MarketBuilder:
    def __init__(self, value):
        self.value = value

    def build_market_input(self, market):
        return self.value


class PortfolioBuilder:
    def build_portfolio_input(self, market):
        risk = RiskSnapshot(
            total_equity=1_000_000,
            available_cash=500_000,
            daily_pnl=0,
            position_value=0,
            market_exposure_value=0,
            sector_exposure_value=0,
            strategy_exposure_value=0,
            reserved_symbol_exposure_value=0,
            reserved_market_exposure_value=0,
            reserved_sector_exposure_value=0,
            reserved_strategy_exposure_value=0,
            sector_key="semiconductor",
            average_daily_trading_value=10_000_000,
            open_position_risk_amount_excluding_reservations=0,
            current_position_qty=0,
            market_regime="untrusted",
            data_as_of=NOW,
            evaluated_at=NOW,
            kill_switch_active=False,
        )
        return PortfolioBuildInput(
            account_id="A1",
            market=market,
            source="broker",
            data_as_of=NOW,
            cash=500_000,
            total_eval=1_000_000,
            stock_eval=500_000,
            risk_snapshots={"005930": risk},
            payload={"holdings": []},
        )


class Persistence:
    def __init__(self):
        self.market = []
        self.portfolio = []

    def create_market_snapshot(self, data):
        self.market.append(data)
        return 11

    def create_portfolio_snapshot(self, data):
        self.portfolio.append(data)
        return 22


class MarketRegimeClassifierTest(unittest.TestCase):
    def setUp(self):
        self.classifier = MarketRegimeClassifier()

    def assert_regime(self, expected, **changes):
        decision = self.classifier.classify(indicators(**changes))
        self.assertEqual(decision.regime, expected)
        self.assertTrue(decision.fresh)

    def test_classifies_all_supported_quantitative_regimes(self):
        cases = (
            (MarketRegime.BULL, {}),
            (
                MarketRegime.BULL_PULLBACK,
                {"index_price": 102, "sma20": 105, "return_5d": -0.02},
            ),
            (
                MarketRegime.SIDEWAYS_LOW_VOL,
                {
                    "index_price": 100, "sma20": 101, "sma60": 100,
                    "sma200": 99, "return_5d": 0, "return_20d": 0,
                    "advance_ratio": 0.5, "realized_volatility": 0.08,
                },
            ),
            (
                MarketRegime.SIDEWAYS_HIGH_VOL,
                {
                    "index_price": 100, "sma20": 101, "sma60": 100,
                    "sma200": 99, "return_5d": 0, "return_20d": 0,
                    "advance_ratio": 0.5, "realized_volatility": 0.18,
                },
            ),
            (
                MarketRegime.BEAR_RALLY,
                {
                    "index_price": 88, "sma20": 85, "sma60": 90, "sma200": 100,
                    "return_5d": 0.03, "return_20d": -0.08,
                    "advance_ratio": 0.5,
                },
            ),
            (
                MarketRegime.BEAR,
                {
                    "index_price": 80, "sma20": 85, "sma60": 90, "sma200": 100,
                    "return_5d": -0.02, "return_20d": -0.08,
                    "advance_ratio": 0.35,
                },
            ),
            (MarketRegime.CRASH, {"return_5d": -0.09}),
        )
        for expected, changes in cases:
            with self.subTest(expected=expected):
                self.assert_regime(expected, **changes)

    def test_missing_non_finite_and_stale_input_are_unknown(self):
        cases = (
            indicators(sma200=float("nan")),
            indicators(baseline_volatility=0),
            indicators(data_as_of=NOW - timedelta(seconds=301)),
        )
        for value in cases:
            with self.subTest(value=value):
                decision = self.classifier.classify(value)
                self.assertEqual(decision.regime, MarketRegime.UNKNOWN)
                self.assertFalse(decision.fresh)

    def test_context_provider_persists_inputs_and_overrides_untrusted_regime(self):
        persistence = Persistence()
        provider = TrustedSnapshotContextProvider(
            MarketBuilder(indicators()),
            PortfolioBuilder(),
            persistence=persistence,
        )
        market = provider.market_context("KR")
        portfolio = provider.portfolio_context("KR")
        self.assertEqual(market.regime, "bull")
        self.assertEqual(market.snapshot_id, "11")
        self.assertEqual(portfolio.snapshot_id, "22")
        self.assertEqual(
            portfolio.risk_snapshot_for("005930").market_regime,
            "bull",
        )
        self.assertEqual(persistence.market[0]["regime"], "bull")

    def test_unknown_regime_fails_closed_before_snapshot_persistence(self):
        persistence = Persistence()
        provider = TrustedSnapshotContextProvider(
            MarketBuilder(indicators(sma200=float("nan"))),
            PortfolioBuilder(),
            persistence=persistence,
        )
        with self.assertRaisesRegex(SnapshotBuildError, "regime unavailable"):
            provider.market_context("KR")
        self.assertEqual(persistence.market, [])


if __name__ == "__main__":
    unittest.main()
