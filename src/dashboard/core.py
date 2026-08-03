import json
import hashlib
import concurrent.futures
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

if os.environ.get("HANSTOCK_TESTING") != "1":
    load_dotenv()

from src import trader  # noqa: E402
from src.config import apply_env_updates  # noqa: E402
from src.trader import KIStockAPI  # noqa: E402
from src.api.kis_api import KISAccountError, KISConfigError, KISRateLimitError  # noqa: E402
from src.api.quantconnect_api import QuantConnectAPI, QuantConnectCredentials  # noqa: E402
from src.futures_signals import (  # noqa: E402
    FuturesSignalParseError,
    FuturesSignalService,
    OhlcCandle,
    TelegramSignalCollector,
    collector_status,
)
from src.notifier.slack import slack_order as _slack_order, slack_error as _slack_error  # noqa: E402
from src.online_access import OnlineAccessBlockedError  # noqa: E402
from src.runtime_state import PersistentRuntimeState  # noqa: E402
from src.dashboard.services.cache_service import DashboardCacheService  # noqa: E402
from src.dashboard.services.futures_service import FuturesDashboardService  # noqa: E402
from src.dashboard.services.scheduler_service import DashboardSchedulerService  # noqa: E402
from src.dashboard.services.external_service import ExternalIntegrationService  # noqa: E402
from src.dashboard.services.stock_service import DashboardStockService  # noqa: E402
from src.dashboard.services.order_history_service import (
    _broker_order_id_from_history,
    _history_action,
    _history_fill_price,
    _history_fill_qty,
    _history_int,
    _history_matches_tracked_order,
    _history_name,
    _history_order_is_canceled,
    _history_order_is_rejected,
    _history_remaining_qty,
    _history_row_to_trade,
    _history_symbol,
    _history_text,
    _history_timestamp,
    _history_trade_key,
)
from src.dashboard.services.analysis_cycle_service import (  # noqa: E402
    AnalysisCycleError,
    get_common_analysis_stage,
    load_or_capture_common_stage,
    mark_common_analysis_stage,
    resolve_common_analysis_cycle,
)
from src.dashboard.services.balance_service import (  # noqa: E402
    clamp_ratio,
    holding_value,
    parse_balance,
    portfolio_totals,
    summary_item,
    to_float,
    to_int,
)
from src.dashboard.services.auth_service import (  # noqa: E402
    dashboard_auth_config as _dashboard_auth_config,
    dashboard_basic_credentials as _dashboard_basic_credentials,
    require_dashboard_auth,
)
from src.dashboard.services.env_service import (  # noqa: E402
    env_bool_value,
    env_value_without_inline_comment,
    expand_virtual_env_updates,
    read_env_values,
    serialize_env_value,
    virtual_env_value,
    write_env_values,
)
from src.strategy.seven_split import adjust_tick_size  # noqa: E402
from src.utils.logger import logger  # noqa: E402


def _json_safe_value(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return value


class SafeJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return super().render(_json_safe_value(content))


app = FastAPI(
    title="Seven Split Dashboard",
    version="1.0.0",
    default_response_class=SafeJSONResponse,
)
DashboardOperationError = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    ImportError,
    sqlite3.Error,
    subprocess.SubprocessError,
    KISAccountError,
    KISConfigError,
    KISRateLimitError,
    FuturesSignalParseError,
    OnlineAccessBlockedError,
)


@app.exception_handler(OnlineAccessBlockedError)
async def online_access_blocked_handler(_request: Request, exc: OnlineAccessBlockedError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.middleware("http")
async def require_dashboard_auth(request: Request, call_next):
    from src.dashboard.services.auth_service import require_dashboard_auth as authenticate

    return await authenticate(request, call_next)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / "data"
DB_PATH = trader.DB_PATH
FINRL_DIR = BASE_DIR / "vendor" / "FinRL"
BALANCE_CACHE = trader.RUNTIME_DIR / "balance_snapshot.json"
CANDIDATE_CACHE = trader.RUNTIME_DIR / "candidate_snapshot.json"
AUTO_APPROVAL_STATE = trader.RUNTIME_DIR / "auto_approval.json"
DEFAULT_AUTO_APPROVAL_STATE = AUTO_APPROVAL_STATE
AUTO_APPROVAL_EXCLUDED_SOURCES = {"narrative_momentum", "autonomous_strategy"}
QUANTCONNECT_MNQ_DIR = BASE_DIR / "src" / "integrations" / "quantconnect" / "mnq_paper_auto"
QUANTCONNECT_MNQ_RESULTS = trader.RUNTIME_DIR / "quantconnect_mnq_results.json"
QUANTCONNECT_AUTH_CACHE = trader.RUNTIME_DIR / "quantconnect_auth_cache.json"
QUANTCONNECT_CLOUD_CACHE = trader.RUNTIME_DIR / "quantconnect_cloud_cache.json"
ENV_PATH = BASE_DIR / ".env"
CANDIDATE_CACHE_TTL_SECONDS = int(os.environ.get("CANDIDATE_CACHE_TTL_SECONDS", "180"))
BALANCE_CACHE_TTL_SECONDS = int(os.environ.get("BALANCE_CACHE_TTL_SECONDS", "30"))
# 대시보드 탭 read-through 스냅샷의 기본 신선도 TTL(초). 이 시간 안에는 API를
# 호출하지 않고 DB 스냅샷을 그대로 돌려준다. 만료되면 builder(API)로 재생성한다.
DASHBOARD_SNAPSHOT_TTL_SECONDS = int(os.environ.get("DASHBOARD_SNAPSHOT_TTL_SECONDS", "20"))
BALANCE_FETCH_TIMEOUT_SECONDS = float(os.environ.get("BALANCE_FETCH_TIMEOUT_SECONDS", "25"))
GIT_FETCH_TIMEOUT_SECONDS = float(os.environ.get("GIT_FETCH_TIMEOUT_SECONDS", "3"))
MIN_ORDER_HISTORY_SYNC_DAYS = 30
_balance_fetch_lock = threading.Lock()
ENV_FIELDS = [
    {"key": "KIS_REAL_CHECK_ENABLED", "label": "KIS real_check Enabled", "type": "bool", "hint": "Use real KIS API for market-data checks only; balance and orders keep using the execution account."},
    {"key": "KIS_REAL_CHECK_APP_KEY", "label": "KIS real_check App Key", "type": "secret"},
    {"key": "KIS_REAL_CHECK_APP_SECRET", "label": "KIS real_check App Secret", "type": "secret"},
    {"key": "KIS_REAL_CHECK_ACCOUNT", "label": "KIS real_check Account", "type": "text"},
    {"key": "KIS_REAL_CHECK_HTS_ID", "label": "KIS real_check HTS ID", "type": "text"},
    {"key": "KIS_REAL_CHECK_CONDITION_SEARCH_ENABLED", "label": "KIS real_check Condition Enabled", "type": "bool"},
    {"key": "KIS_REAL_CHECK_CONDITION_USER_ID", "label": "KIS real_check Condition User ID", "type": "text"},
    {"key": "KIS_REAL_CHECK_CONDITION_SEQ", "label": "KIS real_check Condition Seq", "type": "text"},
    {"key": "KIS_REAL_CHECK_CONDITION_NAME", "label": "KIS real_check Condition Name", "type": "text"},
    {"key": "KISTOCK_APP_KEY", "label": "KIS App Key", "type": "secret"},
    {"key": "KISTOCK_APP_SECRET", "label": "KIS App Secret", "type": "secret"},
    {"key": "KISTOCK_ACCOUNT", "label": "KIS Account", "type": "text", "hint": "계좌번호 8자리 또는 계좌번호 8자리 + 상품코드 2자리. 예: 12345678 또는 1234567801"},
    {"key": "KISTOCK_HTS_ID", "label": "KIS HTS ID", "type": "text", "hint": "실시간 주문체결 통보와 조건검색식 조회에 사용할 HTS ID입니다."},
    {"key": "KIS_WEBSOCKET_ENABLED", "label": "KIS WebSocket Enabled", "type": "bool", "hint": "true이면 서버에서 KIS 실시간 주문체결 통보 웹소켓을 시작할 수 있습니다."},
    {"key": "KIS_CONDITION_SEARCH_ENABLED", "label": "KIS Condition Search Enabled", "type": "bool", "hint": "true이면 매수 후보 스캔 유니버스에 KIS 조건검색식 결과를 우선 반영합니다."},
    {"key": "KIS_CONDITION_USER_ID", "label": "KIS Condition User ID", "type": "text", "hint": "조건검색식 API 조회용 사용자 ID입니다. 비워두면 KISTOCK_HTS_ID를 사용합니다."},
    {"key": "KIS_CONDITION_SEQ", "label": "KIS Condition Seq", "type": "text", "hint": "HTS에 저장된 조건검색식 일련번호입니다."},
    {"key": "KIS_CONDITION_NAME", "label": "KIS Condition Name", "type": "text", "hint": "HTS에 저장된 조건검색식 이름입니다."},
    {"key": "TRADING_ENV", "label": "거래 환경", "type": "select", "options": ["demo", "real"], "hint": "demo=모의투자, real=실전투자"},
    {"key": "DRY_RUN", "label": "주문 차단", "type": "bool", "hint": "true이면 KIS 주문 API 전송을 막고 계획과 기록만 생성합니다."},
    {"key": "ENABLE_LIVE_TRADING", "label": "실전매매 최종 허용", "type": "bool", "hint": "실전 주문을 허용하는 최종 안전 스위치입니다."},
    {"key": "REQUIRE_APPROVAL", "label": "주문 승인 필요", "type": "bool"},
    {"key": "ONLINE_ACCESS_BLOCKED", "label": "Online Access Blocked", "type": "bool", "hint": "true이면 외부 API, 웹소켓, 주문 실행을 차단하고 DB에 저장된 정보만 표시합니다."},
    {"key": "SPLIT_N", "label": "Split N", "type": "int"},
    {"key": "STOP_LOSS_PCT", "label": "Stop Loss %", "type": "float"},
    {"key": "TAKE_PROFIT", "label": "Take Profit %", "type": "float"},
    {"key": "RSI_BUY", "label": "RSI Buy", "type": "int"},
    {"key": "RSI_SELL", "label": "RSI Sell", "type": "int"},
    {"key": "TRAILING_STOP_ACTIVATION_PCT", "label": "Trailing Stop Activation %", "type": "float"},
    {"key": "TRAILING_STOP_PCT", "label": "Trailing Stop Drawdown %", "type": "float"},
    {"key": "TRAILING_STOP_LOOKBACK", "label": "Trailing Stop Lookback", "type": "int"},
    {"key": "TRADE_VALUE_SURGE_RATIO", "label": "Trade Value Surge Ratio", "type": "float"},
    {"key": "FIRST_WAVE_MIN_PCT", "label": "First Wave Minimum %", "type": "float"},
    {"key": "FIRST_WAVE_PULLBACK_MIN_PCT", "label": "First Wave Pullback Minimum %", "type": "float"},
    {"key": "FIRST_WAVE_PULLBACK_MAX_PCT", "label": "First Wave Pullback Maximum %", "type": "float"},
    {"key": "TOTAL_CAPITAL", "label": "Total Capital", "type": "float"},
    {"key": "ACCOUNT_INITIAL_CAPITAL", "label": "Account Initial Capital", "type": "float", "hint": "계좌 전체 손익 표시 기준입니다. 주문 규모에는 영향을 주지 않습니다."},
    {"key": "MAX_POSITIONS", "label": "Max Positions", "type": "int"},
    {"key": "MAX_SINGLE_WEIGHT", "label": "Max Single Weight", "type": "float"},
    {"key": "CASH_BUFFER", "label": "Cash Buffer", "type": "float"},
    {"key": "MAX_DAILY_LOSS_PCT", "label": "Max Daily Loss %", "type": "float"},
    {"key": "HANSTOCK_EXCLUDED_SYMBOLS", "label": "Hanstock Excluded Symbols", "type": "text", "hint": "Comma-separated domestic stock codes excluded from automated scans and orders."},
    {"key": "KIS_ORDER_MIN_INTERVAL_SECONDS", "label": "KIS Order Min Interval Seconds", "type": "float", "hint": "Minimum wait between broker order submissions."},
    {"key": "SCAN_UNIVERSE_SIZE", "label": "Scan Universe Size", "type": "int"},
    {"key": "KIS_CIRCUIT_COOLDOWN_SECONDS", "label": "KIS API 차단 대기시간", "type": "int", "hint": "KIS API 오류 후 재시도까지 기다리는 시간(초)입니다. 대시보드 재시작 후 적용됩니다."},
    {"key": "TRADE_DB_PATH", "label": "Trade DB Path", "type": "text"},
    {"key": "ACTIVE_MODEL_VERSION", "label": "Active Model Version", "type": "text"},
    {"key": "AI_STRATEGY_ENABLED", "label": "AI Strategy Enabled", "type": "bool"},
    {"key": "AI_SCORE_WEIGHT", "label": "AI Score Weight", "type": "float"},
    {"key": "AI_MIN_MODEL_CONFIDENCE", "label": "AI Min Confidence", "type": "float"},
    {"key": "AI_REQUIRE_BACKTEST_PASS", "label": "AI Require Backtest Pass", "type": "bool"},
    {"key": "AI_AUTO_APPROVE", "label": "AI Auto Approve", "type": "bool"},
    {"key": "AI_MIN_RULE_SCORE", "label": "AI Min Rule Score", "type": "float"},
    {"key": "AI_ALLOW_CANDIDATE_PROMOTION", "label": "AI Allow Candidate Promotion", "type": "bool"},
    {"key": "OPENAI_API_KEY", "label": "OpenAI API Key", "type": "secret"},
    {"key": "OPENAI_MODEL", "label": "OpenAI Model", "type": "text"},
    {"key": "OPENAI_TIMEOUT_SECONDS", "label": "OpenAI Timeout Seconds", "type": "float"},
    {"key": "AI_CANDIDATE_LIMIT", "label": "AI Candidate Limit", "type": "int"},
    {"key": "SLACK_WEBHOOK_URL", "label": "Slack Webhook URL", "type": "secret"},
    {"key": "MISTOCK_SLACK_WEBHOOK_URL", "label": "Mistock Slack Webhook URL", "type": "secret"},
    {"key": "TELEGRAM_API_ID", "label": "Telegram API ID", "type": "secret"},
    {"key": "TELEGRAM_API_HASH", "label": "Telegram API Hash", "type": "secret"},
    {"key": "TELEGRAM_SESSION_NAME", "label": "Telegram Session Name", "type": "text", "hint": "Local Telethon session path. Keep it out of git."},
    {"key": "TELEGRAM_TARGET_CHANNELS", "label": "Telegram Target Channels", "type": "text", "hint": "Comma-separated channel usernames, IDs, or invite targets."},
    {"key": "MISTOCK_EXCHANGE_MAP", "label": "Mistock Exchange Map", "type": "text", "hint": "미국주식 거래소 매핑입니다. 예: BRK.B=NYSE,TSLA=NASD"},
    {"key": "MISTOCK_CURRENCY", "label": "Mistock Currency", "type": "text", "hint": "미스톡 대시보드 표기 통화입니다. 예: USD, KRW"},
    {"key": "MISTOCK_MARKET", "label": "Mistock Market", "type": "text", "hint": "미국주식 타겟 시장입니다. 예: NASDAQ"},
    {"key": "MISTOCK_TRADING_ENV", "label": "Mistock Trading Env", "type": "select", "options": ["paper", "demo", "real"], "hint": "paper=가상모의, demo=실제모의, real=실전매매"},
    {"key": "MISTOCK_DRY_RUN", "label": "Mistock Dry Run (주문차단)", "type": "bool", "hint": "true이면 실제 KIS 미국주식 주문 API를 호출하지 않습니다."},
    {"key": "MISTOCK_ENABLE_LIVE_TRADING", "label": "Mistock Enable Live Trading", "type": "bool", "hint": "미국주식 실전매매 최종허용 안전스위치입니다."},
    {"key": "MISTOCK_REQUIRE_APPROVAL", "label": "Mistock Require Approval", "type": "bool", "hint": "true이면 미국주식 주문 시 승인 대기를 거칩니다."},
    {"key": "MISTOCK_TOTAL_CAPITAL", "label": "Mistock Total Capital", "type": "float", "hint": "미국주식 총 운용 자금입니다. 단위는 MISTOCK_CURRENCY 값을 따릅니다."},
    {"key": "MISTOCK_TRADE_DB_PATH", "label": "Mistock Trade DB Path", "type": "text", "hint": "미국주식 거래 기록용 SQLite DB 경로입니다."},
    {"key": "USDKRW_FALLBACK_RATE", "label": "USD/KRW Fallback Rate", "type": "float", "hint": "yfinance 환율 수집 실패 시 사용할 고정/기본 환율입니다. 기본값: 1380.0"},
    {"key": "MISTOCK_UNIVERSE", "label": "미스톡 기본 스캔 유니버스", "type": "text", "hint": "미국주식 스캔 시 사용할 기본 관심종목 목록(쉼표 구분)입니다. 기본값: 60종목"},
]
ENV_FIELD_MAP = {field["key"]: field for field in ENV_FIELDS}
VENDOR_PROJECTS = {
    "finrl": {
        "name": "FinRL",
        "path": BASE_DIR / "vendor" / "FinRL",
        "package": "finrl",
        "dashboard": "/finrl",
        "license_hint": "MIT",
        "adapter": "Weight-centric allocation for current KIS holdings",
        "entrypoints": [
            "finrl/train.py",
            "finrl/test.py",
            "finrl/trade.py",
            "finrl/meta/env_stock_trading/env_stocktrading.py",
            "finrl/agents/stablebaselines3/models.py",
        ],
    },
    "qlib": {
        "name": "Qlib",
        "path": BASE_DIR / "vendor" / "qlib",
        "package": "qlib",
        "dashboard": "/vendors",
        "license_hint": "MIT",
        "adapter": "AI quant research pipeline map: dataset, feature, model, signal, execution",
        "entrypoints": [
            "qlib/workflow",
            "qlib/model",
            "qlib/contrib",
            "qlib/backtest",
            "examples",
        ],
    },
    "pyportfolioopt": {
        "name": "PyPortfolioOpt",
        "path": BASE_DIR / "vendor" / "PyPortfolioOpt",
        "package": "pypfopt",
        "dashboard": "/vendors",
        "license_hint": "MIT",
        "adapter": "Portfolio target weights and risk-aware rebalance planning",
        "entrypoints": [
            "pypfopt/efficient_frontier",
            "pypfopt/risk_models",
            "pypfopt/expected_returns",
            "pypfopt/objective_functions",
        ],
    },
    "freqtrade": {
        "name": "freqtrade",
        "path": BASE_DIR / "vendor" / "freqtrade",
        "package": "freqtrade",
        "dashboard": "/vendors",
        "license_hint": "GPL-3.0",
        "adapter": "Dry-run, approval workflow, strategy status concepts only; source kept isolated",
        "entrypoints": [
            "freqtrade/strategy",
            "freqtrade/rpc",
            "freqtrade/persistence",
            "freqtrade/freqai",
            "user_data/strategies",
        ],
    },
}

app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
app.mount("/templates", StaticFiles(directory=WEB_DIR / "templates"), name="templates")


@app.middleware("http")
async def _disable_dashboard_cache(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if (
        path in {"/", "/mistock", "/static/js/app.js", "/static/js/mistock_app.js"}
        or path.startswith("/api/performance")
        or path.startswith("/api/mistock/")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        if "ETag" in response.headers:
            del response.headers["ETag"]
        if "Last-Modified" in response.headers:
            del response.headers["Last-Modified"]
    return response


def _public_override(name: str, current):
    module = sys.modules.get("src.dashboard")
    if module is None:
        return None
    value = getattr(module, name, None)
    if value is not None and value is not current:
        return value
    return None


def _public_value(name: str, default):
    module = sys.modules.get("src.dashboard")
    if module is None:
        return default
    return getattr(module, name, default)


_external_integration_service = ExternalIntegrationService(
    env_path_fn=lambda: _public_value("ENV_PATH", ENV_PATH),
    auth_cache_path_fn=lambda: _public_value(
        "QUANTCONNECT_AUTH_CACHE",
        QUANTCONNECT_AUTH_CACHE,
    ),
    now_fn=lambda: trader.datetime.now(trader.KST),
)


def _required_env_missing() -> list[str]:
    override = _public_override("_required_env_missing", _required_env_missing)
    if override is not None:
        return override()
    required = ["KISTOCK_APP_KEY", "KISTOCK_APP_SECRET", "KISTOCK_ACCOUNT"]
    missing = [name for name in required if not os.environ.get(name)]
    if _account_format_warning(trader.config.kistock_account):
        missing.append("KISTOCK_ACCOUNT_FORMAT")
    return missing


def _account_format_warning(account: str) -> str:
    digits = "".join(char for char in str(account or "") if char.isdigit())
    if not digits:
        return "KISTOCK_ACCOUNT is required"
    if len(digits) not in {8, 10}:
        return "KISTOCK_ACCOUNT must be 8 digits, or 10 digits including 2-digit product code"
    return ""


def _to_int(value, default: int = 0) -> int:
    return to_int(value, default)


def _to_float(value, default: float = 0.0) -> float:
    return to_float(value, default)


def _summary_item(summary):
    return summary_item(summary)


def _clamp_ratio(value: float) -> float:
    return clamp_ratio(value)


def _holding_value(stock: dict, qty: int, price: int) -> int:
    return holding_value(stock, qty, price)


def _portfolio_totals(cash: int, summary_total: int, holdings: list[dict]) -> dict:
    return portfolio_totals(cash, summary_total, holdings)


def _parse_balance(balance_data: dict) -> dict:
    override = _public_override("_parse_balance", _parse_balance)
    if override is not None:
        return override(balance_data)
    return parse_balance(balance_data)


def _get_api() -> KIStockAPI:
    override = _public_override("_get_api", _get_api)
    if override is not None:
        return override()
    return KIStockAPI(notify_errors=False)


def _account_cache_key() -> str:
    source = f"{trader.TRADING_ENV}:{trader.config.kistock_account}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _save_balance_cache(balance_data: dict) -> None:
    _dashboard_cache_service.save_balance(balance_data)


# 잔고(보유/현금)에 의존하는 탭 스냅샷들. 주문/매도 등으로 잔고가 바뀌면 함께 무효화한다.
_BALANCE_DERIVED_SNAPSHOT_KINDS = (
    "balance",
    "risk_status",
    "ai_allocation",
    "portfolio_optimizer",
    "signals",
    "execution_plan",
)

_dashboard_cache_service = DashboardCacheService(
    BALANCE_CACHE,
    account_key_fn=_account_cache_key,
    trading_env_fn=lambda: trader.TRADING_ENV,
    captured_at_fn=lambda: trader.datetime.now(trader.KST).isoformat(),
    derived_kinds=_BALANCE_DERIVED_SNAPSHOT_KINDS,
)
stock_service = DashboardStockService()


def _clear_balance_cache() -> None:
    _dashboard_cache_service.clear_balance()


def _balance_envelope_to_data(cached) -> dict | None:
    """파일/DB 어느 쪽 envelope든 동일하게 검증해 잔고 data를 복원한다."""
    return _dashboard_cache_service.balance_envelope_to_data(cached)


def _load_balance_cache() -> dict | None:
    return _dashboard_cache_service.load_balance()


def _snapshot_age_seconds(captured_at: str) -> float | None:
    if not captured_at:
        return None
    try:
        return (trader.datetime.now(trader.KST) - trader.datetime.fromisoformat(captured_at)).total_seconds()
    except DashboardOperationError:
        return None


def snapshot_read_through(
    kind: str,
    builder,
    *,
    ttl: int | None = None,
    account_scoped: bool = True,
    env: str | None = None,
):
    """대시보드 탭 데이터의 DB-우선 read-through.

    1) DB 스냅샷이 TTL 안이면 API 없이 그대로 반환(_snapshot.stale=False)
    2) 만료/부재면 builder()(=API 호출)로 재생성하고 DB에 write-back
    3) builder 실패 시 마지막 DB 스냅샷을 stale 표시로 반환, 없으면 예외 전파

    builder()는 dict payload를 반환해야 한다. 반환값에는 `_snapshot` 메타를 덧붙인다.
    """
    from src.db.repository import load_account_snapshot, save_account_snapshot

    ttl = DASHBOARD_SNAPSHOT_TTL_SECONDS if ttl is None else ttl
    env = env or trader.TRADING_ENV
    account_key = _account_cache_key() if account_scoped else "_global_"

    snap = None
    try:
        snap = load_account_snapshot(account_key, env, kind)
    except DashboardOperationError:
        snap = None

    if snap is not None:
        age = _snapshot_age_seconds(snap.get("captured_at", ""))
        from src.online_access import is_online_access_blocked

        if is_online_access_blocked():
            payload = dict(snap["payload"])
            payload["_snapshot"] = {
                "stale": True,
                "captured_at": snap.get("captured_at", ""),
                "source": "db",
                "offline": True,
            }
            return payload
        if age is not None and age < ttl:
            payload = dict(snap["payload"])
            payload["_snapshot"] = {"stale": False, "captured_at": snap.get("captured_at", ""), "source": "db"}
            return payload

    from src.online_access import require_online_access

    require_online_access(f"{kind} refresh")
    try:
        payload = builder()
        if not isinstance(payload, dict):
            return payload
        captured_at = trader.datetime.now(trader.KST).isoformat()
        try:
            save_account_snapshot(account_key, env, kind, payload, captured_at)
        except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as exc:
            logger.warning(f"Failed to persist {kind} snapshot: {exc}")
        result = dict(payload)
        result["_snapshot"] = {"stale": False, "captured_at": captured_at, "source": "live"}
        return result
    except DashboardOperationError as exc:
        if snap is not None:
            payload = dict(snap["payload"])
            payload["_snapshot"] = {
                "stale": True,
                "captured_at": snap.get("captured_at", ""),
                "source": "db",
                "error": f"{type(exc).__name__}: {exc}",
            }
            return payload
        raise


def invalidate_snapshot(kind: str, *, account_scoped: bool = True, env: str | None = None) -> None:
    """주문/승인 등 상태 변경 후 해당 탭 스냅샷을 지워 다음 read에서 즉시 재생성되게 한다."""
    try:
        from src.db.repository import delete_account_snapshot

        env = env or trader.TRADING_ENV
        account_key = _account_cache_key() if account_scoped else "_global_"
        delete_account_snapshot(account_key, env, kind)
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as exc:
        logger.warning(f"Failed to invalidate {kind} snapshot: {exc}")


# ---------------------------------------------------------------------------
# (옵션) 백그라운드 스냅샷 리프레셔
#   대시보드 read가 없어도 DB 스냅샷을 주기적으로 갱신해 항상 따뜻하게 유지한다.
#   트레이딩 API 부하를 피하기 위해 기본 비활성(opt-in)이며,
#   DASHBOARD_SNAPSHOT_REFRESH_ENABLED=true 일 때만 동작한다.
# ---------------------------------------------------------------------------
SNAPSHOT_REFRESH_ENABLED = str(os.environ.get("DASHBOARD_SNAPSHOT_REFRESH_ENABLED", "false")).lower() in (
    "1", "true", "yes", "on",
)
SNAPSHOT_REFRESH_INTERVAL_SECONDS = int(os.environ.get("DASHBOARD_SNAPSHOT_REFRESH_INTERVAL_SECONDS", "60"))
_snapshot_refresher_thread: threading.Thread | None = None
_snapshot_refresher_stop = threading.Event()


def _refresh_balance_snapshot_once() -> None:
    """잔고를 라이브로 한 번 받아 DB 스냅샷에 반영(write-through)한다."""
    from src.online_access import is_online_access_blocked

    if is_online_access_blocked() or _required_env_missing():
        return
    api = _get_api()
    balance_data = _get_balance_data(api, allow_cache=False)
    _parse_balance(balance_data)  # 파싱 검증 (실패 시 저장 안 함)
    # _get_balance_data 성공 시 내부에서 _save_balance_cache가 DB write-through 수행


def _snapshot_refresher_loop() -> None:
    while not _snapshot_refresher_stop.wait(SNAPSHOT_REFRESH_INTERVAL_SECONDS):
        try:
            _refresh_balance_snapshot_once()
        except DashboardOperationError as exc:
            logger.warning(f"snapshot refresher: balance refresh failed: {exc}")


def start_snapshot_refresher() -> bool:
    """백그라운드 리프레셔를 시작한다(이미 켜져 있거나 비활성이면 no-op)."""
    global _snapshot_refresher_thread
    if not SNAPSHOT_REFRESH_ENABLED:
        return False
    if _snapshot_refresher_thread is not None and _snapshot_refresher_thread.is_alive():
        return True
    _snapshot_refresher_stop.clear()
    _snapshot_refresher_thread = threading.Thread(
        target=_snapshot_refresher_loop, name="snapshot-refresher", daemon=True
    )
    _snapshot_refresher_thread.start()
    logger.info(f"snapshot refresher started (interval={SNAPSHOT_REFRESH_INTERVAL_SECONDS}s)")
    return True


# ---------------------------------------------------------------------------
# 자동승인 주기 스위퍼
#   "자동승인" 토글이 켜져 있으면, 어떤 경로(자동매매 cron의 StrategyRouter 등)가
#   만든 대기 승인이든 주기적으로 일괄 승인/실행한다. 토글이 꺼져 있으면 아무 일도
#   하지 않는다(자체 게이트). DASHBOARD_AUTO_APPROVAL_SWEEP_ENABLED=false로 끌 수 있다.
# ---------------------------------------------------------------------------
AUTO_APPROVAL_SWEEP_ENABLED = str(
    os.environ.get("DASHBOARD_AUTO_APPROVAL_SWEEP_ENABLED", "true")
).lower() in ("1", "true", "yes", "on")
AUTO_APPROVAL_SWEEP_INTERVAL_SECONDS = int(
    os.environ.get("DASHBOARD_AUTO_APPROVAL_SWEEP_INTERVAL_SECONDS", "15")
)
# 이 시간(초)보다 오래 'executing'에 머문 승인은 프로세스 중단 등으로 고아가 된 것으로
# 보고 failed 처리한다(정상 승인은 수 초 내 완료되므로 넉넉히 잡는다).
AUTO_APPROVAL_STALE_EXECUTING_SECONDS = int(
    os.environ.get("DASHBOARD_AUTO_APPROVAL_STALE_EXECUTING_SECONDS", "600")
)
_auto_approval_sweeper_thread: threading.Thread | None = None
_auto_approval_sweeper_stop = threading.Event()
_approval_submission_lock = threading.Lock()


def _reclaim_stale_executing_approvals(max_age_seconds: int | None = None) -> int:
    """프로세스 중단 등으로 'executing'에 고아처럼 멈춘 승인을 failed로 정리한다.

    재실행(중복 주문) 위험을 피하기 위해 pending이 아니라 failed로 표시한다.
    """
    from datetime import timedelta

    max_age = AUTO_APPROVAL_STALE_EXECUTING_SECONDS if max_age_seconds is None else max_age_seconds
    now = trader.datetime.now(trader.KST)
    cutoff = (now - timedelta(seconds=max_age)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        _init_approval_db()
        with trader.connect_db() as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = 'failed',
                    response_msg = 'Order was left in Submitting order to broker state; process was interrupted or broker API did not return before timeout. Check KIS order history before retrying.',
                    updated_at = ?
                WHERE status = 'executing' AND updated_at < ?
                """,
                (now.strftime("%Y-%m-%d %H:%M:%S"), cutoff),
            )
            return cursor.rowcount or 0
    except DashboardOperationError as exc:
        logger.warning(f"reclaim stale executing approvals failed: {exc}")
        return 0


def _auto_approval_sweeper_loop() -> None:
    while not _auto_approval_sweeper_stop.wait(AUTO_APPROVAL_SWEEP_INTERVAL_SECONDS):
        try:
            reclaimed = _reclaim_stale_executing_approvals()
            if reclaimed:
                logger.info(f"auto-approval sweeper: reclaimed {reclaimed} stale executing approval(s)")
            if not _auto_approval_enabled():
                continue
            if not _pending_approval_ids(limit=1, exclude_sources=AUTO_APPROVAL_EXCLUDED_SOURCES):
                continue
            processed = _auto_approve_pending_approvals()
            done = [r for r in processed if isinstance(r, dict) and r.get("status") == "executed"]
            if processed:
                logger.info(
                    f"auto-approval sweeper: processed {len(processed)} pending "
                    f"({len(done)} executed)"
                )
        except DashboardOperationError as exc:
            logger.warning(f"auto-approval sweeper failed: {exc}")


def start_auto_approval_sweeper() -> bool:
    """자동승인 주기 스위퍼를 시작한다(비활성이거나 이미 켜져 있으면 no-op)."""
    global _auto_approval_sweeper_thread
    if not AUTO_APPROVAL_SWEEP_ENABLED:
        return False
    if _auto_approval_sweeper_thread is not None and _auto_approval_sweeper_thread.is_alive():
        return True
    _auto_approval_sweeper_stop.clear()
    _auto_approval_sweeper_thread = threading.Thread(
        target=_auto_approval_sweeper_loop, name="auto-approval-sweeper", daemon=True
    )
    _auto_approval_sweeper_thread.start()
    logger.info(
        f"auto-approval sweeper started (interval={AUTO_APPROVAL_SWEEP_INTERVAL_SECONDS}s)"
    )
    return True


def _balance_cache_age_seconds(balance_data: dict) -> float | None:
    cached_at = balance_data.get("_cache", {}).get("cached_at", "")
    if not cached_at:
        return None
    try:
        return (trader.datetime.now(trader.KST) - trader.datetime.fromisoformat(cached_at)).total_seconds()
    except DashboardOperationError:
        return None


def _run_with_timeout(func, timeout_seconds: float):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(func)
    try:
        return future.result(timeout=timeout_seconds)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _get_balance_data(api: KIStockAPI, allow_cache: bool = True) -> dict:
    override = _public_override("_get_balance_data", _get_balance_data)
    if override is not None:
        try:
            return override(api, allow_cache=allow_cache)
        except TypeError:
            return override(api)
    cached = _load_balance_cache() if allow_cache else None
    if allow_cache:
        if cached is not None:
            age = _balance_cache_age_seconds(cached)
            if age is not None and age < BALANCE_CACHE_TTL_SECONDS:
                return cached

    with _balance_fetch_lock:
        if allow_cache:
            cached = _load_balance_cache()
            if cached is not None:
                age = _balance_cache_age_seconds(cached)
                if age is not None and age < BALANCE_CACHE_TTL_SECONDS:
                    return cached
        try:
            balance_data = _run_with_timeout(api.get_balance, BALANCE_FETCH_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            if cached is not None:
                return cached
            raise RuntimeError("KIS balance API timed out")
        except KISConfigError:
            if allow_cache:
                cached = _load_balance_cache()
                if cached is not None:
                    return cached
            raise
        except DashboardOperationError:
            if allow_cache:
                cached = _load_balance_cache()
                if cached is not None:
                    return cached
            raise
        except Exception:
            if allow_cache:
                cached = _load_balance_cache()
                if cached is not None:
                    return cached
            raise
        try:
            parsed_balance = _parse_balance(balance_data)
        except DashboardOperationError:
            if allow_cache:
                cached = _load_balance_cache()
                if cached is not None:
                    return cached
            raise
        except Exception:
            if allow_cache:
                cached = _load_balance_cache()
                if cached is not None:
                    return cached
            raise
        try:
            from src.db.performance_repository import record_account_equity_snapshot
            summary_hash = hashlib.sha256(
                json.dumps(balance_data.get("output2") or {}, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            record_account_equity_snapshot(
                total_equity=float(parsed_balance.get("total_eval") or 0),
                cash=float(parsed_balance.get("cash") or 0),
                stock_value=float(parsed_balance.get("stock_eval") or 0),
                source="kis_balance",
                raw_summary_hash=summary_hash,
            )
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            logger.warning(f"Failed to persist account equity snapshot: {exc}")
        _save_balance_cache(balance_data)
        return balance_data


def _candidate_strategy_cache_signature(ranker: str) -> dict | None:
    try:
        from src.db.repository import load_ai_strategies

        strategy = next((item for item in load_ai_strategies() if item.get("id") == ranker), None)
    except DashboardOperationError:
        strategy = None
    if not strategy:
        return None
    return {
        "strategy_id": strategy.get("id"),
        "strategy_version": int(strategy.get("strategy_version") or 1),
        "profile_hash": strategy.get("profile_hash") or "",
    }


def _get_candidate_cache_path(ranker: str, optimizer: str):
    """전략·옵티마이저 조합별로 독립된 캐시 파일 경로를 반환한다.

    CANDIDATE_CACHE가 테스트용 MemoryCachePath로 교체된 경우에는
    그 객체를 그대로 반환하여 기존 테스트 패턴과의 호환성을 유지한다.
    """
    if not isinstance(CANDIDATE_CACHE, Path):
        return CANDIDATE_CACHE
    safe = re.sub(r"[^\w-]", "_", f"{ranker}__{optimizer}")
    return CANDIDATE_CACHE.parent / f"candidate_snapshot_{safe}.json"


def _load_candidate_cache(
    min_score: int,
    ranker: str = "gpt_5_mini",
    optimizer: str = "score_tilted_inverse_vol",
    allow_stale: bool = False,
) -> dict | None:
    override = _public_override("_load_candidate_cache", _load_candidate_cache)
    if override is not None:
        if ranker == "gpt_5_mini" and optimizer == "score_tilted_inverse_vol":
            return override(min_score)
        return override(min_score, ranker, optimizer)

    # 1) 파일 캐시 우선 (테스트의 MemoryCachePath 포함)
    cache_path = _get_candidate_cache_path(ranker, optimizer)
    try:
        if cache_path.exists():
            result = _candidate_envelope_to_result(
                json.loads(cache_path.read_text(encoding="utf-8")),
                min_score,
                ranker,
                optimizer,
                allow_stale=allow_stale,
            )
            if result is not None:
                return result
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(f"Failed to read candidate cache: {exc}")

    # 2) DB 스냅샷 폴백 (.runtime 유실/재배포 등으로 파일이 없을 때)
    try:
        from src.db.repository import load_account_snapshot

        snap = load_account_snapshot(
            "_candidates_", trader.TRADING_ENV, _candidate_snapshot_kind(min_score, ranker, optimizer)
        )
        if snap is not None:
            return _candidate_envelope_to_result(
                snap["payload"], min_score, ranker, optimizer, allow_stale=allow_stale
            )
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as exc:
        logger.warning(f"Failed to load candidate snapshot: {exc}")
    return None


def _candidate_snapshot_kind(min_score: int, ranker: str, optimizer: str) -> str:
    return f"candidates:{ranker}:{optimizer}:{min_score}"


def _candidate_envelope_to_result(
    cached, min_score: int, ranker: str, optimizer: str, *, allow_stale: bool = False
) -> dict | None:
    """파일/DB 어느 쪽 envelope든 동일하게 검증해 후보 결과를 복원한다."""
    if not isinstance(cached, dict):
        return None
    expected_ai_signature = {
        "enabled": bool(getattr(trader.config, "ai_strategy_enabled", False)),
        "model": getattr(trader.config, "openai_model", "gpt-5-mini"),
        "candidate_limit": int(getattr(trader.config, "ai_candidate_limit", 5) or 5),
        "api_configured": bool(str(getattr(trader.config, "openai_api_key", "") or "").strip()),
        "strategy": _candidate_strategy_cache_signature(ranker),
    }
    if (
        cached.get("trading_env") != trader.TRADING_ENV
        or cached.get("min_score") != min_score
        or cached.get("ranker") != ranker
        or cached.get("optimizer") != optimizer
        or cached.get("ai_signature") != expected_ai_signature
    ):
        return None
    cached_at = cached.get("cached_at")
    if not cached_at:
        return None
    try:
        age = (trader.datetime.now(trader.KST) - trader.datetime.fromisoformat(cached_at)).total_seconds()
    except ValueError:
        return None
    is_stale = age > CANDIDATE_CACHE_TTL_SECONDS
    if is_stale and not allow_stale:
        return None
    rows = cached.get("rows")
    if not isinstance(rows, list):
        return None
    return {
        "candidates": rows,
        "scan_summary": cached.get("scan_summary", []),
        "scanned": cached.get("scanned", len(rows)),
        "min_score": min_score,
        "_cache": {"stale": is_stale, "cached_at": cached_at},
    }


def _save_candidate_cache(
    min_score: int,
    rows: list[dict],
    scan_summary: list[dict],
    scanned: int,
    ranker: str = "gpt_5_mini",
    optimizer: str = "score_tilted_inverse_vol",
) -> str | None:
    override = _public_override("_save_candidate_cache", _save_candidate_cache)
    if override is not None:
        if ranker == "gpt_5_mini" and optimizer == "score_tilted_inverse_vol":
            return override(min_score, rows, scan_summary, scanned)
        return override(min_score, rows, scan_summary, scanned, ranker, optimizer)
    envelope = {
        "cached_at": trader.datetime.now(trader.KST).isoformat(),
        "trading_env": trader.TRADING_ENV,
        "min_score": min_score,
        "ranker": ranker,
        "optimizer": optimizer,
        "ai_signature": {
            "enabled": bool(getattr(trader.config, "ai_strategy_enabled", False)),
            "model": getattr(trader.config, "openai_model", "gpt-5-mini"),
            "candidate_limit": int(getattr(trader.config, "ai_candidate_limit", 5) or 5),
            "api_configured": bool(str(getattr(trader.config, "openai_api_key", "") or "").strip()),
            "strategy": _candidate_strategy_cache_signature(ranker),
        },
        "rows": rows,
        "scan_summary": scan_summary,
        "scanned": scanned,
    }
    cache_path = _get_candidate_cache_path(ranker, optimizer)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    # DB write-through: 파일 캐시가 유실되어도 마지막 성공본을 DB에서 복구한다.
    try:
        from src.db.repository import save_account_snapshot

        save_account_snapshot(
            "_candidates_",
            trader.TRADING_ENV,
            _candidate_snapshot_kind(min_score, ranker, optimizer),
            envelope,
            envelope["cached_at"],
        )
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as exc:
        logger.warning(f"Failed to persist candidate snapshot: {exc}")
    return envelope["cached_at"]


def _resolve_dashboard_strategy(strategy_id: str | None = None) -> dict | None:
    return stock_service.resolve_dashboard_strategy(strategy_id)


def build_dashboard_signals(api, parsed: dict, strategy_id: str | None = None) -> list[dict]:
    strategy = _resolve_dashboard_strategy(strategy_id)
    return stock_service.build_dashboard_signals(api, parsed, strategy)


def build_dashboard_candidates(
    api,
    parsed: dict,
    min_score: int = 2,
    ranker: str = "gpt_5_mini",
    ranker_weight: float = 0.4,
    optimizer: str = "score_tilted_inverse_vol",
    strategy_model: str = "",
    strategy_profile: dict | None = None,
    strategy_description: str = "",
    universe: list[str] | None = None,
) -> dict:
    return stock_service.build_dashboard_candidates(
        api=api,
        parsed=parsed,
        min_score=min_score,
        ranker=ranker,
        ranker_weight=ranker_weight,
        optimizer=optimizer,
        strategy_model=strategy_model,
        strategy_profile=strategy_profile,
        strategy_description=strategy_description,
        universe=universe,
    )


def _build_candidate_orders_from_scan(candidates: list, *, held_count: int = 0, cash: int) -> list:
    """Build candidate orders using scan prices (no live quote lookup)."""
    available_slots = max(0, trader.MAX_POSITIONS - held_count)
    orders = []
    remaining_cash = cash
    for cand in candidates[:available_slots]:
        price = int(cand.get("current_price", 0) or 0)
        if price <= 0:
            continue
        limit_price = adjust_tick_size(price)
        if limit_price <= 0:
            continue
        qty = remaining_cash // limit_price
        if qty <= 0:
            continue
        estimated_cost = qty * limit_price
        orders.append({
            "ticker": cand["ticker"],
            "limit_price": limit_price,
            "quantity": qty,
            "estimated_cost": estimated_cost,
        })
        remaining_cash -= estimated_cost
    return orders


def build_dashboard_execution_plan(strategy_id: str | None = None) -> dict:
    api = _get_api()
    balance_data = _get_balance_data(api)
    parsed = _parse_balance(balance_data)
    return stock_service.build_dashboard_execution_plan(
        api=api,
        balance_data=balance_data,
        parsed_balance=parsed,
        strategy_id=strategy_id,
    )


def _init_approval_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with trader.connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                action TEXT NOT NULL,
                qty INTEGER NOT NULL,
                price INTEGER NOT NULL,
                reason TEXT,
                source TEXT,
                status TEXT NOT NULL,
                response_msg TEXT
            )
            """
        )
        try:
            from src.db.repository import _ensure_column

            _ensure_column(conn, "approvals", "strategy_id", "TEXT")
            _ensure_column(conn, "approvals", "strategy_version", "INTEGER")
            _ensure_column(conn, "approvals", "profile_hash", "TEXT")
            _ensure_column(conn, "approvals", "source_candidate_id", "INTEGER")
        except sqlite3.DatabaseError as exc:
            logger.warning(f"Failed to migrate approval columns: {exc}")


def _approval_row(row) -> dict:
    return dict(row)


def _approval_by_id(approval_id: int) -> dict | None:
    _init_approval_db()
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return _approval_row(row) if row else None


def _auto_approval_enabled() -> bool:
    # Tests and isolated callers replace the state path. In that case the
    # injected store is authoritative and must not leak the operational DB.
    if AUTO_APPROVAL_STATE != DEFAULT_AUTO_APPROVAL_STATE:
        if not AUTO_APPROVAL_STATE.exists():
            return False
        try:
            state = json.loads(AUTO_APPROVAL_STATE.read_text(encoding="utf-8"))
            return bool(state.get("enabled"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
    try:
        from src.db.repository import load_auto_approval_state
        return load_auto_approval_state()
    except DashboardOperationError:
        if not AUTO_APPROVAL_STATE.exists():
            return False
        try:
            state = json.loads(AUTO_APPROVAL_STATE.read_text(encoding="utf-8"))
            return bool(state.get("enabled"))
        except DashboardOperationError:
            return False


def _save_auto_approval(enabled: bool) -> None:
    try:
        AUTO_APPROVAL_STATE.parent.mkdir(parents=True, exist_ok=True)
        AUTO_APPROVAL_STATE.write_text(
            json.dumps({
                "enabled": bool(enabled),
                "updated_at": trader.datetime.now(trader.KST).isoformat(),
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as exc:
        logger.warning(f"Failed to persist auto approval file: {exc}")
        
    try:
        from src.db.repository import save_auto_approval_state
        save_auto_approval_state(enabled)
    except (sqlite3.DatabaseError, OSError, TypeError, ValueError) as exc:
        logger.warning(f"Failed to persist auto approval state: {exc}")


def _read_env_values(path: Path = ENV_PATH) -> dict[str, str]:
    return read_env_values(path)


def _env_value_without_inline_comment(value: str) -> str:
    return env_value_without_inline_comment(value)


def _mask_env_value(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * max(4, len(value) - 4)}{value[-2:]}"


def _validate_env_value(key: str, value: object) -> str:
    field = ENV_FIELD_MAP[key]
    value_text = _env_value_without_inline_comment(str(value).strip())
    field_type = field["type"]
    if field_type == "bool":
        lowered = value_text.lower()
        if lowered not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise HTTPException(status_code=400, detail=f"{key} must be a boolean")
        return "true" if lowered in {"true", "1", "yes", "on"} else "false"
    if field_type == "int":
        value_text = value_text.replace(",", "")
        try:
            int(value_text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{key} must be an integer") from exc
        return value_text
    if field_type == "float":
        value_text = value_text.replace(",", "")
        try:
            float(value_text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{key} must be a number") from exc
        return value_text
    if field_type == "select":
        options = field.get("options", [])
        if value_text not in options:
            raise HTTPException(status_code=400, detail=f"{key} must be one of: {', '.join(options)}")
        return value_text
    if key == "KISTOCK_ACCOUNT":
        digits = "".join(char for char in value_text if char.isdigit())
        warning = _account_format_warning(digits)
        if warning:
            raise HTTPException(status_code=400, detail=warning)
        return digits
    return value_text


def _env_bool_value(values: dict[str, str], key: str, default: bool = False) -> bool:
    return env_bool_value(values, key, default)


def _virtual_env_value(key: str, values: dict[str, str]) -> str:
    return virtual_env_value(key, values)


def _expand_virtual_env_updates(updates: dict[str, str]) -> dict[str, str]:
    return expand_virtual_env_updates(updates)


def _apply_runtime_env_updates(updates: dict[str, str]) -> None:
    previous_account = trader.config.kistock_account
    previous_app_key = trader.config.kistock_app_key
    previous_app_secret = trader.config.kistock_app_secret
    apply_env_updates({
        key: value
        for key, value in updates.items()
        if not key.startswith("MISTOCK_") and key != "USDKRW_FALLBACK_RATE"
    })
    trader.sync_legacy_config_aliases()

    credentials_changed = (
        previous_account != trader.config.kistock_account
        or previous_app_key != trader.config.kistock_app_key
        or previous_app_secret != trader.config.kistock_app_secret
    )
    if previous_account != trader.config.kistock_account:
        _clear_balance_cache()
    if credentials_changed:
        (Path("data") / "kis_token.json").unlink(missing_ok=True)
        try:
            import src.mistock.trader as mistock_trader
        except ImportError:
            mistock_trader = None
        if mistock_trader is not None:
            mistock_trader._kis_client_cache = None

    for key, value in updates.items():
        if key == "MISTOCK_TRADING_ENV":
            from src.mistock.config import config as mistock_config
            mistock_config.trading_env = value
        elif key == "MISTOCK_DRY_RUN":
            from src.mistock.config import config as mistock_config
            parsed = _env_bool_value({"value": value}, "value")
            mistock_config.dry_run = parsed
        elif key == "MISTOCK_ENABLE_LIVE_TRADING":
            from src.mistock.config import config as mistock_config
            parsed = _env_bool_value({"value": value}, "value")
            mistock_config.enable_live_trading = parsed
        elif key == "MISTOCK_REQUIRE_APPROVAL":
            from src.mistock.config import config as mistock_config
            parsed = _env_bool_value({"value": value}, "value")
            mistock_config.require_approval = parsed
        elif key == "MISTOCK_TOTAL_CAPITAL":
            from src.mistock.config import config as mistock_config
            try:
                mistock_config.total_capital = float(value)
            except ValueError:
                pass
        elif key == "MISTOCK_CURRENCY":
            from src.mistock.config import config as mistock_config
            mistock_config.currency = value
        elif key == "MISTOCK_MARKET":
            from src.mistock.config import config as mistock_config
            mistock_config.market = value
        elif key == "MISTOCK_TRADE_DB_PATH":
            from src.mistock.config import config as mistock_config
            mistock_config.trade_db_path = Path(value)
        elif key == "USDKRW_FALLBACK_RATE":
            from src.mistock.config import config as mistock_config
            try:
                mistock_config.usdkrw_fallback_rate = float(value)
                import src.utils.exchange_rate as ex_rate
                ex_rate._USD_KRW_RATE = float(value)
            except (ValueError, ImportError):
                pass
        elif key == "MISTOCK_UNIVERSE":
            from src.mistock.config import config as mistock_config
            try:
                mistock_config.universe_list = [s.strip().upper() for s in str(value).split(",") if s.strip()]
                import src.mistock.strategy as mistock_strat
                mistock_strat.NASDAQ_UNIVERSE.clear()
                mistock_strat.NASDAQ_UNIVERSE.extend(mistock_config.universe_list)
            except Exception:
                pass



STRATEGY_ENV_BINDINGS = {
    "SPLIT_N": ("split_n", "SPLIT_N", int),
    "STOP_LOSS_PCT": ("stop_loss_pct", "STOP_LOSS_PCT", float),
    "TAKE_PROFIT": ("take_profit", "TAKE_PROFIT", float),
    "RSI_BUY": ("rsi_buy", "RSI_BUY", int),
    "RSI_SELL": ("rsi_sell", "RSI_SELL", int),
    "TRAILING_STOP_ACTIVATION_PCT": ("trailing_stop_activation_pct", None, float),
    "TRAILING_STOP_PCT": ("trailing_stop_pct", None, float),
    "TRAILING_STOP_LOOKBACK": ("trailing_stop_lookback", None, int),
    "TRADE_VALUE_SURGE_RATIO": ("trade_value_surge_ratio", None, float),
    "FIRST_WAVE_MIN_PCT": ("first_wave_min_pct", None, float),
    "FIRST_WAVE_PULLBACK_MIN_PCT": ("first_wave_pullback_min_pct", None, float),
    "FIRST_WAVE_PULLBACK_MAX_PCT": ("first_wave_pullback_max_pct", None, float),
    "TOTAL_CAPITAL": ("total_capital", "TOTAL_CAPITAL", float),
    "MAX_POSITIONS": ("max_positions", "MAX_POSITIONS", int),
    "MAX_SINGLE_WEIGHT": ("max_single_weight", "MAX_SINGLE_WEIGHT", float),
    "CASH_BUFFER": ("cash_buffer", "CASH_BUFFER", float),
    "MAX_DAILY_LOSS_PCT": ("max_daily_loss_pct", "MAX_DAILY_LOSS_PCT", float),
    "HANSTOCK_EXCLUDED_SYMBOLS": ("hanstock_excluded_symbols", None, str),
    "SCAN_UNIVERSE_SIZE": ("scan_universe_size", "SCAN_UNIVERSE_SIZE", int),
}


AI_ENV_BINDINGS = {
    "AI_STRATEGY_ENABLED": ("ai_strategy_enabled", lambda value: str(value).lower() in ("1", "true", "yes", "on")),
    "AI_SCORE_WEIGHT": ("ai_score_weight", float),
    "AI_MIN_MODEL_CONFIDENCE": ("ai_min_model_confidence", float),
    "AI_REQUIRE_BACKTEST_PASS": ("ai_require_backtest_pass", lambda value: str(value).lower() in ("1", "true", "yes", "on")),
    "AI_AUTO_APPROVE": ("ai_auto_approve", lambda value: str(value).lower() in ("1", "true", "yes", "on")),
    "OPENAI_API_KEY": ("openai_api_key", str),
    "OPENAI_MODEL": ("openai_model", str),
    "OPENAI_TIMEOUT_SECONDS": ("openai_timeout_seconds", float),
    "AI_CANDIDATE_LIMIT": ("ai_candidate_limit", int),
}


KIS_ENV_BINDINGS = {
    "KISTOCK_HTS_ID": ("kistock_hts_id", str),
    "KIS_WEBSOCKET_ENABLED": ("kis_websocket_enabled", lambda value: str(value).lower() in ("1", "true", "yes", "on")),
    "KIS_CONDITION_SEARCH_ENABLED": ("kis_condition_search_enabled", lambda value: str(value).lower() in ("1", "true", "yes", "on")),
    "KIS_CONDITION_USER_ID": ("kis_condition_user_id", str),
    "KIS_CONDITION_SEQ": ("kis_condition_seq", str),
    "KIS_CONDITION_NAME": ("kis_condition_name", str),
    "KIS_REAL_CHECK_ENABLED": ("kis_real_check_enabled", lambda value: str(value).lower() in ("1", "true", "yes", "on")),
    "KIS_REAL_CHECK_APP_KEY": ("kis_real_check_app_key", str),
    "KIS_REAL_CHECK_APP_SECRET": ("kis_real_check_app_secret", str),
    "KIS_REAL_CHECK_ACCOUNT": ("kis_real_check_account", str),
    "KIS_REAL_CHECK_HTS_ID": ("kis_real_check_hts_id", str),
    "KIS_REAL_CHECK_CONDITION_SEARCH_ENABLED": ("kis_real_check_condition_search_enabled", lambda value: str(value).lower() in ("1", "true", "yes", "on")),
    "KIS_REAL_CHECK_CONDITION_USER_ID": ("kis_real_check_condition_user_id", str),
    "KIS_REAL_CHECK_CONDITION_SEQ": ("kis_real_check_condition_seq", str),
    "KIS_REAL_CHECK_CONDITION_NAME": ("kis_real_check_condition_name", str),
}


def _apply_strategy_env_updates(updates: dict[str, str]) -> None:
    for key, value in updates.items():
        if key == "ACCOUNT_INITIAL_CAPITAL":
            trader.config.account_initial_capital = float(value)
            continue
        binding = STRATEGY_ENV_BINDINGS.get(key)
        if binding:
            config_attr, trader_attr, caster = binding
            parsed = caster(value)
            setattr(trader.config, config_attr, parsed)
            if trader_attr:
                setattr(trader, trader_attr, parsed)
            continue
        ai_binding = AI_ENV_BINDINGS.get(key)
        if ai_binding:
            config_attr, caster = ai_binding
            setattr(trader.config, config_attr, caster(value))
            continue
        kis_binding = KIS_ENV_BINDINGS.get(key)
        if kis_binding:
            config_attr, caster = kis_binding
            setattr(trader.config, config_attr, caster(value))
            continue
        if key == "MISTOCK_EXCHANGE_MAP":
            os.environ[key] = value


def _ai_analysis_config() -> dict:
    model_name = getattr(trader.config, "openai_model", "gpt-5-mini")
    api_key = str(getattr(trader.config, "openai_api_key", "") or "").strip()
    ai_enabled = bool(getattr(trader.config, "ai_strategy_enabled", False))
    score_weight = max(0.0, min(1.0, float(getattr(trader.config, "ai_score_weight", 0.0) or 0.0)))
    candidate_limit = int(getattr(trader.config, "ai_candidate_limit", 5) or 5)
    return {
        "enabled": ai_enabled,
        "provider": "openai_responses",
        "provider_label": "OpenAI Responses API",
        "model_name": model_name,
        "model_type": "OpenAI text model",
        "model_available": bool(api_key),
        "account_priority": "current_kis_account",
        "account": trader.config.kistock_account,
        "account_label": "현재 KIS 계좌 1순위",
        "openai_account_priority": "openai_api_first",
        "openai_api_configured": bool(api_key),
        "score_weight": score_weight if ai_enabled else 0.0,
        "rule_weight": 1.0 - score_weight if ai_enabled else 1.0,
        "min_confidence": float(getattr(trader.config, "ai_min_model_confidence", 0.6) or 0.6),
        "candidate_limit": candidate_limit,
        "auto_approve": bool(getattr(trader.config, "ai_auto_approve", False)),
        "require_backtest_pass": bool(getattr(trader.config, "ai_require_backtest_pass", True)),
        "fallback_mode": "rule_based" if (not ai_enabled or not api_key) else "",
        "flow": [
            "현재 KIS 계좌의 보유/현금/리스크 상태를 1순위 기준으로 읽습니다.",
            "관심종목과 거래량 상위 종목의 RSI, MACD, Bollinger, 추세, 거래량 피처를 계산합니다.",
            f"AI가 켜져 있고 OPENAI_API_KEY가 있으면 OpenAI Responses API로 상위 {candidate_limit}개 후보만 우선 평가합니다.",
            "최종 점수는 룰 점수와 AI 점수를 AI_SCORE_WEIGHT 비율로 결합합니다.",
            "주문은 승인 대기열과 DRY_RUN/실거래 보호 설정을 통과해야만 처리됩니다.",
        ],
    }



def _runtime_order_mode_updates(key: str, enabled: bool) -> dict[str, str]:
    normalized = key.upper()
    if normalized == "DRY_RUN":
        return {"DRY_RUN": "true" if enabled else "false"}
    raise HTTPException(status_code=400, detail="key must be DRY_RUN")


def _serialize_env_value(value: str) -> str:
    return serialize_env_value(value)


def _write_env_values(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    write_env_values(updates, path)



_FUTURES_SIGNAL_SERVICE: FuturesSignalService | None = None


def _is_poll_running() -> bool:
    """poll.py 프로세스가 실행 중인지 확인"""
    try:
        import psutil
        for proc in psutil.process_iter(['cmdline']):
            cmdline = proc.info.get('cmdline') or []
            if any('poll.py' in str(c) for c in cmdline):
                return True
        return False
    except DashboardOperationError:
        # psutil 없으면 상태 파일로 확인
        state_file = Path(".runtime/poll_running")
        return state_file.exists()


def _get_futures_signal_service() -> FuturesSignalService:
    global _FUTURES_SIGNAL_SERVICE
    public_service = _public_value("_FUTURES_SIGNAL_SERVICE", _FUTURES_SIGNAL_SERVICE)
    if public_service is not _FUTURES_SIGNAL_SERVICE:
        _FUTURES_SIGNAL_SERVICE = public_service
    if _FUTURES_SIGNAL_SERVICE is None:
        service = FuturesSignalService()
        _seed_futures_signal_service(service)
        _FUTURES_SIGNAL_SERVICE = service
        module = sys.modules.get("src.dashboard")
        if module is not None:
            setattr(module, "_FUTURES_SIGNAL_SERVICE", service)
    return _FUTURES_SIGNAL_SERVICE


def _seed_futures_signal_service(service: FuturesSignalService) -> None:
    seed_rows = [
        (
            "tg-sample-001",
            "2026-05-05T09:15:00+09:00",
            "#MNQ M26 LONG\nEntry: 18325.25\nSL 18280\nTP1 18370\nTP2 18420",
            [
                OhlcCandle("2026-05-05T09:16:00+09:00", open=18325.25, high=18362, low=18305, close=18355),
                OhlcCandle("2026-05-05T09:17:00+09:00", open=18355, high=18372, low=18343, close=18368),
            ],
        ),
        (
            "tg-sample-002",
            "2026-05-05T10:05:00+09:00",
            "MCL M26 SELL\nEntry: 64.82\nSL 65.18\nTP1 64.35\nTP2 63.95",
            [
                OhlcCandle("2026-05-05T10:06:00+09:00", open=64.82, high=65.2, low=64.32, close=64.7),
            ],
        ),
        (
            "tg-sample-003",
            "2026-05-05T10:40:00+09:00",
            "GC Q26 LONG\nEntry: 2358.4\nSL 2350\nTP1 2371",
            [
                OhlcCandle("2026-05-05T10:41:00+09:00", open=2358.4, high=2362, low=2349.8, close=2351),
            ],
        ),
    ]
    for message_id, received_at, text, candles in seed_rows:
        record = service.ingest_message(
            text,
            source="telegram_sample",
            source_message_id=message_id,
            received_at=trader.datetime.fromisoformat(received_at),
        )
        service.verify(record.signal.id, candles)


def _futures_signal_public_id(record) -> str:
    return record.signal.source_message_id or record.signal.id


def _futures_signal_confidence(record) -> float:
    if record.verification is None:
        return 0.68
    if record.verification.status == "verified":
        return 0.88
    if record.verification.requires_manual_review:
        return 0.74
    if record.verification.status == "rejected":
        return 0.61
    return 0.7


def _futures_risk_reward(record) -> float | None:
    signal = record.signal
    if not signal.take_profits:
        return None
    risk = abs(signal.entry - signal.stop_loss)
    if risk <= 0:
        return None
    reward = abs(signal.take_profits[0] - signal.entry)
    return round(reward / risk, 2)


def _futures_signal_record_to_api(record) -> dict:
    signal = record.signal
    verification = record.verification
    status = verification.status if verification else signal.status
    return {
        "id": _futures_signal_public_id(record),
        "internal_id": signal.id,
        "received_at": signal.received_at.isoformat() if signal.received_at else None,
        "source": signal.source,
        "channel": "overseas-futures-signals",
        "symbol": signal.symbol,
        "market": _futures_market_name(signal.symbol),
        "side": "buy" if signal.direction == "long" else "sell",
        "direction": signal.direction,
        "entry": signal.entry,
        "entry_price": signal.entry,
        "stop": signal.stop_loss,
        "stop_loss": signal.stop_loss,
        "targets": list(signal.take_profits),
        "take_profit_1": signal.take_profits[0] if signal.take_profits else None,
        "confidence": _futures_signal_confidence(record),
        "parse_status": signal.status,
        "status": status,
        "verification_status": status,
        "verification": {
            "status": status,
            "outcome": verification.outcome if verification else "pending",
            "hit_at": verification.hit_at if verification else None,
            "hit_price": verification.hit_price if verification else None,
            "hit_target_index": verification.hit_target_index if verification else None,
            "reason": verification.reason if verification else "",
            "rule_match": status != "rejected",
            "risk_reward": _futures_risk_reward(record),
            "duplicate": bool(record.metadata.get("duplicate")),
            "requires_manual_review": bool(verification.requires_manual_review) if verification else False,
        },
        "raw_text": signal.raw_text,
    }


_futures_dashboard_service = FuturesDashboardService(
    lambda: trader.datetime.now(trader.KST)
)


def _db_futures_signal_to_api(row: dict) -> dict:
    return _futures_dashboard_service.db_signal_to_api(row)


def _list_db_futures_signals(limit: int | None = 100) -> list[dict]:
    return _futures_dashboard_service.list_persisted_signals(limit)


def _futures_market_name(symbol: str) -> str:
    return _futures_dashboard_service.market_name(symbol)


def _find_futures_signal_record(public_or_internal_id: str):
    service = _get_futures_signal_service()
    direct = service.repository.get(public_or_internal_id)
    if direct is not None:
        return direct
    for record in service.list_records(limit=None):
        if _futures_signal_public_id(record) == public_or_internal_id:
            return record
    return None


def _futures_signals_summary(records: list, *, telegram_connected: bool = False) -> dict:
    return _futures_dashboard_service.summarize(
        records,
        converter=_futures_signal_record_to_api,
        telegram_connected=telegram_connected,
    )


def _read_json_file(path: Path, default):
    return _external_integration_service.read_json(path, default)


def _quantconnect_auth_status(credentials: QuantConnectCredentials) -> dict:
    return _external_integration_service.quantconnect_auth_status(credentials)


def _first_item(value):
    if isinstance(value, list):
        return value[0] if value else {}
    if isinstance(value, dict):
        return value
    return {}


def _quantconnect_errors(*payloads: dict) -> list[str]:
    errors = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for error in payload.get("errors") or []:
            if error:
                errors.append(str(error))
        if payload.get("error"):
            errors.append(str(payload["error"]))
        if payload.get("message") and payload.get("success") is False:
            errors.append(str(payload["message"]))
    return list(dict.fromkeys(errors))


def _quantconnect_order_rows(payload: dict) -> list[dict]:
    orders = payload.get("orders") or payload.get("Orders") or []
    if isinstance(orders, dict):
        orders = list(orders.values())
    rows = []
    for order in orders if isinstance(orders, list) else []:
        if not isinstance(order, dict):
            continue
        symbol = order.get("symbol") or order.get("Symbol") or "MNQ"
        if isinstance(symbol, dict):
            symbol = symbol.get("value") or symbol.get("id") or symbol.get("permtick") or "MNQ"
        direction = order.get("direction") or order.get("side") or order.get("Direction")
        if direction in {0, "0"}:
            direction = "Buy"
        elif direction in {1, "1"}:
            direction = "Sell"
        elif direction is None and (order.get("quantity") or order.get("Quantity") or 0):
            direction = "Buy" if float(order.get("quantity") or order.get("Quantity") or 0) > 0 else "Sell"
        rows.append({
            "id": order.get("id") or order.get("orderId") or order.get("OrderId"),
            "time": order.get("time") or order.get("createdTime") or order.get("lastFillTime") or order.get("Time"),
            "symbol": symbol,
            "side": direction,
            "quantity": order.get("quantity") or order.get("Quantity"),
            "price": order.get("price") or order.get("Price") or order.get("averageFillPrice"),
            "status": order.get("status") or order.get("Status"),
        })
    return rows


def _quantconnect_portfolio_state(payload: dict) -> dict:
    portfolio = payload.get("portfolio") or payload.get("Portfolio") or {}
    holdings_raw = portfolio.get("holdings") if isinstance(portfolio, dict) else {}
    cash_raw = portfolio.get("cash") if isinstance(portfolio, dict) else {}
    holdings = []
    if isinstance(holdings_raw, dict):
        iterator = holdings_raw.items()
    elif isinstance(holdings_raw, list):
        iterator = enumerate(holdings_raw)
    else:
        iterator = []
    for key, value in iterator:
        if not isinstance(value, dict):
            continue
        holdings.append({
            "symbol": value.get("symbol") or value.get("Symbol") or str(key),
            "quantity": value.get("quantity") or value.get("Quantity") or value.get("holdings") or value.get("q"),
            "average_price": value.get("averagePrice") or value.get("AveragePrice") or value.get("a"),
            "market_price": value.get("price") or value.get("Price") or value.get("p"),
            "market_value": value.get("marketValue") or value.get("MarketValue") or value.get("value") or value.get("v"),
            "unrealized_pnl": value.get("unrealizedProfit") or value.get("UnrealizedProfit") or value.get("u"),
        })
    return {
        "raw": portfolio if isinstance(portfolio, dict) else {},
        "holdings": holdings,
        "cash": cash_raw if isinstance(cash_raw, dict) else {},
        "total_portfolio_value": portfolio.get("totalPortfolioValue") if isinstance(portfolio, dict) else None,
    }


def _quantconnect_cloud_snapshot(credentials: QuantConnectCredentials, *, force_refresh: bool = False) -> dict:
    override = _public_override("_quantconnect_cloud_snapshot", _quantconnect_cloud_snapshot)
    if override is not None:
        return override(credentials, force_refresh=force_refresh)
    if not credentials.configured or not credentials.project_configured:
        return {
            "enabled": False,
            "errors": [],
            "project": {},
            "live": {},
            "portfolio": {},
            "orders": [],
        }

    now = trader.datetime.now(trader.KST)
    cached = _read_json_file(QUANTCONNECT_CLOUD_CACHE, {})
    if not force_refresh and isinstance(cached, dict) and cached.get("checked_at"):
        try:
            age = (now - trader.datetime.fromisoformat(cached["checked_at"])).total_seconds()
        except ValueError:
            age = None
        if age is not None and age < 60 and isinstance(cached.get("snapshot"), dict):
            snapshot = cached["snapshot"]
            snapshot["cached"] = True
            return snapshot

    api = QuantConnectAPI(credentials)
    project_payload = api.read_project(credentials.project_id, timeout=8.0)
    live_list_payload = api.list_live_algorithms(credentials.project_id, timeout=8.0)
    live_payload = api.read_live_algorithm(credentials.project_id, timeout=8.0)
    portfolio_payload = api.read_live_portfolio(credentials.project_id, timeout=8.0)

    projects = project_payload.get("projects") if isinstance(project_payload, dict) else []
    project = _first_item(projects)
    live_algorithms = (
        live_list_payload.get("live") or
        live_list_payload.get("algorithms") or
        live_list_payload.get("liveAlgorithms") or
        []
    )
    live_algorithm = _first_item(live_algorithms)
    deploy_id = (
        live_payload.get("deployId") or
        live_payload.get("algorithmId") or
        live_algorithm.get("deployId") or
        live_algorithm.get("algorithmId")
        if isinstance(live_payload, dict)
        else None
    )

    orders_payload = {}
    if deploy_id:
        orders_payload = api.read_live_orders(credentials.project_id, deploy_id, start=0, end=100, timeout=8.0)

    portfolio = _quantconnect_portfolio_state(portfolio_payload if isinstance(portfolio_payload, dict) else {})
    snapshot = {
        "enabled": True,
        "cached": False,
        "project": {
            "id": project.get("projectId") or credentials.project_id,
            "name": project.get("name") or project.get("Name") or "",
            "modified": project.get("modified") or project.get("Modified") or "",
            "language": project.get("language") or project.get("Language") or "",
        },
        "live": {
            "status": live_payload.get("status") or live_algorithm.get("status"),
            "deploy_id": deploy_id,
            "message": live_payload.get("message") or live_algorithm.get("message"),
            "launched": live_payload.get("launched") or live_algorithm.get("launched"),
            "stopped": live_payload.get("stopped") or live_algorithm.get("stopped"),
            "brokerage": live_payload.get("brokerage") or live_algorithm.get("brokerage"),
        },
        "portfolio": portfolio,
        "orders": _quantconnect_order_rows(orders_payload if isinstance(orders_payload, dict) else {}),
        "api_errors": _quantconnect_errors(project_payload, live_list_payload, live_payload, portfolio_payload, orders_payload),
    }
    QUANTCONNECT_CLOUD_CACHE.parent.mkdir(parents=True, exist_ok=True)
    QUANTCONNECT_CLOUD_CACHE.write_text(
        json.dumps({"checked_at": now.isoformat(), "snapshot": snapshot}, ensure_ascii=False),
        encoding="utf-8",
    )
    return snapshot


def _clear_quantconnect_cloud_cache() -> None:
    try:
        QUANTCONNECT_CLOUD_CACHE.unlink(missing_ok=True)
    except OSError:
        pass


def _quantconnect_live_nodes(nodes_payload: dict) -> list[dict]:
    nodes = nodes_payload.get("nodes") if isinstance(nodes_payload, dict) else {}
    live_nodes = nodes.get("live") if isinstance(nodes, dict) else []
    return [node for node in live_nodes if isinstance(node, dict)]


def _select_quantconnect_live_node(nodes_payload: dict, requested_node_id: str = "") -> dict:
    live_nodes = _quantconnect_live_nodes(nodes_payload)
    if requested_node_id:
        for node in live_nodes:
            if str(node.get("id") or "") == requested_node_id:
                return node
        raise HTTPException(status_code=400, detail=f"QuantConnect live node not found: {requested_node_id}")

    for node in live_nodes:
        if node.get("active") and not node.get("busy"):
            return node
    for node in live_nodes:
        if node.get("active"):
            return node
    if live_nodes:
        return live_nodes[0]
    raise HTTPException(status_code=409, detail="No QuantConnect live node is available for this project")


def _wait_for_quantconnect_compile(
    api: QuantConnectAPI,
    project_id: str,
    compile_payload: dict,
    *,
    attempts: int = 12,
    interval_seconds: float = 2.0,
) -> dict:
    compile_id = str(compile_payload.get("compileId") or "")
    if not compile_id:
        errors = compile_payload.get("errors") or [compile_payload.get("error") or "QuantConnect compile did not return compileId"]
        raise HTTPException(status_code=502, detail="; ".join(str(error) for error in errors if error))

    result = compile_payload
    for _ in range(attempts):
        state = str(result.get("state") or "").lower()
        if state == "buildsuccess":
            return result
        if state == "builderror":
            logs = result.get("logs") or result.get("errors") or ["QuantConnect build failed"]
            raise HTTPException(status_code=502, detail="; ".join(str(log) for log in logs if log))
        time.sleep(interval_seconds)
        result = api.read_compile(project_id, compile_id, timeout=10.0)

    raise HTTPException(status_code=504, detail=f"QuantConnect compile is still pending: {compile_id}")


def _quantconnect_mnq_status() -> dict:
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    algorithm_path = QUANTCONNECT_MNQ_DIR / "main.py"
    config_path = QUANTCONNECT_MNQ_DIR / "config.json"
    doc_path = BASE_DIR / "doc" / "S1.한스톡사용설명서.md"
    config = _read_json_file(config_path, {})
    if not isinstance(config, dict):
        config = {}
    results = _read_json_file(QUANTCONNECT_MNQ_RESULTS, {})
    if not isinstance(results, dict):
        results = {}
    qc_user_id = os.environ.get("QUANTCONNECT_USER_ID") or os.environ.get("QC_USER_ID")
    qc_api_token = os.environ.get("QUANTCONNECT_API_TOKEN") or os.environ.get("QC_API_TOKEN")
    qc_project_id = os.environ.get("QUANTCONNECT_PROJECT_ID") or os.environ.get("QC_PROJECT_ID")
    credentials = QuantConnectCredentials(
        user_id=qc_user_id or "",
        api_token=qc_api_token or "",
        project_id=qc_project_id or "",
    )
    auth = _quantconnect_auth_status(credentials)
    cloud_sync_configured = credentials.configured and credentials.project_configured
    cloud_snapshot = _quantconnect_cloud_snapshot(credentials)
    project_ready = algorithm_path.exists() and config_path.exists()

    deployment = results.get("deployment") if isinstance(results.get("deployment"), dict) else {}
    if not deployment:
        cloud_live = cloud_snapshot.get("live", {}) if isinstance(cloud_snapshot.get("live"), dict) else {}
        cloud_status = str(cloud_live.get("status") or "").strip()
        if cloud_status:
            if cloud_status.lower() == "running":
                deployment_status = "running"
                deployment_message = "QuantConnect Paper Live deployment is running."
            else:
                deployment_status = cloud_status.lower()
                deployment_message = (
                    f"QuantConnect project is configured, but the Paper Live deployment is {cloud_status}. "
                    "Start or redeploy it before sending dashboard orders."
                )
        elif not credentials.configured:
            deployment_status = "not_connected"
            deployment_message = "QuantConnect User Id and API Token are required."
        elif not credentials.project_configured:
            deployment_status = "not_connected"
            deployment_message = "QuantConnect Project Id is required for project/order sync."
        else:
            deployment_status = "ready_to_sync"
            deployment_message = "QuantConnect API and Project Id are configured. Deploy the project as Paper Live before sending dashboard orders."
        deployment = {
            "status": deployment_status,
            "message": deployment_message,
        }

    return {
        "as_of": trader.datetime.now(trader.KST).isoformat(),
        "feasible": True,
        "project_ready": project_ready,
        "cloud_sync_configured": cloud_sync_configured,
        "auth": {
            "configured": credentials.configured,
            "project_configured": credentials.project_configured,
            "success": bool(auth.get("success")),
            "status_code": auth.get("status_code"),
            "error": auth.get("error"),
        },
        "algorithm": {
            "path": str(algorithm_path),
            "exists": algorithm_path.exists(),
            "symbol": "MNQ",
            "quantconnect_symbol": "Futures.Indices.MICRO_NASDAQ_100_E_MINI",
            "brokerage": "QuantConnect Paper Trading",
            "max_contracts": config.get("parameters", {}).get("MAX_CONTRACTS", "1"),
        },
        "files": {
            "config": {"path": str(config_path), "exists": config_path.exists()},
            "documentation": {"path": str(doc_path), "exists": doc_path.exists()},
            "results": {"path": str(QUANTCONNECT_MNQ_RESULTS), "exists": QUANTCONNECT_MNQ_RESULTS.exists()},
        },
        "deployment": deployment,
        "account": cloud_snapshot.get("portfolio", {}).get("raw") or results.get("account", {}),
        "positions": cloud_snapshot.get("portfolio", {}).get("holdings") or results.get("positions", []),
        "orders": cloud_snapshot.get("orders") or results.get("orders", []),
        "metrics": results.get("metrics", {}),
        "cloud": cloud_snapshot,
        "sources": [
            "https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/brokerages/quantconnect-paper-trading",
            "https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/algoseek/us-futures",
        ],
    }


def _quantconnect_credentials() -> QuantConnectCredentials:
    override = _public_override("_quantconnect_credentials", _quantconnect_credentials)
    if override is not None:
        return override()
    return _external_integration_service.quantconnect_credentials()


def _quantconnect_mnq_deploy(payload: dict | None = None) -> dict:
    payload = payload or {}
    credentials = _quantconnect_credentials()
    if not credentials.configured:
        raise HTTPException(status_code=400, detail="QuantConnect User Id and API Token are required")
    if not credentials.project_configured:
        raise HTTPException(status_code=400, detail="QUANTCONNECT_PROJECT_ID is required")

    api = QuantConnectAPI(credentials)
    payload_node_id = str(payload.get("node_id") or "").strip()
    requested_node_id = (
        payload_node_id
        or os.environ.get("QUANTCONNECT_LIVE_NODE_ID", "").strip()
        or os.environ.get("QC_LIVE_NODE_ID", "").strip()
    )

    nodes_payload = api.read_project_nodes(credentials.project_id, timeout=10.0)
    if not nodes_payload.get("success", False):
        errors = nodes_payload.get("errors") or [nodes_payload.get("error") or "QuantConnect live node lookup failed"]
        raise HTTPException(status_code=502, detail="; ".join(str(error) for error in errors if error))
    try:
        node = _select_quantconnect_live_node(nodes_payload, requested_node_id)
    except HTTPException:
        if payload_node_id:
            raise
        node = _select_quantconnect_live_node(nodes_payload, "")

    compile_payload = api.create_compile(credentials.project_id, timeout=10.0)
    if not compile_payload.get("success", False):
        errors = compile_payload.get("errors") or [compile_payload.get("error") or "QuantConnect compile failed"]
        raise HTTPException(status_code=502, detail="; ".join(str(error) for error in errors if error))
    compile_result = _wait_for_quantconnect_compile(api, credentials.project_id, compile_payload)

    config = _read_json_file(QUANTCONNECT_MNQ_DIR / "config.json", {})
    parameters = config.get("parameters", {}) if isinstance(config, dict) else {}
    live_payload = api.create_live_algorithm(
        credentials.project_id,
        str(compile_result.get("compileId")),
        str(node.get("id")),
        parameters=parameters,
        timeout=20.0,
    )
    if not live_payload.get("success", False):
        errors = live_payload.get("errors") or [live_payload.get("error") or "QuantConnect Paper Live deployment failed"]
        raise HTTPException(status_code=502, detail="; ".join(str(error) for error in errors if error))

    _clear_quantconnect_cloud_cache()
    snapshot = _quantconnect_cloud_snapshot(credentials, force_refresh=True)
    return {
        "success": True,
        "project_id": credentials.project_id,
        "compile_id": compile_result.get("compileId"),
        "node": {
            "id": node.get("id"),
            "name": node.get("name"),
            "sku": node.get("sku"),
        },
        "deploy_id": live_payload.get("deployId") or live_payload.get("algorithmId"),
        "raw": live_payload,
        "cloud": snapshot,
    }


def _quantconnect_mnq_order(payload: dict) -> dict:
    credentials = _quantconnect_credentials()
    side = str(payload.get("side") or "").strip().lower()
    signal_id = str(payload.get("signal_id") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    try:
        quantity = int(payload.get("quantity") or payload.get("qty") or 0)
    except (TypeError, ValueError):
        quantity = 0

    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="side must be buy or sell")
    if quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be at least 1")
    if quantity > 3:
        raise HTTPException(status_code=400, detail="MNQ paper dashboard orders are limited to 3 contracts")
    if not credentials.configured:
        raise HTTPException(status_code=400, detail="QuantConnect User Id and API Token are required")
    if not credentials.project_configured:
        raise HTTPException(status_code=400, detail="QUANTCONNECT_PROJECT_ID is required")

    cloud_snapshot = _quantconnect_cloud_snapshot(credentials, force_refresh=True)
    live = cloud_snapshot.get("live", {}) if isinstance(cloud_snapshot.get("live"), dict) else {}
    live_status = str(live.get("status") or "").strip()
    if live_status.lower() != "running":
        detail = (
            f"QuantConnect project {credentials.project_id} has no running Paper Live instance"
        )
        if live_status:
            detail += f" (current status: {live_status})"
        detail += ". Start or redeploy the project in QuantConnect before sending dashboard orders."
        raise HTTPException(status_code=409, detail=detail)

    order_tag = "hanstock-dashboard-mnq-paper"
    if signal_id:
        tag_source = re.sub(r"[^A-Za-z0-9_-]+", "-", provider or "telegram").strip("-") or "telegram"
        signal_ref = re.sub(r"[^A-Za-z0-9_-]+", "-", signal_id).strip("-") or "signal"
        order_tag = f"hanstock-signal-{tag_source}-{signal_ref}"[:80]

    command = {
        "command_type": "order",
        "symbol": "MNQ",
        "side": side,
        "quantity": quantity,
        "tag": order_tag,
    }
    result = QuantConnectAPI(credentials).create_live_command(credentials.project_id, command, timeout=10.0)
    return {
        "success": bool(result.get("success")),
        "command": command,
        "status_code": result.get("status_code"),
        "error": result.get("error"),
        "errors": result.get("errors", []),
    }


def _license_name(text: str, hint: str) -> str:
    lowered = text.lower()
    if "gnu general public license" in lowered:
        return "GPL-3.0"
    if "mit license" in lowered:
        return "MIT"
    if "apache license" in lowered:
        return "Apache-2.0"
    return hint or "unknown"


def _vendor_status(slug: str, meta: dict) -> dict:
    root = meta["path"]
    exists = root.exists()
    license_path = root / "LICENSE"
    if not license_path.exists():
        license_path = root / "LICENSE.txt"
    license_text = license_path.read_text(encoding="utf-8", errors="replace") if license_path.exists() else ""
    files = list(root.rglob("*")) if exists else []
    pkg = root / meta["package"]
    modules = []
    if pkg.exists():
        modules = [
            child.name
            for child in sorted(pkg.iterdir())
            if child.is_dir() and not child.name.startswith("__")
        ]
    return {
        "slug": slug,
        "name": meta["name"],
        "exists": exists,
        "path": str(root),
        "license": _license_name(license_text, meta["license_hint"]),
        "license_notice": license_text[:500],
        "file_count": len([path for path in files if path.is_file()]),
        "python_file_count": len([path for path in files if path.suffix == ".py"]),
        "notebook_count": len([path for path in files if path.suffix == ".ipynb"]),
        "modules": modules,
        "adapter": meta["adapter"],
        "entrypoints": meta["entrypoints"],
        "dashboard": meta["dashboard"],
    }


def _demo_trading_readiness() -> dict:
    missing = _required_env_missing()
    account_warning = _account_format_warning(trader.config.kistock_account)
    checks = [
        {
            "key": "required_env",
            "ok": not missing,
            "message": "Required KIS environment values are configured" if not missing else f"Missing: {', '.join(missing)}",
            "critical": True,
        },
        {
            "key": "account_format",
            "ok": not account_warning,
            "message": "KIS account format is valid" if not account_warning else account_warning,
            "critical": True,
        },
        {
            "key": "demo_environment",
            "ok": trader.TRADING_ENV == "demo",
            "message": f"TRADING_ENV={trader.TRADING_ENV}",
            "critical": True,
        },
        {
            "key": "dry_run_disabled",
            "ok": trader.DRY_RUN is False,
            "message": f"DRY_RUN={str(trader.DRY_RUN).lower()}",
            "critical": True,
        },
        {
            "key": "live_trading_disabled",
            "ok": trader.ENABLE_LIVE_TRADING is False and trader.REAL_ORDERS_ENABLED is False,
            "message": f"ENABLE_LIVE_TRADING={str(trader.ENABLE_LIVE_TRADING).lower()}, real_orders={str(trader.REAL_ORDERS_ENABLED).lower()}",
            "critical": True,
        },
        {
            "key": "demo_order_submission",
            "ok": trader.ORDER_SUBMISSION_ENABLED is True,
            "message": f"ORDER_SUBMISSION_ENABLED={str(trader.ORDER_SUBMISSION_ENABLED).lower()}",
            "critical": True,
        },
        {
            "key": "kill_switch",
            "ok": not Path(".runtime/kill_switch.json").exists(),
            "message": "Kill switch is inactive" if not Path(".runtime/kill_switch.json").exists() else "Kill switch is active",
            "critical": False,
        },
        {
            "key": "approval_policy",
            "ok": trader.REQUIRE_APPROVAL or _auto_approval_enabled(),
            "message": f"REQUIRE_APPROVAL={str(trader.REQUIRE_APPROVAL).lower()}, auto_approval={str(_auto_approval_enabled()).lower()}",
            "critical": False,
        },
    ]
    critical_ready = all(item["ok"] for item in checks if item["critical"])
    return {
        "ready": critical_ready,
        "mode": "kis_demo_auto",
        "trading_env": trader.TRADING_ENV,
        "dry_run": trader.DRY_RUN,
        "enable_live_trading": trader.ENABLE_LIVE_TRADING,
        "order_submission_enabled": trader.ORDER_SUBMISSION_ENABLED,
        "real_orders_enabled": trader.REAL_ORDERS_ENABLED,
        "checks": checks,
    }


def _runtime_dashboard_info() -> dict:
    hostname = socket.gethostname()
    explicit_label = os.environ.get("HANSTOCK_DASHBOARD_LABEL", "").strip()
    explicit_origin = os.environ.get("HANSTOCK_DASHBOARD_ORIGIN", "").strip().lower()
    is_vm = explicit_origin == "vm" or hostname.startswith("hanstock-server")
    label = explicit_label or ("VM DASHBOARD" if is_vm else "LOCAL DASHBOARD")
    return {
        "label": label,
        "origin": "vm" if is_vm else "local",
        "is_vm": is_vm,
        "hostname": hostname,
    }



@app.post("/api/futures-signals/collector/run")
async def run_futures_signal_collector(payload: dict | None = Body(default=None)):
    payload = payload or {}
    status = collector_status()
    if not status["ready"]:
        return {**status, "ok": False, "ingested": 0}

    limit = max(1, min(int(payload.get("limit_per_channel", 50) or 50), 200))
    try:
        messages = await TelegramSignalCollector().fetch_recent_messages(limit_per_channel=limit)
    except DashboardOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    from src.futures_signals import db as futures_signals_db

    ingested = 0
    parse_errors = []
    for message in messages:
        if message.get("collector_error"):
            parse_errors.append({
                "channel": message.get("channel"),
                "error": message.get("collector_error"),
            })
            continue
        try:
            raw_text = str(message.get("raw_text") or "")
            channel = str(message.get("channel") or "telegram")
            msg_id = message.get("telegram_message_id") or 0
            msg_date = message.get("received_at") or ""

            # parser로 파싱 시도
            parsed = None
            try:
                from src.futures_signals.parser import parse_signal
                parsed = parse_signal(raw_text)
            except (FuturesSignalParseError, TypeError, ValueError) as exc:
                logger.warning(f"Failed to parse collected futures signal: {exc}")

            inserted = futures_signals_db.insert_signal(
                channel_key=channel,
                message_id=int(msg_id) if str(msg_id).isdigit() else 0,
                message_date=msg_date,
                raw_text=raw_text,
                symbol=parsed.symbol if parsed else None,
                direction=parsed.direction if parsed else None,
                entry_price=parsed.entry if parsed else None,
                stop_loss=parsed.stop_loss if parsed else None,
                target_price=parsed.take_profits[0] if parsed and parsed.take_profits else None,
                confidence=None,
                notes=None,
            )
            if inserted:
                ingested += 1
        except DashboardOperationError as exc:
            parse_errors.append({
                "telegram_message_id": message.get("telegram_message_id"),
                "error": str(exc),
            })
    return {
        "ok": True,
        "ingested": ingested,
        "parse_errors": parse_errors,
        "collector": status,
    }



@app.post("/api/futures-signals/collector/settings")
async def save_collector_settings(request: Request):
    """Telegram 설정 저장 - .env 파일에 기록"""
    body = await request.json()

    env_path = Path(".env")

    # 기존 .env 읽기
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    # 업데이트할 키-값 쌍
    updates = {}
    if "api_id" in body:
        updates["TELEGRAM_API_ID"] = str(body["api_id"])
    if "api_hash" in body:
        updates["TELEGRAM_API_HASH"] = str(body["api_hash"])
    if "channels" in body:
        updates["TELEGRAM_TARGET_CHANNELS"] = str(body["channels"])

    # 기존 라인에서 해당 키 업데이트
    new_lines = []
    updated_keys = set()
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f'{key}={updates[key]}')
                updated_keys.add(key)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    # 새 키 추가
    for key, val in updates.items():
        if key not in updated_keys:
            new_lines.append(f'{key}={val}')

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return {"ok": True, "message": "설정이 저장되었습니다. 서버를 재시작하면 적용됩니다."}



# =============================================================================
# KIS 해외선물optsms Trading API
# =============================================================================

def _get_futures_api():
    from src.api.kis_futures_api import KISFuturesAPI
    return KISFuturesAPI()



from pydantic import BaseModel, Field

class NewStrategyPayload(BaseModel):
    name: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    weight: float = Field(..., ge=0.0, le=1.0)
    description: str = Field("")

class SelectStrategyPayload(BaseModel):
    selected: bool



from typing import Optional

class WatchlistAddPayload(BaseModel):
    symbol: str = Field(..., min_length=6, max_length=6)
    strategy_id: str | None = None

class WatchlistTogglePayload(BaseModel):
    enabled: bool
    threshold: Optional[float] = None


class WatchlistPolicyPayload(BaseModel):
    enabled: bool = True
    min_price: float = Field(5_000.0, ge=0.0, le=10_000_000.0)
    min_market_cap: float = Field(
        300_000_000_000.0,
        ge=0.0,
        le=10_000_000_000_000_000.0,
    )
    require_mid_large_when_market_cap_unknown: bool = True


WATCHLIST_MIN_SCAN_SCORE = 2.0


def _sync_watchlist_from_scan_result(
    watchlist_data: dict,
    scan_result: dict,
    add_threshold: float,
    keep_threshold: float | None = None,
) -> dict:
    from src.strategy.seven_split import KOSPI_UNIVERSE
    from src.strategy.watchlist_policy import eligibility_reason, normalize_watchlist_policy

    if keep_threshold is None:
        keep_threshold = add_threshold
    symbols = list(watchlist_data.get("symbols", []))
    symbol_set = set(symbols)
    scanned_rows = scan_result.get("scan_summary") or scan_result.get("candidates") or []
    candidates = scan_result.get("candidates") or []
    policy = normalize_watchlist_policy(watchlist_data.get("policy"))

    score_by_symbol: dict[str, float] = {}
    name_by_symbol: dict[str, str] = {}
    for row in scanned_rows:
        symbol = row.get("ticker") or row.get("symbol")
        if not symbol:
            continue
        try:
            score_by_symbol[str(symbol)] = float(row.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score_by_symbol[str(symbol)] = 0.0
        if row.get("name"):
            name_by_symbol[str(symbol)] = row["name"]

    added_symbols = []
    eligible_count = 0
    already_registered_count = 0
    for cand in candidates:
        try:
            score = float(cand.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score < add_threshold:
            continue
        if eligibility_reason(
            price=cand.get("current_price") or cand.get("price"),
            market_cap=cand.get("market_cap"),
            known_mid_large=str(cand.get("ticker") or cand.get("symbol") or "") in KOSPI_UNIVERSE,
            policy=policy,
        ):
            continue
        eligible_count += 1
        symbol = str(cand["ticker"])
        name_by_symbol.setdefault(symbol, cand.get("name") or symbol)
        if symbol in symbol_set:
            already_registered_count += 1
            continue
        symbols.append(symbol)
        symbol_set.add(symbol)
        added_symbols.append({
            "symbol": symbol,
            "name": cand.get("name") or symbol,
            "score": cand.get("score", score),
        })

    # A weak score in one scan means "no entry signal now", not "remove this
    # registered symbol". Keep explicit registrations stable across scheduled
    # scans; pruning must be an explicit user action.
    removed_symbols = []
    watchlist_data["symbols"] = symbols
    return {
        "changed": bool(added_symbols or removed_symbols),
        "eligible_count": eligible_count,
        "already_registered_count": already_registered_count,
        "added_symbols": added_symbols,
        "removed_symbols": removed_symbols,
    }


@app.post("/api/watchlist/scan-trigger")
async def trigger_watchlist_ai_scan(request: WatchlistTogglePayload | None = Body(default=None)):
    from src.db.repository import load_watchlist_data, save_watchlist_data
    from src.strategy.seven_split import sync_watchlist_runtime
    
    missing = _required_env_missing()
    if missing:
        raise HTTPException(status_code=503, detail=f"스캔 환경 변수가 미비합니다: {', '.join(missing)}")
        
    try:
        api = _get_api()
        parsed = _parse_balance(_get_balance_data(api))
        
        # GPT-5-mini 기반 강세 후보 실시간 AI 분석 가동
        ranker_model = "gpt-5-mini"
        ranker_weight = 0.4
        optimizer = "score_tilted_inverse_vol"
        
        scan_result = build_dashboard_candidates(
            api, parsed, min_score=1, ranker=ranker_model, ranker_weight=ranker_weight, optimizer=optimizer
        )
        
        watchlist_data = load_watchlist_data()
        if request is not None:
            threshold_value = request.threshold
            if threshold_value is not None and not 1.0 <= threshold_value <= 10.0:
                raise HTTPException(status_code=400, detail="threshold must be between 1 and 10")
            watchlist_data["ai_auto_add"] = request.enabled
            if threshold_value is not None:
                watchlist_data["ai_auto_add_threshold"] = threshold_value
            save_watchlist_data(watchlist_data)

        threshold = float(watchlist_data.get("ai_auto_add_threshold", 3.0))
        sync_result = {
            "eligible_count": 0,
            "already_registered_count": 0,
            "added_symbols": [],
            "removed_symbols": [],
            "changed": False,
        }
        
        if scan_result["scanned"] > 0:
            sync_result = _sync_watchlist_from_scan_result(watchlist_data, scan_result, threshold)
            if sync_result["changed"]:
                save_watchlist_data(watchlist_data)
                sync_watchlist_runtime()
                
        return {
            "ok": True,
            "scanned": scan_result["scanned"],
            "threshold_used": threshold,
            "eligible_count": sync_result["eligible_count"],
            "already_registered_count": sync_result["already_registered_count"],
            "added_count": len(sync_result["added_symbols"]),
            "added_symbols": sync_result["added_symbols"],
            "removed_count": len(sync_result["removed_symbols"]),
            "removed_symbols": sync_result["removed_symbols"],
        }
    except DashboardOperationError as e:
        logger.error(f"Failed to manually trigger watchlist AI scan: {e}")
        raise HTTPException(status_code=500, detail=f"AI 스캔 및 자동추가 실행 중 오류 발생: {str(e)}")



def _dashboard_analysis_cycle(
    strategy_id: str | None,
    cycle_id: str | None = None,
) -> tuple[str, dict | None]:
    if not strategy_id and not cycle_id:
        return "seven_split", None
    strategy = _resolve_dashboard_strategy(strategy_id)
    if strategy_id and strategy is None:
        raise AnalysisCycleError(f"strategy not found: {strategy_id}")
    resolved_strategy_id = str(strategy.get("id")) if strategy else "seven_split"
    if not cycle_id:
        return resolved_strategy_id, None
    cycle = resolve_common_analysis_cycle(
        resolved_strategy_id,
        trader.TRADING_ENV,
        cycle_id,
    )
    return resolved_strategy_id, cycle


def _cycle_balance_data(api, cycle: dict | None) -> dict:
    if cycle is None:
        return _get_balance_data(api)
    balance_data = load_or_capture_common_stage(
        cycle["id"],
        "account_balance",
        lambda: _get_balance_data(api),
        details={"source": "broker_snapshot"},
    )
    if not isinstance(balance_data, dict):
        raise DashboardOperationError("analysis-cycle account snapshot is invalid")
    return balance_data


@app.get("/api/signals")
async def get_signals(strategy_id: str | None = None, cycle_id: str | None = None):
    missing = _required_env_missing()
    if missing:
        raise HTTPException(status_code=503, detail=f"Missing environment variables: {', '.join(missing)}")

    try:
        resolved_strategy_id, cycle = _dashboard_analysis_cycle(strategy_id, cycle_id)
    except AnalysisCycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _build():
        api = _get_api()
        parsed = _parse_balance(_cycle_balance_data(api, cycle))
        signals = build_dashboard_signals(api, parsed, strategy_id=resolved_strategy_id)
        response_cycle = cycle
        payload = {"signals": signals}
        if cycle is not None:
            response_cycle = mark_common_analysis_stage(
                cycle["id"],
                "signals",
                details={"count": len(signals)},
                payload={"signals": signals},
            )
            payload["_analysis_cycle"] = response_cycle if isinstance(response_cycle, dict) else cycle
        return payload

    try:
        stored = get_common_analysis_stage(cycle["id"], "signals") if cycle else None
        if isinstance((stored or {}).get("payload"), dict):
            return {**stored["payload"], "_analysis_cycle": cycle}
        cache_key = f"signals:{resolved_strategy_id}:{cycle['id'] if cycle else 'latest'}"
        return snapshot_read_through(cache_key, _build)
    except DashboardOperationError as e:
        if cycle:
            mark_common_analysis_stage(cycle["id"], "signals", status="failed", details={"error": str(e)})
        raise HTTPException(status_code=502, detail=f"Signal analysis failed: {e}") from e
    except Exception as e:
        if cycle:
            mark_common_analysis_stage(cycle["id"], "signals", status="failed", details={"error": str(e)})
        raise HTTPException(status_code=500, detail=f"Signal analysis failed: {e}") from e



@app.get("/api/candidates")
async def get_candidates(
    min_score: int = 2,
    ranker: str = "gpt_5_mini",
    optimizer: str = "score_tilted_inverse_vol",
    strategy_id: str | None = None,
    cycle_id: str | None = None,
    refresh: bool = False,
    cache_only: bool = False,
):
    if min_score < 1:
        raise HTTPException(status_code=400, detail="min_score must be greater than 0")

    missing = _required_env_missing()
    if missing:
        raise HTTPException(status_code=503, detail=f"Missing environment variables: {', '.join(missing)}")

    try:
        resolved_strategy_id, cycle = _dashboard_analysis_cycle(strategy_id, cycle_id)
    except AnalysisCycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    stored = get_common_analysis_stage(cycle["id"], "candidates") if cycle else None
    if isinstance((stored or {}).get("payload"), dict):
        return {**stored["payload"], "_analysis_cycle": cycle}

    cache_ranker = strategy_id or ranker
    cached = None
    if not refresh:
        if cache_ranker == "gpt_5_mini" and optimizer == "score_tilted_inverse_vol":
            cached = _load_candidate_cache(min_score, allow_stale=cache_only)
        else:
            cached = _load_candidate_cache(
                min_score, cache_ranker, optimizer, allow_stale=cache_only
            )
        
    if cached is not None:
        if cycle is not None:
            response_cycle = mark_common_analysis_stage(
                cycle["id"],
                "candidates",
                details={"count": len(cached.get("candidates", [])), "source": "cache"},
                payload=cached,
            )
            cached["_analysis_cycle"] = response_cycle if isinstance(response_cycle, dict) else cycle
        return cached

    if cache_only:
        return {
            "candidates": [],
            "scan_summary": [],
            "scanned": 0,
            "min_score": min_score,
            "_cache": {"missing": True, "cached_at": None, "stale": False},
        }

    try:
        api = _get_api()
        parsed = _parse_balance(_cycle_balance_data(api, cycle))
        
        from src.db.repository import load_ai_strategies
        strats = load_ai_strategies()
        selected_strat = next((s for s in strats if s["id"] == cache_ranker), None)
        
        strategy_model = ""
        strategy_profile = None
        strategy_description = ""
        if selected_strat:
            profile = selected_strat.get("profile") or {}
            model = profile.get("model") or selected_strat["model"] or "none"
            provider = selected_strat.get("provider") or "none"
            ranker_weight = float(profile.get("ai_weight", selected_strat["weight"]) or 0.0)

            strategy_model = model
            strategy_profile = profile
            strategy_description = selected_strat.get("description") or ""
            if provider == "none" or model == "none" or ranker_weight == 0.0:
                ranker_model = "rule_only"
            else:
                ranker_model = model
        else:
            ranker_model = cache_ranker
            ranker_weight = 0.4
            strategy_model = ""

        strategy_universe = None
        if selected_strat:
            from src.db.repository import load_strategy_universe_symbols, load_watchlist_data

            registered = list(load_watchlist_data().get("symbols", []))
            dedicated = load_strategy_universe_symbols(selected_strat["id"])
            strategy_universe = dedicated if dedicated else registered

        import asyncio
        loop = asyncio.get_event_loop()
        payload = await loop.run_in_executor(
            None,
            lambda: build_dashboard_candidates(
                api,
                parsed,
                min_score=min_score,
                ranker=ranker_model,
                ranker_weight=ranker_weight,
                optimizer=optimizer,
                strategy_model=strategy_model,
                strategy_profile=strategy_profile,
                strategy_description=strategy_description,
                universe=strategy_universe,
            ),
        )
        if selected_strat:
            for cand in payload.get("candidates", []):
                cand["strategy_id"] = selected_strat.get("id")
                cand["strategy_version"] = selected_strat.get("strategy_version")
                cand["profile_hash"] = selected_strat.get("profile_hash")
        
        if payload["scanned"] > 0:
            # Automatically save scan results to DB for history tracking
            from src.db.repository import save_scanned_candidate
            for cand in payload["candidates"]:
                saved_candidate_id = save_scanned_candidate(
                    symbol=cand["ticker"],
                    name=cand["name"],
                    score=cand["score"],
                    reasons=cand["reasons"],
                    price=cand["current_price"],
                    env=trader.TRADING_ENV,
                    indicators={
                        "rsi": cand.get("rsi"),
                        "rsi2": cand.get("rsi2"),
                        "macd_hist": cand.get("macd_hist"),
                        "sma20": cand.get("sma20"),
                        "sma60": cand.get("sma60"),
                    },
                    strategy=selected_strat,
                    ranker_model=ranker_model,
                    optimizer=optimizer,
                    scoring={
                        "rule_score": cand.get("rule_score"),
                        "ml_score": cand.get("ml_score"),
                        "final_score": cand.get("final_score"),
                        "ai_model_status": cand.get("ai_model_status"),
                        "ai_fallback_reason": cand.get("ai_fallback_reason"),
                        "top_features": cand.get("top_features"),
                    },
                )
                if saved_candidate_id and selected_strat:
                    cand["id"] = saved_candidate_id
            if selected_strat:
                from src.db.repository import record_ai_strategy_event, save_ai_strategies
                now = trader.datetime.now(trader.KST).strftime("%Y-%m-%d %H:%M:%S")
                for s in strats:
                    if s.get("id") == selected_strat.get("id"):
                        s["last_used_at"] = now
                        break
                save_ai_strategies(strats)
                record_ai_strategy_event(
                    selected_strat["id"],
                    "used_for_candidates",
                    {
                        "optimizer": optimizer,
                        "ranker_model": ranker_model,
                        "scanned": payload.get("scanned", 0),
                        "candidates": len(payload.get("candidates", [])),
                    },
                    selected_strat.get("strategy_version"),
                )

            # 후보 이력의 DB id까지 결과에 반영한 뒤 전략별 최신본을 저장한다.
            # 동일 전략/옵티마이저/점수 조합은 DB에서 항상 한 행으로 갱신된다.
            if cache_ranker == "gpt_5_mini" and optimizer == "score_tilted_inverse_vol":
                cached_at = _save_candidate_cache(
                    min_score, payload["candidates"], payload["scan_summary"], payload["scanned"]
                )
            else:
                cached_at = _save_candidate_cache(
                    min_score,
                    payload["candidates"],
                    payload["scan_summary"],
                    payload["scanned"],
                    cache_ranker,
                    optimizer,
                )
            if isinstance(cached_at, str):
                payload["_cache"] = {
                    "stale": False,
                    "cached_at": cached_at,
                    "persisted": True,
                }
            
            # AI 자동 추가적용 로직
            from src.db.repository import load_watchlist_data, save_watchlist_data
            from src.strategy.seven_split import sync_watchlist_runtime
            try:
                watchlist_data = load_watchlist_data()
                if watchlist_data.get("ai_auto_add", False):
                    threshold = float(watchlist_data.get("ai_auto_add_threshold", 3.0))
                    sync_result = _sync_watchlist_from_scan_result(watchlist_data, payload, threshold)
                    if sync_result["changed"]:
                        save_watchlist_data(watchlist_data)
                        sync_watchlist_runtime()
            except DashboardOperationError as w_err:
                logger.warning(f"Failed to auto-add high score candidate to watchlist: {w_err}")
                
        if cycle is not None:
            stored_payload = dict(payload)
            stored_payload.pop("_analysis_cycle", None)
            response_cycle = mark_common_analysis_stage(
                cycle["id"],
                "candidates",
                details={"count": len(payload.get("candidates", [])), "source": "scan"},
                payload=stored_payload,
            )
            payload["_analysis_cycle"] = response_cycle if isinstance(response_cycle, dict) else cycle
        return payload
    except DashboardOperationError as e:
        if cycle:
            mark_common_analysis_stage(cycle["id"], "candidates", status="failed", details={"error": str(e)})
        raise HTTPException(status_code=502, detail=f"Candidate scan failed: {e}") from e
    except Exception as e:
        if cycle:
            mark_common_analysis_stage(cycle["id"], "candidates", status="failed", details={"error": str(e)})
        raise HTTPException(status_code=500, detail=f"Candidate scan failed: {e}") from e



@app.get("/api/candidates/history")
async def get_candidates_history(
    limit: int = 100,
    days: int = 30,
    strategy_id: str | None = None,
):
    try:
        from src.db.repository import get_scanned_candidates_history
        history = get_scanned_candidates_history(
            limit=limit,
            days=days,
            strategy_id=strategy_id,
        )
        return {"history": history}
    except DashboardOperationError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/candidates/forward-returns/refresh")
async def refresh_candidate_forward_returns(limit: int = 500):
    try:
        from src.db.repository import refresh_scanned_candidate_forward_returns

        return refresh_scanned_candidate_forward_returns(limit=limit)
    except DashboardOperationError as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.delete("/api/candidates/history/{candidate_id}")
async def delete_candidate_history(candidate_id: int):
    try:
        from src.db.repository import delete_scanned_candidate
        deleted_count = delete_scanned_candidate(candidate_id)
        if deleted_count <= 0:
            raise HTTPException(status_code=404, detail="Candidate not found")
        return {"ok": True, "deleted_count": deleted_count}
    except HTTPException:
        raise
    except DashboardOperationError as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/api/execution-plan")
async def get_execution_plan(strategy_id: str | None = None, cycle_id: str | None = None):
    missing = _required_env_missing()
    if missing:
        raise HTTPException(status_code=503, detail=f"Missing environment variables: {', '.join(missing)}")
    try:
        resolved_strategy_id, cycle = _dashboard_analysis_cycle(strategy_id, cycle_id)
    except AnalysisCycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _build():
        api = _get_api()
        balance_data = _cycle_balance_data(api, cycle)
        parsed = _parse_balance(balance_data)
        candidate_stage = get_common_analysis_stage(cycle["id"], "candidates") if cycle else None
        candidate_scan = (
            candidate_stage.get("payload")
            if isinstance((candidate_stage or {}).get("payload"), dict)
            else None
        )
        payload = stock_service.build_dashboard_execution_plan(
            api=api,
            balance_data=balance_data,
            parsed_balance=parsed,
            strategy_id=resolved_strategy_id,
            candidate_scan=candidate_scan,
        )
        if cycle is not None:
            stored_payload = dict(payload)
            response_cycle = mark_common_analysis_stage(
                cycle["id"],
                "execution_plan",
                details={"count": len(payload.get("plan", []))},
                payload=stored_payload,
            )
            payload["_analysis_cycle"] = response_cycle if isinstance(response_cycle, dict) else cycle
        return payload

    try:
        stored = get_common_analysis_stage(cycle["id"], "execution_plan") if cycle else None
        if isinstance((stored or {}).get("payload"), dict):
            return {**stored["payload"], "_analysis_cycle": cycle}
        cache_key = f"execution_plan:{resolved_strategy_id}:{cycle['id'] if cycle else 'latest'}"
        return snapshot_read_through(
            cache_key,
            _build,
        )
    except DashboardOperationError as e:
        if cycle:
            mark_common_analysis_stage(cycle["id"], "execution_plan", status="failed", details={"error": str(e)})
        raise HTTPException(status_code=502, detail=f"Execution plan failed: {e}") from e
    except Exception as e:
        if cycle:
            mark_common_analysis_stage(cycle["id"], "execution_plan", status="failed", details={"error": str(e)})
        raise HTTPException(status_code=500, detail=f"Execution plan failed: {e}") from e



def _holding_history(api: KIStockAPI, parsed: dict, n: int = 120) -> list[dict]:
    holdings = []
    for holding in parsed["holdings"]:
        daily = api.get_daily(holding["symbol"], n=n)
        prices = [float(row["stck_clpr"]) for row in daily if row.get("stck_clpr")]
        highs = [float(row["stck_hgpr"]) for row in daily if row.get("stck_hgpr")]
        volumes = [float(row["acml_vol"]) for row in daily if row.get("acml_vol")]
        prices.reverse()
        highs.reverse()
        volumes.reverse()
        holdings.append({
            "symbol": holding["symbol"],
            "name": holding["name"],
            "qty": holding["qty"],
            "price": holding["price"],
            "value": holding["value"],
            "prices": prices,
            "highs": highs,
            "volumes": volumes,
        })
    return holdings



def _load_pending_approval(approval_id: int) -> dict:
    item = _approval_by_id(approval_id)
    if not item:
        raise HTTPException(status_code=404, detail="approval not found")
    if item["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"approval is already {item['status']}")
    return item


def _claim_pending_approval(approval_id: int) -> dict:
    item = _load_pending_approval(approval_id)
    now = trader.datetime.now(trader.KST).strftime("%Y-%m-%d %H:%M:%S")
    with trader.connect_db() as conn:
        cursor = conn.execute(
            """
            UPDATE approvals
            SET status = 'executing', response_msg = 'Submitting order to broker', updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (now, approval_id),
        )
    if cursor.rowcount != 1:
        current = _approval_by_id(approval_id)
        if current is None:
            raise HTTPException(status_code=404, detail="approval not found")
        raise HTTPException(status_code=409, detail=f"approval is already {current['status']}")
    return item


def _approval_response_msg(result: dict, *, ok: bool) -> str:
    response_msg = str(result.get("msg1", ""))
    if ok and not trader.DRY_RUN and trader.TRADING_ENV == "demo":
        response_msg = f"{response_msg} (KIS 모의투자 주문 접수 완료, 체결 여부는 주문내역 동기화 후 확인)"
    return response_msg


def _current_holding_qty_from_balance(api, symbol: str) -> int:
    try:
        parsed = _parse_balance(_get_balance_data(api, allow_cache=True))
    except DashboardOperationError:
        return 0
    for holding in parsed.get("holdings", []):
        if str(holding.get("symbol") or "") == str(symbol):
            return _to_int(holding.get("qty"))
    return 0


def _pending_approval_ids(limit: int = 200, *, exclude_sources: set[str] | None = None) -> list[int]:
    _init_approval_db()
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        if exclude_sources:
            placeholders = ", ".join("?" for _ in exclude_sources)
            rows = conn.execute(
                f"""
                SELECT id FROM approvals
                WHERE status = 'pending'
                  AND COALESCE(source, '') NOT IN ({placeholders})
                ORDER BY id ASC
                LIMIT ?
                """,
                (*sorted(exclude_sources), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM approvals WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
    return [int(row["id"]) for row in rows]


def _is_approval_already_claimed(exc: Exception) -> bool:
    detail = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
    return "approval is already" in detail


def _auto_approve_pending_approvals(limit: int = 200) -> list[dict]:
    results = []
    for approval_id in _pending_approval_ids(limit, exclude_sources=AUTO_APPROVAL_EXCLUDED_SOURCES):
        try:
            results.append(_approve_pending_approval(approval_id, "자동승인"))
        except Exception as exc:
            if _is_approval_already_claimed(exc):
                logger.debug(f"auto approval skipped approval_id={approval_id}: {exc}")
                continue
            logger.warning(f"auto approval failed for approval_id={approval_id}: {exc}")
            continue
    return results


def _approve_pending_approval(approval_id: int, approval_label: str = "수동승인") -> dict:
    # Explicit sell-all batches, the periodic sweeper, scheduler jobs, and
    # manual approvals can all reach this function concurrently. Serialize the
    # broker-facing path so KIS sees one approval order at a time.
    with _approval_submission_lock:
        return _approve_pending_approval_serialized(approval_id, approval_label)


def _approve_pending_approval_serialized(
    approval_id: int,
    approval_label: str = "수동승인",
) -> dict:
    from src.online_access import is_online_access_blocked

    if is_online_access_blocked():
        raise HTTPException(
            status_code=409,
            detail="Online access is blocked. Approval remains pending.",
        )
    pending = _load_pending_approval(approval_id)
    if (
        str(pending.get("action") or "").lower() == "buy"
        and Path(".runtime/kill_switch.json").exists()
    ):
        raise HTTPException(
            status_code=409,
            detail="Kill switch is active. Buy approval remains pending.",
        )
    if pending.get("managed_order_id"):
        from src.strategy.autonomy.ai_stock_integration import (
            approve_managed_ai_stock_order,
        )

        try:
            return approve_managed_ai_stock_order(approval_id)
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail=f"managed AI-stock approval failed closed: {exc}",
            ) from exc
    item = _claim_pending_approval(approval_id)
    result: dict = {}
    status = "failed"
    response_msg = "Order submission did not complete"
    try:
        api = _get_api()
        pre_order_qty = _current_holding_qty_from_balance(api, item["symbol"])
        result = api.place_order(item["symbol"], item["action"], item["price"], item["qty"])
        ok = result.get("rt_cd") == "0"
        status = "executed" if ok else "failed"
        response_msg = _approval_response_msg(result, ok=ok)
        if False:  # legacy non-English broker note disabled
            response_msg = f"{response_msg} (주문 접수 완료 - 실제 체결 여부는 HTS/MTS에서 확인 필요)"
        trader.save_trade(
            item["symbol"],
            item["name"],
            item["action"],
            item["qty"],
            item["price"],
            item["reason"],
            ok,
            trader.ORDER_SUBMISSION_ENABLED,
            broker_result=result,
            order_status="submitted" if ok and trader.ORDER_SUBMISSION_ENABLED else "simulated" if ok else "failed",
            response_msg=response_msg,
            filled_qty=0 if ok and trader.ORDER_SUBMISSION_ENABLED else item["qty"] if ok else 0,
            filled_price=0 if ok and trader.ORDER_SUBMISSION_ENABLED else item["price"] if ok else 0,
            pre_order_qty=pre_order_qty,
            strategy_id=item.get("strategy_id"),
            strategy_version=_to_int(item.get("strategy_version")) or None,
            profile_hash=item.get("profile_hash"),
            source_approval_id=approval_id,
        )
    except Exception as e:
        status = "failed"
        response_msg = str(e)
        logger.warning(f"approval order submission failed approval_id={approval_id}: {e}")

    now = trader.datetime.now(trader.KST).strftime("%Y-%m-%d %H:%M:%S")
    with trader.connect_db() as conn:
        conn.execute(
            "UPDATE approvals SET status = ?, response_msg = ?, updated_at = ? WHERE id = ?",
            (status, response_msg, now, approval_id),
        )

    # Slack 알림
    try:
        indicators = {"rsi": "-", "sma20": 0, "sma60": 0, "rt": 0}
        _slack_order(
            item["name"], item["symbol"], item["action"],
            item["qty"], item["price"],
            f"[대시보드 {approval_label}] {item.get('reason', '')}",
            status == "executed",
            indicators,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning(f"Failed to send approval notification: {exc}")

    # 주문이 실제 체결/제출되었으면 잔고가 바뀌므로 잔고·파생 탭 스냅샷을 무효화해
    # 다음 read에서 최신 상태를 다시 받아오게 한다.
    if status == "executed":
        _clear_balance_cache()

    return {"id": approval_id, "status": status, "response_msg": response_msg}



import time

_cloud_trades_cache = None
_cloud_trades_cache_time = 0

def fetch_cloud_trades():
    global _cloud_trades_cache, _cloud_trades_cache_time
    if _cloud_trades_cache is not None and time.time() - _cloud_trades_cache_time < 10:
        return [dict(t) for t in _cloud_trades_cache]
    from src.online_access import is_online_access_blocked

    if is_online_access_blocked():
        return [dict(t) for t in (_cloud_trades_cache or [])]
        
    try:
        subprocess.run(
            ["git", "fetch", "origin", "database:database"],
            check=False,
            capture_output=True,
            timeout=GIT_FETCH_TIMEOUT_SECONDS,
        )
        output = subprocess.check_output(
            ["git", "show", "origin/database:trades.json"],
            stderr=subprocess.STDOUT,
            timeout=GIT_FETCH_TIMEOUT_SECONDS,
        ).decode("utf-8")
        trades = json.loads(output)
        
        _cloud_trades_cache = trades
        _cloud_trades_cache_time = time.time()
        return [dict(t) for t in trades]
    except DashboardOperationError as e:
        if _cloud_trades_cache is not None:
            return [dict(t) for t in _cloud_trades_cache]
        return []


def _load_merged_trades() -> list[dict]:
    cloud_trades = fetch_cloud_trades() or []
    local_trades = []
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades ORDER BY ts ASC").fetchall()
        local_trades = [dict(row) for row in rows]

    merged_trades = {}
    for t in cloud_trades + local_trades:
        ts = t.get("ts") or t.get("timestamp")
        if not ts:
            continue
        ts_norm = str(ts).replace("T", " ").split(".")[0].strip()
        broker_order_id = str(t.get("broker_order_id") or "").strip()
        source_approval_id = str(t.get("source_approval_id") or "").strip()
        strategy_id = str(t.get("strategy_id") or "").strip()
        trade_env = str(t.get("env") or "demo")
        if broker_order_id:
            key = f"broker:{trade_env}:{ts_norm[:10]}:{broker_order_id}:{t.get('action')}"
        elif source_approval_id:
            key = f"approval:{trade_env}:{source_approval_id}:{t.get('action')}"
        else:
            key = ":".join([
                "fill", ts_norm, str(t.get("symbol") or ""), str(t.get("action") or ""),
                str(t.get("qty") or ""), str(t.get("price") or ""), strategy_id,
                trade_env,
            ])
        merged_trades[key] = {
            "id": t.get("id"),
            "ts": ts_norm,
            "symbol": t.get("symbol"),
            "name": t.get("name", t.get("symbol")),
            "action": t.get("action"),
            "qty": _to_int(t.get("qty")),
            "price": _to_int(t.get("price")),
            "reason": t.get("reason", ""),
            "ok": t.get("ok", 1),
            "env": t.get("env", "demo"),
            "dry_run": t.get("dry_run", 0),
            "broker_order_id": t.get("broker_order_id", ""),
            "order_status": t.get("order_status", ""),
            "filled_qty": _to_int(t.get("filled_qty")),
            "filled_price": _to_int(t.get("filled_price")),
            "response_msg": t.get("response_msg", ""),
            "strategy_id": t.get("strategy_id") or "",
            "strategy_version": t.get("strategy_version"),
            "profile_hash": t.get("profile_hash") or "",
            "source_approval_id": t.get("source_approval_id"),
            "account_key": t.get("account_key") or "",
            "fee": t.get("fee"),
            "tax": t.get("tax"),
            "cost_source": t.get("cost_source") or "unavailable",
        }
    return sorted(merged_trades.values(), key=lambda x: x["ts"])


def _trade_is_ok(trade: dict) -> bool:
    return bool(_to_int(trade.get("ok"), 1))


def _trade_is_dry_run(trade: dict) -> bool:
    return bool(_to_int(trade.get("dry_run"), 0))


def _trade_is_sync_adjustment(trade: dict) -> bool:
    reason = str(trade.get("reason") or "").lower()
    # Broker history imports are actual fills, not synthetic balance adjustments.
    # They must participate in realized-PnL reconstruction.
    if reason.strip() == "broker history import":
        return False
    # Check English terms
    if any(token in reason for token in ("sync", "adjust", "correction", "import")):
        return True
    # Check Korean terms
    if any(token in reason for token in ("동기화", "보정", "조정")):
        return True
    # Detect known mojibake fragments retained in legacy database records.
    broken_tokens = ("利앷텒", "媛뺤젣", "숆린", "蹂댁젙", "섎룞", "꾨씫遺")
    if any(token in reason for token in broken_tokens):
        return True
    return False


def _filled_price_matches_order(trade: dict, *, tolerance: float = 0.30) -> bool:
    filled_price = _to_int(trade.get("filled_price"))
    order_price = _to_int(trade.get("price"))
    if filled_price <= 0 or order_price <= 0:
        return True
    return order_price * (1.0 - tolerance) <= filled_price <= order_price * (1.0 + tolerance)


def _account_trades(trades: list[dict]) -> list[dict]:
    account_rows = []
    # If the trader is running in dry-run/demo mode, or if there are no live trades, show dry-run trades
    show_dry_run = trader.DRY_RUN or (trader.TRADING_ENV == "demo")
    
    for trade in trades:
        if not _trade_is_ok(trade):
            continue
        if _trade_is_sync_adjustment(trade):
            continue
        if not show_dry_run and _trade_is_dry_run(trade):
            continue
            
        order_status = str(trade.get("order_status") or "")
        filled_qty = _to_int(trade.get("filled_qty"))
        filled_price = _to_int(trade.get("filled_price"))
        if order_status in {"submitted", "partial", "open"} and filled_qty <= 0:
            continue
        if filled_qty > 0 and not _filled_price_matches_order(trade):
            if order_status in {"submitted", "partial", "open"}:
                continue
            filled_qty = 0
            filled_price = 0
        if filled_qty > 0:
            trade = {**trade, "qty": filled_qty, "price": filled_price or _to_int(trade.get("price"))}
        account_rows.append(trade)
    return account_rows


def _period_bucket() -> dict:
    return {
        "order_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "buy_amount": 0,
        "sell_amount": 0,
        "realized_pnl": 0,
        "cost_of_sold": 0,
        "realized_pnl_rate": 0.0,
        "net_cashflow": 0,
        "details": [],
    }


def _strategy_label(strategy_id: str) -> str:
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id or strategy_id == "unattributed":
        return "전략 미기록"
    try:
        from src.db.strategy_repository import load_ai_strategies

        strategy = next(
            (item for item in load_ai_strategies() if str(item.get("id") or "") == strategy_id),
            None,
        )
        if strategy:
            return str(strategy.get("name") or strategy.get("title") or strategy_id)
    except Exception:
        pass
    defaults = {
        "seven_split": "7분할 매매",
        "volatility_breakout": "변동성 돌파",
        "rsi_limit_strategy": "RSI 지정가",
        "plunge_bounce_strategy": "급락 반등",
        "issue_sector_rotation_strategy": "이슈 섹터 순환",
        "heikin_ashi_scalping_strategy": "하이킨아시 스캘핑",
    }
    return defaults.get(strategy_id, strategy_id)


def _strategy_validation(strategy_stats: dict[str, dict]) -> list[dict]:
    result = []
    for strategy_id, stats in strategy_stats.items():
        pnls = list(stats.pop("_pnls", []))
        closed_count = len(pnls)
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        win_rate = (len(wins) / closed_count * 100) if closed_count else None
        profit_factor = (gross_profit / gross_loss) if gross_loss else (None if not gross_profit else gross_profit)
        expectancy = (sum(pnls) / closed_count) if closed_count else None
        equity = 0
        peak = 0
        max_drawdown = 0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)

        if closed_count < 5:
            status, reason = "insufficient", "청산 표본 5건 미만"
        elif sum(pnls) > 0 and (win_rate or 0) >= 50 and (profit_factor or 0) >= 1.2:
            status, reason = "effective", "누적손익 양수·승률 50% 이상·손익비 1.2 이상"
        elif sum(pnls) <= 0 or (profit_factor is not None and profit_factor < 1):
            status, reason = "review", "누적손익 또는 손익비가 기준 미달"
        else:
            status, reason = "monitor", "추가 표본과 안정성 확인 필요"

        result.append({
            **stats,
            "strategy_id": strategy_id,
            "strategy_name": _strategy_label(strategy_id),
            "closed_count": closed_count,
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(win_rate, 2) if win_rate is not None else None,
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
            "expectancy": round(expectancy, 0) if expectancy is not None else None,
            "max_drawdown": round(max_drawdown, 0),
            "validation_status": status,
            "validation_reason": reason,
        })
    return sorted(result, key=lambda item: (-item["realized_pnl"], item["strategy_name"]))


_INDEX_ROWS_CACHE: tuple[float, dict[str, list[dict]]] = (0.0, {})
_INDEX_SYMBOL_ALIASES = {
    "KOSPI": ("^KS11", "KOSPI", "0001"),
    "KOSDAQ": ("^KQ11", "KOSDAQ", "1001"),
}


def _load_index_rows() -> dict[str, list[dict]]:
    """Load benchmark closes from local charts first, then best-effort Yahoo data."""
    global _INDEX_ROWS_CACHE
    cached_at, cached_rows = _INDEX_ROWS_CACHE
    if time.monotonic() - cached_at < 300:
        return cached_rows
    series: dict[str, list[dict]] = {}
    try:
        from src.db.repository import connect_db

        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            for name, symbols in _INDEX_SYMBOL_ALIASES.items():
                for symbol in symbols:
                    rows = conn.execute(
                        "SELECT date, close FROM daily_charts WHERE symbol=? AND close>0 "
                        "ORDER BY date DESC LIMIT 90",
                        (symbol,),
                    ).fetchall()
                    if rows:
                        series[name] = [
                            {"date": str(row["date"])[:10], "close": float(row["close"])}
                            for row in reversed(rows)
                        ]
                        break
    except Exception:
        pass

    missing = [name for name in _INDEX_SYMBOL_ALIASES if name not in series]
    if missing:
        try:
            from src.online_access import require_online_access
            import yfinance as yf

            require_online_access("성과 탭 시장지수 조회")
            for name in missing:
                ticker = _INDEX_SYMBOL_ALIASES[name][0]
                data = yf.download(
                    ticker, period="6mo", interval="1d", auto_adjust=False,
                    progress=False, threads=False, timeout=5,
                )
                if data is None or data.empty:
                    continue
                close = data["Close"]
                if getattr(close, "ndim", 1) > 1:
                    close = close.iloc[:, 0]
                series[name] = [
                    {"date": str(index)[:10], "close": float(value)}
                    for index, value in close.dropna().items()
                ]
        except Exception as exc:
            logger.info(f"Performance benchmark data unavailable: {exc}")
    _INDEX_ROWS_CACHE = (time.monotonic(), series)
    return series


def _daily_market_context(index_rows: dict[str, list[dict]]) -> dict[str, dict]:
    context: dict[str, dict] = {}
    for name, rows in index_rows.items():
        closes = [float(row["close"]) for row in rows]
        for idx, row in enumerate(rows):
            change_pct = None
            if idx and closes[idx - 1] > 0:
                change_pct = (closes[idx] / closes[idx - 1] - 1) * 100
            day = context.setdefault(row["date"], {})
            day[name.lower()] = round(float(row["close"]), 2)
            day[f"{name.lower()}_change_pct"] = round(change_pct, 2) if change_pct is not None else None
    return context


def _monthly_market_context(index_rows: dict[str, list[dict]]) -> dict[str, dict]:
    context: dict[str, dict] = {}
    for name, rows in index_rows.items():
        by_month: dict[str, list[float]] = {}
        for row in rows:
            date = str(row.get("date") or "")
            close = float(row.get("close") or 0)
            if len(date) >= 7 and close > 0:
                by_month.setdefault(date[:7], []).append(close)
        previous_close = None
        for month, closes in sorted(by_month.items()):
            close = closes[-1]
            change_pct = None
            if previous_close and previous_close > 0:
                change_pct = (close / previous_close - 1) * 100
            bucket = context.setdefault(month, {})
            bucket[name.lower()] = round(close, 2)
            bucket[f"{name.lower()}_change_pct"] = (
                round(change_pct, 2) if change_pct is not None else None
            )
            previous_close = close
    return context


def _load_symbol_price_rows(symbols: set[str], *, limit: int = 1500) -> dict[str, list[dict]]:
    """Load as-of closes for forward paper-performance reconstruction."""
    if not symbols:
        return {}
    result: dict[str, list[dict]] = {}
    from src.db.repository import connect_db

    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        for symbol in sorted(symbols):
            rows = conn.execute(
                "SELECT date, close FROM daily_charts WHERE symbol=? AND close>0 "
                "ORDER BY date DESC LIMIT ?",
                (symbol, int(limit)),
            ).fetchall()
            if rows:
                result[symbol] = [
                    {"date": str(row["date"])[:10], "close": float(row["close"])}
                    for row in reversed(rows)
                ]
    return result


def _load_long_benchmark_rows() -> dict[str, list[dict]]:
    aliases = {
        code: _load_symbol_price_rows(set(symbols), limit=1500)
        for code, symbols in _INDEX_SYMBOL_ALIASES.items()
    }
    result: dict[str, list[dict]] = {}
    for code, symbols in _INDEX_SYMBOL_ALIASES.items():
        by_symbol = aliases.get(code, {})
        candidates = [by_symbol[symbol] for symbol in symbols if by_symbol.get(symbol)]
        if candidates:
            result[code] = max(candidates, key=lambda rows: len(rows))
    fallback = _load_index_rows()
    for code, rows in fallback.items():
        result.setdefault(code, rows)
    return result


def _build_forward_strategy_performance(
    trades: list[dict], *, strategy_id: str | None = None
) -> list[dict]:
    from src.db.performance_repository import (
        account_scope_key,
        list_strategy_performance_reviews,
        replace_daily_nav,
    )
    from src.db.strategy_repository import load_ai_strategies
    from src.strategy.forward_performance import build_strategy_forward_performance

    account_trades = [
        trade for trade in _account_trades(trades)
        if str(trade.get("env") or trader.TRADING_ENV) == str(trader.TRADING_ENV)
    ]
    if strategy_id:
        account_trades = [
            trade for trade in account_trades
            if str(trade.get("strategy_id") or "unattributed") == strategy_id
        ]
    symbols = {
        str(trade.get("symbol") or "").strip()
        for trade in account_trades
        if str(trade.get("symbol") or "").strip()
    }
    price_rows = _load_symbol_price_rows(symbols)
    benchmark_rows = _load_long_benchmark_rows()
    names = {
        str(item.get("id")): str(item.get("name") or item.get("id"))
        for item in load_ai_strategies()
        if item.get("id")
    }
    reviews = {
        str(item.get("strategy_id")): item
        for item in list_strategy_performance_reviews()
    }
    now_kst = trader.datetime.now(trader.KST)
    as_of = now_kst.date()
    if now_kst.hour < 16:
        as_of -= trader.timedelta(days=1)
    results = build_strategy_forward_performance(
        account_trades,
        price_rows,
        benchmark_rows,
        as_of=as_of.isoformat(),
        strategy_names=names,
        reviews=reviews,
    )
    current_account_key = account_scope_key()
    for row in results:
        issues = row.setdefault("quality_issues", [])
        strategy_trade_rows = [
            trade for trade in account_trades
            if str(trade.get("strategy_id") or "unattributed") == row["strategy_id"]
        ]
        identity_available = bool(strategy_trade_rows) and all(
            str(trade.get("account_key") or "") == current_account_key
            for trade in strategy_trade_rows
        )
        if not identity_available and "account_identity_unavailable" not in issues:
            issues.append("account_identity_unavailable")
            row.setdefault("quality", {}).setdefault("warnings", []).append(
                "account_identity_unavailable"
            )
        row["data_quality"] = "estimated"
        row["attribution_reliable"] = bool(
            row.get("quality", {}).get("status") != "blocked"
            and identity_available
        )
        # Synthetic capital and excluded costs are suitable for monitoring,
        # not for claiming broker-net performance accuracy.
        row["reliable"] = False
        replace_daily_nav(
            row["strategy_id"],
            row.get("daily_nav") or [],
            scope_type="account" if row["strategy_id"] == "__account__" else "strategy",
            input_hash=row["input_hash"],
        )
    return results


def _build_forward_account_performance(trades: list[dict]) -> dict | None:
    from src.db.performance_repository import build_account_equity_performance
    account_rows = [
        {**trade, "strategy_id": "__account__"}
        for trade in trades
    ]
    rows = _build_forward_strategy_performance(account_rows, strategy_id="__account__")
    if not rows:
        return None
    result = {
        **rows[0],
        "strategy_id": "__account__",
        "strategy_name": "전체 모의계좌 체결 원장",
        "scope": "account",
    }
    broker_nav = build_account_equity_performance()
    if broker_nav.get("available"):
        benchmark_rows = _load_long_benchmark_rows()
        sessions = [row["session_date"] for row in broker_nav.get("daily", [])]
        for code in ("KOSPI", "KOSDAQ"):
            rows_by_date = {
                str(item.get("date") or "")[:10]: float(item.get("close") or 0)
                for item in benchmark_rows.get(code, [])
                if float(item.get("close") or 0) > 0
            }
            index_value = 100.0
            valid = True
            ordered_dates = sorted(rows_by_date)
            for session in sessions[1:]:
                previous_dates = [day for day in ordered_dates if day < session]
                current = rows_by_date.get(session)
                previous = rows_by_date.get(previous_dates[-1]) if previous_dates else None
                if not current or not previous:
                    valid = False
                    break
                index_value *= current / previous
            broker_nav[f"{code.lower()}_twr_pct"] = round(index_value - 100, 2) if valid else None
        broker_nav["excess_twr_vs_kospi_pct"] = (
            round(broker_nav["twr_pct"] - broker_nav["kospi_twr_pct"], 2)
            if broker_nav.get("kospi_twr_pct") is not None else None
        )
    result["broker_account_nav"] = broker_nav
    return result


def _build_periodic_performance(trades: list[dict]) -> dict:
    daily: dict[str, dict] = {}
    monthly: dict[str, dict] = {}
    holdings: dict[tuple[str, str], dict] = {}
    strategy_stats: dict[str, dict] = {}

    for trade in _account_trades(trades):
        ts = str(trade.get("ts") or "")
        if len(ts) < 10 or ts[0] == "-":
            continue

        day_key = ts[:10]
        month_key = ts[:7]
        action = str(trade.get("action") or "").lower()
        symbol = str(trade.get("symbol") or "")
        strategy_id = str(trade.get("strategy_id") or "unattributed")
        strategy_name = _strategy_label(strategy_id)
        qty = _to_int(trade.get("qty"))
        price = _to_int(trade.get("price"))
        amount = qty * price

        if qty <= 0 or price <= 0 or action not in {"buy", "sell"}:
            continue

        day = daily.setdefault(day_key, _period_bucket())
        month = monthly.setdefault(month_key, _period_bucket())
        for bucket in (day, month):
            bucket["order_count"] += 1
            if action == "buy":
                bucket["buy_count"] += 1
                bucket["buy_amount"] += amount
            else:
                bucket["sell_count"] += 1
                bucket["sell_amount"] += amount

        holding_key = (strategy_id, symbol)
        if holding_key not in holdings:
            holdings[holding_key] = {"qty": 0, "avg_cost": 0.0}
        holding = holdings[holding_key]
        stats = strategy_stats.setdefault(strategy_id, {
            "order_count": 0, "buy_count": 0, "sell_count": 0,
            "realized_pnl": 0, "_pnls": [],
        })
        stats["order_count"] += 1
        stats[f"{action}_count"] += 1

        if action == "buy":
            total_qty = holding["qty"] + qty
            total_cost = holding["qty"] * holding["avg_cost"] + amount
            holding["qty"] = total_qty
            holding["avg_cost"] = total_cost / total_qty if total_qty > 0 else 0.0
            detail = {
                "ts": ts,
                "symbol": symbol,
                "name": trade.get("name") or symbol,
                "action": action,
                "qty": qty,
                "price": price,
                "amount": amount,
                "realized_pnl": 0,
                "cost_of_sold": 0,
                "realized_pnl_rate": 0.0,
                "reason": trade.get("reason", ""),
                "order_status": trade.get("order_status", ""),
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
            }
        else:
            sell_qty = min(qty, holding["qty"])
            cost_of_shares_sold = int(holding["avg_cost"] * sell_qty)
            realized = int((price - holding["avg_cost"]) * sell_qty)
            
            day["realized_pnl"] += realized
            month["realized_pnl"] += realized
            day["cost_of_sold"] += cost_of_shares_sold
            month["cost_of_sold"] += cost_of_shares_sold
            stats["realized_pnl"] += realized
            if sell_qty > 0:
                stats["_pnls"].append(realized)
            
            holding["qty"] = max(0, holding["qty"] - sell_qty)
            if holding["qty"] <= 0:
                holding["avg_cost"] = 0.0
            detail = {
                "ts": ts,
                "symbol": symbol,
                "name": trade.get("name") or symbol,
                "action": action,
                "qty": qty,
                "price": price,
                "amount": amount,
                "realized_pnl": realized,
                "cost_of_sold": cost_of_shares_sold,
                "realized_pnl_rate": round((realized / cost_of_shares_sold * 100), 2)
                if cost_of_shares_sold > 0
                else 0.0,
                "reason": trade.get("reason", ""),
                "order_status": trade.get("order_status", ""),
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
            }

        day["details"].append(detail)
        month["details"].append(detail)

    for rows in (daily, monthly):
        for bucket in rows.values():
            bucket["net_cashflow"] = bucket["sell_amount"] - bucket["buy_amount"]
            bucket["realized_pnl_rate"] = round((bucket["realized_pnl"] / bucket["cost_of_sold"] * 100), 2) if bucket["cost_of_sold"] > 0 else 0.0

    index_rows = _load_index_rows()
    market_context = _daily_market_context(index_rows)
    monthly_market_context = _monthly_market_context(index_rows)
    daily_rows = [
        {"period": key, **value, **market_context.get(key, {})}
        for key, value in sorted(daily.items())
    ]
    return {
        "daily": daily_rows,
        "monthly": [
            {"period": key, **value, **monthly_market_context.get(key, {})}
            for key, value in sorted(monthly.items())
        ],
        "strategy_validation": _strategy_validation(strategy_stats),
        "market_data_available": bool(market_context),
    }




def _sync_filled_trades_from_history(
    api,
    *,
    days: int = 90,
    history: list[dict] | None = None,
) -> dict:
    from src.db.performance_repository import account_scope_key
    start_date, end_date = _order_history_window(days)
    if history is None:
        history = api.get_trade_history(start_date, end_date)
    trader.init_db()

    merged_trades = _load_merged_trades()
    existing = {_history_trade_key(item): item for item in merged_trades}
    def broker_history_key(item: dict) -> tuple[str, str, str, str, str]:
        return (
            str(item.get("env") or trader.TRADING_ENV),
            str(item.get("ts") or "")[:10],
            str(item.get("broker_order_id") or "").strip(),
            str(item.get("symbol") or ""),
            str(item.get("action") or ""),
        )

    existing_by_broker_order_id = {
        broker_history_key(item): item
        for item in merged_trades
        if str(item.get("broker_order_id") or "").strip()
    }
    active_by_broker_order_id = {
        (
            str(item.get("env") or trader.TRADING_ENV),
            str(item.get("broker_order_id") or "").strip(),
            str(item.get("symbol") or ""),
            str(item.get("action") or ""),
        ): item
        for item in merged_trades
        if str(item.get("broker_order_id") or "").strip()
        and str(item.get("order_status") or "") in {"submitted", "open", "partial"}
    }
    imported_count = 0
    skipped_count = 0
    updated_count = 0
    items = []

    with trader.connect_db() as conn:
        for row in history:
            trade = _history_row_to_trade(row)
            if not trade:
                skipped_count += 1
                items.append({
                    "sync_type": "history",
                    "sync_result": "skipped",
                    "ts": _history_timestamp(row),
                    "symbol": _history_symbol(row),
                    "name": _history_name(row),
                    "action": _history_action(row),
                    "qty": _history_fill_qty(row),
                    "price": _history_fill_price(row),
                    "broker_order_id": _broker_order_id_from_history(row),
                    "order_status": "unrecognized",
                    "message": "체결 거래로 해석할 수 없어 제외",
                })
                continue

            key = _history_trade_key(trade)
            broker_order_id = str(trade.get("broker_order_id") or "").strip()
            stored = existing.get(key) or existing_by_broker_order_id.get(
                broker_history_key(trade)
            )
            if stored is None:
                stored = active_by_broker_order_id.get((
                    str(trade.get("env") or trader.TRADING_ENV),
                    broker_order_id,
                    str(trade.get("symbol") or ""),
                    str(trade.get("action") or ""),
                ))
            if stored is not None:
                item_result = "skipped"
                item_message = "이미 저장된 체결 기록"
                stored_state = (
                    str(stored.get("order_status") or ""),
                    _to_int(stored.get("filled_qty")),
                    _to_int(stored.get("filled_price")),
                )
                requested_qty = _to_int(stored.get("qty"))
                filled_qty = _to_int(trade.get("filled_qty"))
                remaining_qty = _history_remaining_qty(row)
                if _history_order_is_canceled(row):
                    incoming_status = "canceled"
                elif _history_order_is_rejected(row) and filled_qty <= 0:
                    incoming_status = "failed"
                elif remaining_qty > 0 and filled_qty <= 0:
                    incoming_status = "open"
                elif remaining_qty > 0 or (
                    requested_qty > 0 and filled_qty < requested_qty
                ):
                    incoming_status = "partial"
                else:
                    incoming_status = "filled"
                incoming_state = (
                    incoming_status,
                    filled_qty,
                    _to_int(trade.get("filled_price")),
                )
                needs_account_key = not str(stored.get("account_key") or "").strip()
                if trade["broker_order_id"] and (stored_state != incoming_state or needs_account_key):
                    stored_id = _to_int(stored.get("id"))
                    where_sql = (
                        "id = ?"
                        if stored_id > 0
                        else "broker_order_id = ? AND symbol = ? AND action = ? AND env = ? AND substr(ts, 1, 10) = ?"
                    )
                    where_values = (
                        (stored_id,)
                        if stored_id > 0
                        else (
                            trade["broker_order_id"],
                            trade["symbol"],
                            trade["action"],
                            trade["env"],
                            str(trade["ts"])[:10],
                        )
                    )
                    cursor = conn.execute(
                        f"""
                        UPDATE trades
                        SET order_status = ?,
                            filled_qty = ?,
                            filled_price = ?,
                            response_msg = ?,
                            broker_result = ?,
                            account_key = COALESCE(NULLIF(account_key, ''), ?)
                        WHERE {where_sql}
                        """,
                        (
                            incoming_status,
                            trade["filled_qty"],
                            trade["filled_price"],
                            trade["response_msg"],
                            trade["broker_result"],
                            account_scope_key(),
                            *where_values,
                        ),
                    )
                    updated_count += int(cursor.rowcount)
                    if cursor.rowcount:
                        item_result = "updated"
                        item_message = "기존 거래의 체결 상태 갱신"
                        logger.info(
                            "[TRADE_IMPORT_UPDATE] "
                            f"symbol={trade['symbol']} action={trade['action']} "
                            f"qty={trade['qty']} status={trade['order_status']} "
                            f"filled_qty={trade['filled_qty']} filled_price={trade['filled_price']} "
                            f"broker_order_id={trade['broker_order_id'] or '-'}"
                        )
                items.append({
                    "sync_type": "history",
                    "sync_result": item_result,
                    "ts": trade["ts"],
                    "symbol": trade["symbol"],
                    "name": trade["name"],
                    "action": trade["action"],
                    "qty": trade["filled_qty"] or trade["qty"],
                    "price": trade["filled_price"] or trade["price"],
                    "broker_order_id": trade["broker_order_id"],
                    "order_status": trade["order_status"],
                    "message": item_message,
                })
                skipped_count += 1
                continue

            conn.execute(
                """
                INSERT INTO trades (
                    ts, symbol, name, action, qty, price, reason, ok, env, dry_run,
                    broker_order_id, order_status, filled_qty, filled_price, pre_order_qty, response_msg, broker_result,
                    account_key, fee, tax, cost_source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade["ts"],
                    trade["symbol"],
                    trade["name"],
                    trade["action"],
                    trade["qty"],
                    trade["price"],
                    trade["reason"],
                    trade["ok"],
                    trade["env"],
                    trade["dry_run"],
                    trade["broker_order_id"],
                    trade["order_status"],
                    trade["filled_qty"],
                    trade["filled_price"],
                    0,
                    trade["response_msg"],
                    trade["broker_result"],
                    account_scope_key(),
                    None,
                    None,
                    "unavailable",
                ),
            )
            logger.info(
                "[TRADE_IMPORT] "
                f"symbol={trade['symbol']} action={trade['action']} qty={trade['qty']} "
                f"price={trade['price']} status={trade['order_status']} "
                f"filled_qty={trade['filled_qty']} filled_price={trade['filled_price']} "
                f"broker_order_id={trade['broker_order_id'] or '-'}"
            )
            existing[key] = trade
            imported_count += 1
            items.append({
                "sync_type": "history",
                "sync_result": "imported",
                "ts": trade["ts"],
                "symbol": trade["symbol"],
                "name": trade["name"],
                "action": trade["action"],
                "qty": trade["filled_qty"] or trade["qty"],
                "price": trade["filled_price"] or trade["price"],
                "broker_order_id": trade["broker_order_id"],
                "order_status": trade["order_status"],
                "message": "증권사 체결 기록 신규 추가",
            })

    return {
        "ok": True,
        "start_date": start_date,
        "end_date": end_date,
        "history_count": len(history),
        "imported_count": imported_count,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "items": items,
    }


def _order_history_window(days: int = MIN_ORDER_HISTORY_SYNC_DAYS) -> tuple[str, str]:
    end = trader.datetime.now(trader.KST)
    start = end - trader.timedelta(days=max(MIN_ORDER_HISTORY_SYNC_DAYS, days))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _load_trackable_order_trades(days: int = MIN_ORDER_HISTORY_SYNC_DAYS) -> list[dict]:
    trader.init_db()
    cutoff = (trader.datetime.now(trader.KST) - trader.timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM trades
            WHERE broker_order_id IS NOT NULL
              AND broker_order_id != ''
              AND (
                    COALESCE(order_status, '') IN ('submitted', 'partial', 'open')
                    OR (
                        COALESCE(order_status, '') = 'filled'
                        AND action = 'sell'
                        AND source_approval_id IS NOT NULL
                    )
                  )
              AND substr(COALESCE(ts, ''), 1, 10) >= ?
            ORDER BY ts ASC
            """,
            (cutoff,),
        ).fetchall()
    return [dict(row) for row in rows]


def _sync_order_status_from_history(
    api,
    *,
    days: int = MIN_ORDER_HISTORY_SYNC_DAYS,
    history: list[dict] | None = None,
) -> dict:
    tracked = _load_trackable_order_trades(days)
    if not tracked:
        return {"ok": True, "checked_count": 0, "updated_count": 0, "orders": []}

    start_date, end_date = _order_history_window(days)
    if history is None:
        try:
            history = api.get_trade_history(start_date, end_date)
        except DashboardOperationError as exc:
            fallback = _sync_order_status_from_balance(api, tracked, reason=str(exc))
            return {
                **fallback,
                "ok": fallback.get("updated_count", 0) > 0,
                "history_error": str(exc),
                "history_count": 0,
                "fallback": "balance",
            }
    orders = []
    updated_count = 0
    unmatched = []
    for trade in tracked:
        order_id = str(trade.get("broker_order_id") or "")
        row = next((item for item in history if _history_matches_tracked_order(item, trade)), None)
        if row is None:
            unmatched.append(trade)
            continue

        requested_qty = _to_int(trade.get("qty"))
        filled_qty = _history_fill_qty(row)
        filled_price = _history_fill_price(row)
        remaining_qty = _history_remaining_qty(row)
        order_date = _history_timestamp(row)[:10]
        today = trader.datetime.now(trader.KST).strftime("%Y-%m-%d")
        expired_with_remainder = bool(order_date and order_date < today and remaining_qty > 0)
        if _history_order_is_canceled(row) or expired_with_remainder:
            order_status = "canceled"
        elif _history_order_is_rejected(row) and filled_qty <= 0:
            order_status = "failed"
        elif remaining_qty > 0 and filled_qty <= 0:
            order_status = "open"
        elif remaining_qty > 0 or (requested_qty > 0 and filled_qty < requested_qty):
            order_status = "partial"
        else:
            order_status = "filled"
        response_msg = f"KIS order history sync: {order_status}"
        status_changed = str(trade.get("order_status") or "") != order_status
        quantity_changed = _to_int(trade.get("filled_qty")) != filled_qty
        price_changed = filled_price > 0 and _to_int(trade.get("filled_price")) != filled_price
        if status_changed or quantity_changed or price_changed:
            updated_count += trader.update_trade_order_status(
                order_id,
                trade_id=_to_int(trade.get("id")) or None,
                order_status=order_status,
                filled_qty=filled_qty,
                filled_price=filled_price,
                response_msg=response_msg,
                broker_result=row,
            )
        orders.append({
            "broker_order_id": order_id,
            "symbol": trade.get("symbol", ""),
            "name": trade.get("name", ""),
            "action": trade.get("action", ""),
            "order_status": order_status,
            "filled_qty": filled_qty,
            "filled_price": filled_price,
        })

    balance_sync = _sync_order_status_from_balance(
        api,
        unmatched,
        reason="order absent from KIS history",
        close_unreserved_sells=True,
    ) if unmatched else {"ok": True, "checked_count": 0, "updated_count": 0, "orders": []}
    updated_count += int(balance_sync.get("updated_count", 0) or 0)
    orders.extend(balance_sync.get("orders", []) or [])

    return {
        "ok": bool(balance_sync.get("ok", True)),
        "checked_count": len(tracked),
        "updated_count": updated_count,
        "history_count": len(history),
        "unmatched_count": len(unmatched),
        "balance_checked_count": int(balance_sync.get("checked_count", 0) or 0),
        "orders": orders,
    }


def _sync_order_status_from_balance(
    api,
    tracked: list[dict],
    *,
    reason: str = "",
    close_unreserved_sells: bool = False,
) -> dict:
    try:
        parsed = _parse_balance(_get_balance_data(api, allow_cache=False))
    except DashboardOperationError as exc:
        return {
            "ok": False,
            "checked_count": len(tracked),
            "updated_count": 0,
            "orders": [],
            "balance_error": str(exc),
            "history_error": reason,
        }

    holdings = {str(item.get("symbol") or ""): item for item in parsed.get("holdings", [])}
    orders = []
    updated_count = 0
    for trade in tracked:
        order_id = str(trade.get("broker_order_id") or "")
        symbol = str(trade.get("symbol") or "")
        action = str(trade.get("action") or "").lower()
        requested_qty = _to_int(trade.get("qty"))
        pre_order_qty = _to_int(trade.get("pre_order_qty"))
        current = holdings.get(symbol, {})
        current_qty = _to_int(current.get("qty"))
        sellable_qty = _to_int(current.get("sellable_qty"))
        current_price = _to_int(current.get("price")) or _to_int(trade.get("price"))

        filled = False
        if action == "buy" and requested_qty > 0:
            filled = current_qty >= pre_order_qty + requested_qty
        elif action == "sell" and requested_qty > 0:
            filled = current_qty <= max(0, pre_order_qty - requested_qty)

        if not filled:
            inferred_filled_qty = (
                min(requested_qty, max(_to_int(trade.get("filled_qty")), pre_order_qty - current_qty))
                if action == "sell"
                else _to_int(trade.get("filled_qty"))
            )
            sell_is_unreserved = (
                close_unreserved_sells
                and action == "sell"
                and bool(current)
                and current_qty > 0
                and sellable_qty >= current_qty
            )
            if sell_is_unreserved:
                order_status = "partial" if inferred_filled_qty > 0 else "canceled"
                response_msg = f"Balance reconciliation: {order_status} (no active sell reservation)"
                updated_count += trader.update_trade_order_status(
                    order_id,
                    trade_id=_to_int(trade.get("id")) or None,
                    order_status=order_status,
                    filled_qty=inferred_filled_qty,
                    filled_price=current_price if inferred_filled_qty > 0 else 0,
                    response_msg=response_msg,
                    broker_result={
                        "fallback": "balance",
                        "history_error": reason,
                        "pre_order_qty": pre_order_qty,
                        "current_qty": current_qty,
                        "sellable_qty": sellable_qty,
                    },
                )
                orders.append({
                    "broker_order_id": order_id,
                    "symbol": symbol,
                    "name": trade.get("name", ""),
                    "action": action,
                    "order_status": order_status,
                    "filled_qty": inferred_filled_qty,
                    "filled_price": current_price if inferred_filled_qty > 0 else 0,
                    "balance_confirmed": True,
                    "sell_reservation_active": False,
                })
                continue
            orders.append({
                "broker_order_id": order_id,
                "symbol": symbol,
                "name": trade.get("name", ""),
                "action": action,
                "order_status": trade.get("order_status") or "submitted",
                "balance_confirmed": False,
            })
            continue

        response_msg = "Balance fallback sync: filled"
        updated_count += trader.update_trade_order_status(
            order_id,
            trade_id=_to_int(trade.get("id")) or None,
            order_status="filled",
            filled_qty=requested_qty,
            filled_price=current_price,
            response_msg=response_msg,
            broker_result={
                "fallback": "balance",
                "history_error": reason,
                "pre_order_qty": pre_order_qty,
                "current_qty": current_qty,
            },
        )
        orders.append({
            "broker_order_id": order_id,
            "symbol": symbol,
            "name": trade.get("name", ""),
            "action": action,
            "order_status": "filled",
            "filled_qty": requested_qty,
            "filled_price": current_price,
            "balance_confirmed": True,
        })

    return {
        "ok": True,
        "checked_count": len(tracked),
        "updated_count": updated_count,
        "orders": orders,
    }



# =============================================================================
# Executor 상태 (스위치) API
# =============================================================================


@app.get("/api/futures-signals/executor/state")
async def get_executor_state():
    """실행 상태 조회 (스위치 ON/OFF 현황)"""
    from src.futures_signals.executor import get_executor
    from dataclasses import asdict
    executor = get_executor()
    return asdict(executor.state)



@app.put("/api/futures-signals/executor/state")
async def update_executor_state(request: Request):
    """스위치 ON/OFF 변경"""
    from src.futures_signals.executor import get_executor
    from dataclasses import asdict
    body = await request.json()
    executor = get_executor()
    executor.update_state(**body)
    return {"ok": True, "state": asdict(executor.state)}


# =============================================================================
# 성과 조회 API
# =============================================================================


@app.get("/api/futures-signals/performance/mock")
async def get_mock_performance():
    """Mock 시뮬레이터 성과"""
    from src.futures_signals.executor import get_executor
    executor = get_executor()
    return executor.get_mock_performance()



@app.get("/api/futures-signals/performance/paper")
async def get_paper_performance():
    """KIS 모의계좌 성과"""
    try:
        from src.api.kis_futures_api import KISFuturesAPI
        api = KISFuturesAPI(demo=True)
        if not api._configured:
            return {"status": "not_configured", "demo": True}
        balance = api.get_balance()
        positions = api.get_positions()
        executions = api.get_executions()
        return {
            "balance": balance,
            "positions": positions,
            "executions": executions,
            "demo": True,
        }
    except DashboardOperationError as e:
        return {"status": "error", "message": str(e), "demo": True}



@app.get("/api/futures-signals/performance/live")
async def get_live_performance():
    """KIS 실계좌 성과"""
    try:
        from src.futures_signals.executor import get_executor
        executor = get_executor()
        if not executor.state.live_trading_enabled:
            return {"status": "disabled", "message": "실계좌 거래가 비활성화 상태입니다"}
        from src.api.kis_futures_api import KISFuturesAPI
        api = KISFuturesAPI(demo=False)
        balance = api.get_balance()
        positions = api.get_positions()
        return {
            "balance": balance,
            "positions": positions,
            "demo": False,
        }
    except DashboardOperationError as e:
        return {"status": "error", "message": str(e), "demo": False}


# =============================================================================
# Telegram 단계별 인증 API
# =============================================================================

# Telegram 인증 상태 저장 (메모리)
_telegram_auth_state: dict = {"step": "idle", "phone_code_hash": None}



@app.post("/api/futures-signals/collector/auth/start")
async def telegram_auth_start(request: Request):
    """
    Telegram 인증 1단계: 전화번호로 SMS 코드 발송
    body: {"phone": "+821012345678"}
    """
    global _telegram_auth_state
    body = await request.json()
    phone = body.get("phone", "")

    from src.online_access import require_online_access

    require_online_access("Telegram authentication")
    try:
        from telethon import TelegramClient
    except ImportError:
        return {"ok": False, "error": "telethon not installed"}

    try:
        from src.config import config as _cfg
        api_id = int(_cfg.telegram_api_id or 0) if hasattr(_cfg, "telegram_api_id") else int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
        api_hash = (_cfg.telegram_api_hash or "") if hasattr(_cfg, "telegram_api_hash") else (os.environ.get("TELEGRAM_API_HASH", "") or "")
        session_path = str(Path(".runtime/telegram_session"))

        if not api_id or not api_hash:
            return {"ok": False, "error": "TELEGRAM_API_ID 또는 TELEGRAM_API_HASH가 설정되지 않았습니다"}

        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        result = await client.send_code_request(phone)
        _telegram_auth_state = {
            "step": "code_sent",
            "phone": phone,
            "phone_code_hash": result.phone_code_hash,
        }
        await client.disconnect()
        return {"ok": True, "message": f"{phone}으로 인증 코드가 발송되었습니다"}
    except DashboardOperationError as e:
        return {"ok": False, "error": str(e)}



@app.post("/api/futures-signals/collector/auth/verify")
async def telegram_auth_verify(request: Request):
    """
    Telegram 인증 2단계: SMS 코드 입력으로 세션 생성
    body: {"code": "12345"}
    """
    global _telegram_auth_state
    body = await request.json()
    code = body.get("code", "")

    from src.online_access import require_online_access

    require_online_access("Telegram authentication")
    if _telegram_auth_state.get("step") != "code_sent":
        return {"ok": False, "error": "먼저 인증 코드를 발송해주세요"}

    try:
        from telethon import TelegramClient
    except ImportError:
        return {"ok": False, "error": "telethon not installed"}

    try:
        from src.config import config as _cfg
        api_id = int(_cfg.telegram_api_id or 0) if hasattr(_cfg, "telegram_api_id") else int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
        api_hash = (_cfg.telegram_api_hash or "") if hasattr(_cfg, "telegram_api_hash") else (os.environ.get("TELEGRAM_API_HASH", "") or "")
        session_path = str(Path(".runtime/telegram_session"))
        phone = _telegram_auth_state["phone"]
        phone_code_hash = _telegram_auth_state["phone_code_hash"]

        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        await client.disconnect()

        _telegram_auth_state = {"step": "authenticated"}
        return {"ok": True, "message": "Telegram 인증이 완료되었습니다. 서버를 재시작하거나 폴링을 수동으로 시작하세요."}
    except DashboardOperationError as e:
        return {"ok": False, "error": str(e)}



# ----------------------------------------------------
# Scheduler Run and Status Management APIs
# ----------------------------------------------------

_dashboard_scheduler_service = DashboardSchedulerService(
    "domestic_scheduler",
    now_fn=lambda: trader.datetime.now(trader.KST).isoformat(),
)
_scheduler_running_lock = _dashboard_scheduler_service.lock
_scheduler_run_state = _dashboard_scheduler_service.state

def _bg_run_scheduled_cycle(
    mode: str,
    include_ai_rebalance: bool,
    auto_approve: bool,
    force_strategy_id: str | None = None,
    allowed_categories: set[str] | None = None,
):
    from src.scheduler import run_scheduled_cycle

    _dashboard_scheduler_service.run(
        run_scheduled_cycle,
        mode=mode,
        include_ai_rebalance=include_ai_rebalance,
        auto_approve=auto_approve,
        force_strategy_id=force_strategy_id,
        allowed_categories=allowed_categories,
    )


def _run_scheduled_cycles_for_strategies(
    mode: str,
    include_ai_rebalance: bool,
    auto_approve: bool,
    strategy_ids: list[str],
    allowed_categories: set[str] | None = None,
) -> dict:
    from src.scheduler import run_scheduled_cycle
    from src.config import config
    from src.dashboard.services.analysis_cycle_service import ISOLATED_STRATEGY_IDS

    try:
        from src.db.repository import load_ai_strategies

        registered_strategies = load_ai_strategies()
        registered_ai_ids = {
            str(item.get("id"))
            for item in registered_strategies
            if item.get("id")
        }
    except Exception:
        registered_strategies = []
        registered_ai_ids = set()
    from src.strategy_ids import resolve_ai_schedule_strategy_ids

    requested_strategy_ids = resolve_ai_schedule_strategy_ids(
        strategy_ids,
        strategies=registered_strategies,
    )

    runs = []
    errors = []
    for strategy_id in requested_strategy_ids:
        if strategy_id in ISOLATED_STRATEGY_IDS:
            try:
                result = run_scheduled_cycle(
                    mode,
                    include_ai_rebalance=False,
                    auto_approve=auto_approve,
                    force_strategy_id=strategy_id,
                    allowed_categories={"candidate"},
                )
                runs.append({
                    "strategy_id": strategy_id,
                    "cycle_id": None,
                    "result": result,
                })
            except Exception as exc:
                errors.append({
                    "strategy_id": strategy_id,
                    "message": str(exc),
                })
            continue

        from src.dashboard.services.analysis_cycle_service import start_common_analysis_cycle
        from src.db.analysis_repository import set_analysis_cycle_status

        cycle = start_common_analysis_cycle(
            strategy_id,
            trader.TRADING_ENV,
            mode=f"scheduled_{mode}",
        )
        try:
            if (
                bool(getattr(config, "autonomy_enabled", False))
                and strategy_id in registered_ai_ids
                and mode != "analysis_only"
            ):
                from src.ai_stock.automation_service import run_strategy

                result = run_strategy(
                    market="KR",
                    strategy_id=strategy_id,
                    run_type=f"dashboard_{mode}",
                )
            else:
                result = run_scheduled_cycle(
                    mode,
                    include_ai_rebalance=include_ai_rebalance,
                    auto_approve=auto_approve,
                    force_strategy_id=strategy_id,
                    allowed_categories=allowed_categories,
                )
            mark_common_analysis_stage(
                cycle["id"],
                "scheduled_run",
                details={"mode": mode},
                payload={
                    "ok": bool(result.get("ok", True)) if isinstance(result, dict) else True,
                    "status": result.get("status") if isinstance(result, dict) else "completed",
                },
            )
            set_analysis_cycle_status(cycle["id"], "completed")
            runs.append({"strategy_id": strategy_id, "cycle_id": cycle["id"], "result": result})
        except Exception as exc:
            mark_common_analysis_stage(
                cycle["id"],
                "scheduled_run",
                status="failed",
                details={"mode": mode, "error": str(exc)},
            )
            errors.append({"strategy_id": strategy_id, "message": str(exc)})
    return {
        "status": "failed" if errors and not runs else "success",
        "ok": bool(runs),
        "strategy_ids": requested_strategy_ids,
        "runs": runs,
        "errors": errors,
    }


def _bg_run_multiple_scheduled_cycles(
    mode: str,
    include_ai_rebalance: bool,
    auto_approve: bool,
    strategy_ids: list[str],
    allowed_categories: set[str] | None = None,
):
    _dashboard_scheduler_service.run(
        _run_scheduled_cycles_for_strategies,
        mode=mode,
        include_ai_rebalance=include_ai_rebalance,
        auto_approve=auto_approve,
        strategy_ids=strategy_ids,
        allowed_categories=allowed_categories,
    )
