import unittest
from unittest.mock import patch

from src.config import Settings
from src.db.strategy_repository import normalize_ai_strategy
from src.strategy.autonomy.runtime import (
    RuntimeConfigurationError,
    _require_runtime_mode,
)


class AutonomyOperatingConfigTests(unittest.TestCase):
    def test_safe_autonomy_defaults(self):
        fields = Settings.model_fields

        self.assertFalse(fields["autonomy_enabled"].default)
        self.assertEqual(fields["autonomy_trading_env"].default, "demo")
        self.assertTrue(fields["autonomy_require_approval"].default)
        self.assertFalse(fields["autonomy_enable_live_trading"].default)
        self.assertFalse(fields["autonomy_live_opt_in"].default)

    def test_legacy_profile_receives_conservative_limits_and_is_operational(self):
        strategy = normalize_ai_strategy(
            {
                "id": "legacy",
                "model": "none",
                "selected": True,
                "profile": {
                    "risk": {"max_risk_per_trade_pct": 0.7},
                },
            }
        )

        self.assertEqual(strategy["status"], "approved")
        risk = strategy["profile"]["risk"]
        self.assertEqual(risk["max_risk_per_trade_pct"], 0.7)
        self.assertEqual(risk["max_total_open_risk_pct"], 2.0)
        self.assertEqual(risk["max_sector_exposure_pct"], 20.0)
        self.assertEqual(risk["max_liquidity_participation_pct"], 0.5)
        self.assertEqual(risk["max_strategy_exposure_pct"], 30.0)
        self.assertEqual(risk["max_data_age_seconds"], 60)
        self.assertEqual(risk["min_cash_reserve_pct"], 20.0)
        self.assertTrue(strategy["profile"]["market_regime_filter"])

    def test_real_runtime_requires_separate_explicit_opt_in(self):
        settings = {
            "autonomy_enabled": True,
            "autonomy_trading_env": "real",
            "autonomy_enable_live_trading": True,
            "autonomy_live_opt_in": False,
            "enable_live_trading": True,
            "trading_env": "real",
            "dry_run": False,
        }
        with patch.multiple(
            "src.strategy.autonomy.runtime.config", **settings
        ):
            with self.assertRaises(RuntimeConfigurationError):
                _require_runtime_mode()


if __name__ == "__main__":
    unittest.main()
