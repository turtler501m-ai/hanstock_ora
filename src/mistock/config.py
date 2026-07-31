from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


if os.environ.get("HANSTOCK_TESTING") != "1":
    load_dotenv(override=True)


@dataclass
class MistockConfig:
    market: str = os.environ.get("MISTOCK_MARKET", "NASDAQ")
    trading_env: str = os.environ.get("MISTOCK_TRADING_ENV", "demo")
    dry_run: bool = os.environ.get("MISTOCK_DRY_RUN", "true").lower() in {"1", "true", "yes", "on"}
    enable_live_trading: bool = os.environ.get("MISTOCK_ENABLE_LIVE_TRADING", "false").lower() in {"1", "true", "yes", "on"}
    require_approval: bool = os.environ.get("MISTOCK_REQUIRE_APPROVAL", "true").lower() in {"1", "true", "yes", "on"}
    total_capital: float = float(os.environ.get("MISTOCK_TOTAL_CAPITAL", "100000"))
    cash_buffer: float = float(os.environ.get("MISTOCK_CASH_BUFFER", "0.20"))
    max_positions: int = int(os.environ.get("MISTOCK_MAX_POSITIONS", "5"))
    max_single_weight: float = float(os.environ.get("MISTOCK_MAX_SINGLE_WEIGHT", "0.25"))
    max_daily_loss_pct: float = float(os.environ.get("MISTOCK_MAX_DAILY_LOSS_PCT", "3.0"))
    split_n: int = int(os.environ.get("MISTOCK_SPLIT_N", "7"))
    stop_loss_pct: float = float(os.environ.get("MISTOCK_STOP_LOSS_PCT", "-12"))
    take_profit: float = float(os.environ.get("MISTOCK_TAKE_PROFIT", "25"))
    rsi_buy: int = int(os.environ.get("MISTOCK_RSI_BUY", "35"))
    rsi_sell: int = int(os.environ.get("MISTOCK_RSI_SELL", "72"))
    trailing_stop_activation_pct: float = float(os.environ.get("MISTOCK_TRAILING_STOP_ACTIVATION_PCT", "10"))
    trailing_stop_pct: float = float(os.environ.get("MISTOCK_TRAILING_STOP_PCT", "7"))
    trailing_stop_lookback: int = int(os.environ.get("MISTOCK_TRAILING_STOP_LOOKBACK", "20"))
    trade_value_surge_ratio: float = float(os.environ.get("MISTOCK_TRADE_VALUE_SURGE_RATIO", "1.5"))
    first_wave_min_pct: float = float(os.environ.get("MISTOCK_FIRST_WAVE_MIN_PCT", "12"))
    first_wave_pullback_min_pct: float = float(os.environ.get("MISTOCK_FIRST_WAVE_PULLBACK_MIN_PCT", "3"))
    first_wave_pullback_max_pct: float = float(os.environ.get("MISTOCK_FIRST_WAVE_PULLBACK_MAX_PCT", "12"))
    strategy_model: str = os.environ.get("MISTOCK_STRATEGY_MODEL", "default")
    indicator_min_score: int = int(os.environ.get("MISTOCK_INDICATOR_MIN_SCORE", "4"))
    indicator_rsi_entry_min: int = int(os.environ.get("MISTOCK_INDICATOR_RSI_ENTRY_MIN", "50"))
    indicator_rsi_entry_max: int = int(os.environ.get("MISTOCK_INDICATOR_RSI_ENTRY_MAX", "70"))
    indicator_volume_ratio: float = float(os.environ.get("MISTOCK_INDICATOR_VOLUME_RATIO", "1.3"))
    scan_universe_size: int = int(os.environ.get("MISTOCK_SCAN_UNIVERSE_SIZE", "100"))
    yfinance_timeout_seconds: int = int(os.environ.get("MISTOCK_YFINANCE_TIMEOUT_SECONDS", "10"))
    currency: str = os.environ.get("MISTOCK_CURRENCY", "USD")
    trade_db_path: Path = Path(os.environ.get("MISTOCK_TRADE_DB_PATH", ".runtime/mistock/trades.sqlite"))
    usdkrw_fallback_rate: float = float(os.environ.get("USDKRW_FALLBACK_RATE", "1380.0"))
    universe_list: list[str] = None

    def __post_init__(self):
        default_universe = (
            "AAPL,MSFT,NVDA,AMZN,META,GOOGL,GOOG,TSLA,AVGO,COST,"
            "NFLX,AMD,PEP,ADBE,CSCO,TMUS,INTU,QCOM,AMAT,TXN,"
            "ISRG,AMGN,HON,BKNG,VRTX,SBUX,ADP,PANW,MU,LRCX,"
            "GILD,MDLZ,ADI,KLAC,MELI,REGN,CRWD,PYPL,CDNS,SNPS,"
            "MAR,CSX,ORLY,ABNB,FTNT,NXPI,MRVL,ROP,PCAR,ADSK,"
            "CHTR,WDAY,MNST,KDP,PAYX,AEP,TEAM,ROST,KHC,FAST,"
            "ASML,AZN,NVO,PDD,MCHP,CTAS,IDXX,CPRT,ODFL,VRSK,"
            "CSGP,LULU,EXC,XEL,BKR,GEHC,MCO,ALGN,DDOG,"
            "DXCM,EA,FANG,ILMN,MRNA,VRSN,ZS,CTSH,CDW,FITB,"
            "HBAN,JCI,KEYS,NVR,PTC,VFC,WDC,WYNN,ZBRA,EBAY"
        )
        raw_univ = os.environ.get("MISTOCK_UNIVERSE", default_universe)
        self.universe_list = [s.strip().upper() for s in raw_univ.split(",") if s.strip()]


config = MistockConfig()
