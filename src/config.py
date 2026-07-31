from pydantic_settings import BaseSettings, SettingsConfigDict
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv
import os
import threading


_TESTING = os.environ.get("HANSTOCK_TESTING") == "1"

if not _TESTING:
    load_dotenv(override=True)

class Settings(BaseSettings):
    # KIS API Credentials (국내주식)
    kistock_app_key: str = ""
    kistock_app_secret: str = ""
    kistock_account: str = ""
    kistock_hts_id: str = ""
    kis_websocket_enabled: bool = False
    kis_condition_search_enabled: bool = False
    kis_condition_user_id: str = ""
    kis_condition_seq: str = ""
    kis_condition_name: str = ""
    kis_real_check_enabled: bool = False
    kis_real_check_app_key: str = ""
    kis_real_check_app_secret: str = ""
    kis_real_check_account: str = ""
    kis_real_check_hts_id: str = ""
    kis_real_check_condition_search_enabled: bool = False
    kis_real_check_condition_user_id: str = ""
    kis_real_check_condition_seq: str = ""
    kis_real_check_condition_name: str = ""

    # LS Securities API
    ls_api_enabled: bool = False
    ls_app_key: str = ""
    ls_app_secret: str = ""
    ls_account_no: str = ""
    ls_trading_env: str = "demo"

    # KIS 해외선물 모의계좌
    kis_futures_demo_app_key: Optional[str] = None
    kis_futures_demo_app_secret: Optional[str] = None
    kis_futures_demo_account: Optional[str] = None

    # KIS 해외선물 실계좌
    kis_futures_real_app_key: Optional[str] = None
    kis_futures_real_app_secret: Optional[str] = None
    kis_futures_real_account: Optional[str] = None

    # Telegram
    telegram_api_id: Optional[str] = None
    telegram_api_hash: Optional[str] = None
    telegram_target_channels: Optional[str] = None

    # Notifications
    slack_webhook_url: Optional[str] = ""
    mistock_slack_webhook_url: Optional[str] = ""
    
    # Trading Modes
    trading_env: str = "demo"
    dry_run: bool = True
    enable_live_trading: bool = False
    require_approval: bool = True
    online_access_blocked: bool = False
    
    # Strategy Params
    split_n: int = 7
    stop_loss_pct: float = -10.0
    take_profit: float = 30.0
    rsi_buy: int = 30
    rsi_sell: int = 70
    trailing_stop_activation_pct: float = 10.0
    trailing_stop_pct: float = 6.0
    trailing_stop_lookback: int = 20
    
    # Risk Management
    total_capital: float = 10000000.0
    # Display-only account baseline. It must not affect order sizing or risk limits.
    account_initial_capital: float = 0.0
    max_positions: int = 3
    max_single_weight: float = 0.30
    cash_buffer: float = 0.20
    max_daily_loss_pct: float = 30.0
    # Comma-separated Korean stock symbols to exclude from automated orders and scans.
    hanstock_excluded_symbols: str = ""
    
    # Others
    scan_universe_size: int = 50
    yfinance_timeout_seconds: int = 25
    candidate_scan_source: str = "yfinance"
    kis_circuit_cooldown_seconds: int = 60
    trade_db_path: str = ".runtime/trades.sqlite"
    log_file: str = "logs/trader.log"
    active_model_version: str = "v1"
    ai_strategy_enabled: bool = False
    ai_score_weight: float = 0.40
    ai_min_model_confidence: float = 0.60
    ai_require_backtest_pass: bool = True
    ai_auto_approve: bool = False
    ai_min_rule_score: float = 1.5
    ai_allow_candidate_promotion: bool = False
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_timeout_seconds: float = 20.0
    ai_candidate_limit: int = 5

    # Autonomous strategy platform is a separate, explicit safety boundary.
    autonomy_enabled: bool = False
    autonomy_trading_env: str = "demo"
    autonomy_require_approval: bool = True
    autonomy_enable_live_trading: bool = False
    autonomy_live_opt_in: bool = False

    model_config = SettingsConfigDict(
        env_file=None if _TESTING else ".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

config = Settings()

_settings_lock = threading.RLock()


@dataclass(frozen=True)
class TradingFlags:
    trading_env: str
    dry_run: bool
    enable_live_trading: bool
    require_approval: bool
    online_access_blocked: bool
    order_submission_enabled: bool
    real_orders_enabled: bool


def get_settings() -> Settings:
    return config


def trading_flags(settings: Settings | None = None) -> TradingFlags:
    current = settings or get_settings()
    real_orders_enabled = (
        not current.online_access_blocked
        and not current.dry_run
        and current.trading_env == "real"
        and current.enable_live_trading
    )
    order_submission_enabled = (
        not current.online_access_blocked
        and not current.dry_run
        and (current.trading_env == "demo" or real_orders_enabled)
    )
    return TradingFlags(
        trading_env=current.trading_env,
        dry_run=current.dry_run,
        enable_live_trading=current.enable_live_trading,
        require_approval=current.require_approval,
        online_access_blocked=current.online_access_blocked,
        order_submission_enabled=order_submission_enabled,
        real_orders_enabled=real_orders_enabled,
    )


def apply_env_updates(updates: dict[str, str]) -> Settings:
    with _settings_lock:
        previous = {key: os.environ.get(key) for key in updates}
        try:
            for key, value in updates.items():
                os.environ[key] = str(value)
            refreshed = Settings()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        field_names = set(Settings.model_fields)
        for key in updates:
            field_name = key.lower()
            if field_name in field_names:
                setattr(config, field_name, getattr(refreshed, field_name))
        return config
