"""
Seven Split auto-trading engine (Refactored).
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to sys.path to allow running as a script directly
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import config, get_settings, trading_flags
from src.utils.logger import logger
from src.online_access import require_online_access
from src.api.kis_api import HTTP
from src.kis_client import KISClient, KISClientConfig
from src.db.repository import init_db, connect_db, save_trade, update_trade_order_status
from src.notifier.slack import slack_session_start, slack_order, slack_candidates, slack_session_end, slack_error
from src.strategy.seven_split import (
    WATCHLIST, KOSPI_UNIVERSE, STOCK_NAMES,
    generate_signal, build_scan_universe, find_candidates, build_orders,
    generate_ai_weight_plan, generate_portfolio_optimizer_plan,
    calc_strategy_profile,
    is_excluded_symbol,
)
from src.strategy.indicators import calc_bollinger, calc_macd, calc_rsi, calc_sma
from src.strategy.risk import RiskEngine
from src.strategy.router import OrderRouter
from src.execution_plan import (
    PlanRow,
    signal_to_plan_row,
    candidate_order_to_plan_row,
    build_execution_plan,
)

KST = timezone(timedelta(hours=9))

TRADING_ENV = config.trading_env
DRY_RUN = config.dry_run
ENABLE_LIVE_TRADING = config.enable_live_trading
REQUIRE_APPROVAL = config.require_approval
ONLINE_ACCESS_BLOCKED = config.online_access_blocked

SPLIT_N = config.split_n
STOP_LOSS_PCT = config.stop_loss_pct
TAKE_PROFIT = config.take_profit
RSI_BUY = config.rsi_buy
RSI_SELL = config.rsi_sell

TOTAL_CAPITAL = config.total_capital
MAX_POSITIONS = config.max_positions
MAX_SINGLE_WEIGHT = config.max_single_weight
CASH_BUFFER = config.cash_buffer
MAX_DAILY_LOSS_PCT = config.max_daily_loss_pct
SCAN_UNIVERSE_SIZE = config.scan_universe_size

REAL_ORDERS_ENABLED = (
    not ONLINE_ACCESS_BLOCKED
    and (not DRY_RUN)
    and TRADING_ENV == "real"
    and ENABLE_LIVE_TRADING
)
ORDER_SUBMISSION_ENABLED = (
    not ONLINE_ACCESS_BLOCKED
    and (not DRY_RUN)
    and (TRADING_ENV == "demo" or REAL_ORDERS_ENABLED)
)


def runtime_flags():
    return trading_flags(get_settings())


def sync_legacy_config_aliases() -> None:
    global TRADING_ENV, DRY_RUN, ENABLE_LIVE_TRADING, REQUIRE_APPROVAL
    global ONLINE_ACCESS_BLOCKED, REAL_ORDERS_ENABLED, ORDER_SUBMISSION_ENABLED
    global SPLIT_N, STOP_LOSS_PCT, TAKE_PROFIT, RSI_BUY, RSI_SELL
    global TOTAL_CAPITAL, MAX_POSITIONS, MAX_SINGLE_WEIGHT, CASH_BUFFER
    global MAX_DAILY_LOSS_PCT, SCAN_UNIVERSE_SIZE
    global BASE_URL, KISTOCK_APP_KEY, KISTOCK_APP_SECRET, KISTOCK_ACCOUNT

    settings = get_settings()
    flags = trading_flags(settings)
    TRADING_ENV = flags.trading_env
    DRY_RUN = flags.dry_run
    ENABLE_LIVE_TRADING = flags.enable_live_trading
    REQUIRE_APPROVAL = flags.require_approval
    ONLINE_ACCESS_BLOCKED = flags.online_access_blocked
    REAL_ORDERS_ENABLED = flags.real_orders_enabled
    ORDER_SUBMISSION_ENABLED = flags.order_submission_enabled
    SPLIT_N = settings.split_n
    STOP_LOSS_PCT = settings.stop_loss_pct
    TAKE_PROFIT = settings.take_profit
    RSI_BUY = settings.rsi_buy
    RSI_SELL = settings.rsi_sell
    TOTAL_CAPITAL = settings.total_capital
    MAX_POSITIONS = settings.max_positions
    MAX_SINGLE_WEIGHT = settings.max_single_weight
    CASH_BUFFER = settings.cash_buffer
    MAX_DAILY_LOSS_PCT = settings.max_daily_loss_pct
    SCAN_UNIVERSE_SIZE = settings.scan_universe_size
    BASE_URL = (
        "https://openapi.koreainvestment.com:9443"
        if settings.trading_env == "real"
        else "https://openapivts.koreainvestment.com:29443"
    )
    KISTOCK_APP_KEY = settings.kistock_app_key
    KISTOCK_APP_SECRET = settings.kistock_app_secret
    KISTOCK_ACCOUNT = settings.kistock_account
    if "_LEGACY_SYNCED_VALUES" in globals():
        _LEGACY_SYNCED_VALUES.update({
            name: globals()[name]
            for name in _LEGACY_SYNCED_VALUES
        })

RUNTIME_DIR = Path(".runtime")
DB_PATH = Path(config.trade_db_path)

# KIS API module-level constants (patchable in tests)
BASE_URL = (
    "https://openapi.koreainvestment.com:9443"
    if config.trading_env == "real"
    else "https://openapivts.koreainvestment.com:29443"
)
KISTOCK_APP_KEY = config.kistock_app_key
KISTOCK_APP_SECRET = config.kistock_app_secret
KISTOCK_ACCOUNT = config.kistock_account
_LEGACY_SYNCED_VALUES = {
    name: globals()[name]
    for name in (
        "TRADING_ENV",
        "DRY_RUN",
        "ENABLE_LIVE_TRADING",
        "REQUIRE_APPROVAL",
        "ONLINE_ACCESS_BLOCKED",
        "REAL_ORDERS_ENABLED",
        "ORDER_SUBMISSION_ENABLED",
        "TOTAL_CAPITAL",
        "MAX_DAILY_LOSS_PCT",
        "BASE_URL",
        "KISTOCK_APP_KEY",
        "KISTOCK_APP_SECRET",
        "KISTOCK_ACCOUNT",
    )
}


def _runtime_value(alias: str, settings_value):
    legacy_value = globals()[alias]
    if legacy_value != _LEGACY_SYNCED_VALUES.get(alias):
        return legacy_value
    return settings_value


def operating_capital(account_total_eval: int | float = 0) -> int:
    """Return the configured capital available to Hanstock for this account."""
    settings = get_settings()
    configured = max(
        0,
        int(_runtime_value("TOTAL_CAPITAL", settings.total_capital) or 0),
    )
    account_total = max(0, int(account_total_eval or 0))
    if configured <= 0:
        return account_total
    if account_total <= 0:
        return configured
    return min(configured, account_total)


def available_buying_cash(
    broker_cash: int | float,
    stock_eval: int | float,
    account_total_eval: int | float,
) -> int:
    """Cap new buys by configured capital, cash buffer, and current exposure."""
    settings = get_settings()
    capital = operating_capital(account_total_eval)
    cash_buffer = float(_runtime_value("CASH_BUFFER", settings.cash_buffer) or 0)
    investable_limit = int(capital * max(0.0, 1.0 - cash_buffer))
    remaining_exposure = max(0, investable_limit - max(0, int(stock_eval or 0)))
    return min(max(0, int(broker_cash or 0)), remaining_exposure)


_KIS_ORDER_THROTTLE_LOCK = threading.Lock()
_KIS_ORDER_LAST_CALL = 0.0
_KIS_ORDER_MIN_INTERVAL_SECONDS = float(os.environ.get("KIS_ORDER_MIN_INTERVAL_SECONDS", "4.0"))


def build_kis_client_config() -> KISClientConfig:
    settings = get_settings()
    flags = trading_flags(settings)
    base_url = (
        "https://openapi.koreainvestment.com:9443"
        if flags.trading_env == "real"
        else "https://openapivts.koreainvestment.com:29443"
    )
    return KISClientConfig(
        base_url=_runtime_value("BASE_URL", base_url),
        app_key=_runtime_value("KISTOCK_APP_KEY", settings.kistock_app_key),
        app_secret=_runtime_value("KISTOCK_APP_SECRET", settings.kistock_app_secret),
        account_no=_runtime_value("KISTOCK_ACCOUNT", settings.kistock_account),
        trading_env=_runtime_value("TRADING_ENV", flags.trading_env),
        token_cache_path=Path("data") / "kis_token.json",
    )


def _kis_order_throttle() -> None:
    global _KIS_ORDER_LAST_CALL
    if _KIS_ORDER_MIN_INTERVAL_SECONDS <= 0:
        return
    with _KIS_ORDER_THROTTLE_LOCK:
        elapsed = time.monotonic() - _KIS_ORDER_LAST_CALL
        if elapsed < _KIS_ORDER_MIN_INTERVAL_SECONDS:
            time.sleep(_KIS_ORDER_MIN_INTERVAL_SECONDS - elapsed)
        _KIS_ORDER_LAST_CALL = time.monotonic()


class KIStockAPI:
    """KIS API client wired through trader module-level constants for testability."""

    TOKEN_CACHE = Path("data") / "kis_token.json"
    ETF_MARKET_CODES = {
        "102110", "133690", "148020", "152100", "157490",
        "229200", "251340", "261240", "273130", "278530",
        "305720", "381170", "448290", "481190",
    }
    _err_count: int = 0
    _circuit_opened_at: "datetime | None" = None
    MAX_ERRORS: int = 5

    def __init__(self, notify_errors: bool = True) -> None:
        require_online_access("KIS API access")
        self.notify_errors = notify_errors
        self.client_config = build_kis_client_config()
        self.base_url = getattr(self.client_config, "base_url", BASE_URL)
        self.app_key = getattr(self.client_config, "app_key", KISTOCK_APP_KEY)
        self.app_secret = getattr(self.client_config, "app_secret", KISTOCK_APP_SECRET)
        self.account_no = getattr(self.client_config, "account_no", KISTOCK_ACCOUNT)
        self.trading_env = getattr(self.client_config, "trading_env", TRADING_ENV)
        self.access_token = self._load_or_fetch_token()
        self._client = KISClient(self.client_config, session=HTTP, access_token=self.access_token)

    def _load_or_fetch_token(self) -> str:
        if self.TOKEN_CACHE.exists():
            try:
                cached = json.loads(self.TOKEN_CACHE.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(cached["expires_at"])
                if (
                    cached.get("trading_env") == self.trading_env
                    and cached.get("base_url") == self.base_url
                    and cached.get("app_key_prefix") == self.app_key[:8]
                    and expires_at > datetime.now() + timedelta(minutes=5)
                ):
                    return cached["token"]
            except Exception:
                pass
        return self._fetch_token()

    def _fetch_token(self) -> str:
        r = HTTP.post(
            f"{self.base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        token = data.get("access_token", "")
        expires_at = datetime.now() + timedelta(hours=23)
        self.TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        import hashlib
        app_key_hash = hashlib.sha256(self.app_key.encode("utf-8")).hexdigest()
        self.TOKEN_CACHE.write_text(
            json.dumps({
                "token": token,
                "expires_at": expires_at.isoformat(),
                "trading_env": self.trading_env,
                "base_url": self.base_url,
                "app_key_prefix": self.app_key[:8],
                "app_key_hash": app_key_hash,
            }),
            encoding="utf-8",
        )
        return token

    def _headers(self, tr_id: str) -> dict:
        self._client.access_token = self.access_token
        return self._client.headers(tr_id)

    def _hashkey(self, payload: dict) -> str:
        try:
            return self._client.create_hashkey(payload)
        except Exception:
            return ""

    def _fail(self) -> None:
        self.__class__._err_count = min(self.MAX_ERRORS, self.__class__._err_count + 1)

    def _success(self) -> None:
        self.__class__._err_count = 0
        self.__class__._circuit_opened_at = None

    def _record_result(self, data: dict) -> None:
        if data.get("rt_cd") == "0":
            self._success()
        else:
            self._fail()

    def _sync_circuit_to_client(self) -> None:
        self._client.circuit.error_count = self.__class__._err_count
        self._client.circuit.opened_at = self.__class__._circuit_opened_at

    def _sync_circuit_from_client(self) -> None:
        self.__class__._err_count = self._client.circuit.error_count
        self.__class__._circuit_opened_at = self._client.circuit.opened_at

    @classmethod
    def reset_circuit(cls) -> None:
        cls._err_count = 0
        cls._circuit_opened_at = None

    @classmethod
    def circuit_status(cls) -> dict:
        opened = cls._err_count >= cls.MAX_ERRORS
        opened_at = cls._circuit_opened_at.isoformat() if cls._circuit_opened_at else None
        return {
            "opened": opened,
            "error_count": cls._err_count,
            "max_errors": cls.MAX_ERRORS,
            "opened_at": opened_at,
        }

    def get_balance(self) -> dict:
        tr_id = "VTTC8434R" if self.trading_env == "demo" else "TTTC8434R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        cano = self.account_no[:8]
        acnt = self.account_no[8:] if len(self.account_no) > 8 else "01"
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt,
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
            "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        }
        _kis_order_throttle()
        try:
            r = HTTP.get(url, headers=self._headers(tr_id), params=params, timeout=15)
            if getattr(r, "status_code", 200) != 200:
                try:
                    data = r.json()
                except Exception:
                    data = {}
                msg = data.get("msg1") if isinstance(data, dict) else ""
                if not msg:
                    msg = getattr(r, "text", "")
                raise RuntimeError(f"KIS balance HTTP {r.status_code}: {msg or 'request failed'}")
            self._success()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to get KIS balance: {e}")
            self._fail()
            raise

    def get_quote(self, symbol: str) -> dict:
        self._sync_circuit_to_client()
        _kis_order_throttle()
        try:
            r = HTTP.get(
                f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers=self._headers("FHKST01010100"),
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
                timeout=10,
            )
            output = r.json().get("output", {})
            self._success()
            self._sync_circuit_from_client()
            return {
                "current": float(output.get("stck_prpr", 0)),
                "ask1": float(output.get("askp1", 0)),
                "bid1": float(output.get("bidp1", 0)),
            }
        except Exception as e:
            logger.warning(f"get_quote failed for {symbol}: {e}")
            self._fail()
            self._sync_circuit_from_client()
            return {"current": 0.0, "ask1": 0.0, "bid1": 0.0}

    def get_volume_rank(self, top_n: int = 50) -> list:
        self._sync_circuit_to_client()
        result = self._client.get_volume_rank(top_n=top_n)
        self._sync_circuit_from_client()
        return result

    def get_daily(self, symbol: str, n: int = 60) -> list:
        self._sync_circuit_to_client()
        result = self._client.get_daily(symbol, n=n)
        self._sync_circuit_from_client()
        return result

    def place_order(self, symbol: str, order_type: str, price: int, qty: int) -> dict:
        require_online_access("KIS order submission")
        flags = trading_flags(get_settings())
        # Settings is authoritative. The alias remains an explicit
        # compatibility override for callers that still patch this module.
        submission_enabled = (
            flags.order_submission_enabled or bool(ORDER_SUBMISSION_ENABLED)
        )
        if not submission_enabled:
            return {"rt_cd": "0", "msg1": "DRY_RUN"}
        is_demo = self.trading_env == "demo"
        if order_type == "buy":
            tr_id = "VTTC0802U" if is_demo else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if is_demo else "TTTC0801U"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        cano = self.account_no[:8]
        acnt = self.account_no[8:] if len(self.account_no) > 8 else "01"
        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt,
            "PDNO": symbol,
            "ORD_DVSN": "01" if price == 0 else "00",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        headers = self._headers(tr_id)
        _kis_order_throttle()
        hashkey = self._hashkey(body)
        if hashkey:
            headers["hashkey"] = hashkey
        _kis_order_throttle()
        try:
            r = HTTP.post(url, headers=headers, json=body, timeout=15)
            self._success()
            return r.json()
        except Exception as e:
            logger.error(f"place_order failed for {symbol}: {e}")
            self._fail()
            return {"rt_cd": "1", "msg1": str(e)}

    def get_trade_history(self, start_date: str, end_date: str) -> list:
        tr_id = "VTTC0081R" if self.trading_env == "demo" else "TTTC0081R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        cano = self.account_no[:8]
        acnt = self.account_no[8:] if len(self.account_no) > 8 else "01"
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt,
            "INQR_STRT_DT": start_date, "INQR_END_DT": end_date,
            "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "",
            "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": "",
            "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
            "EXCG_ID_DVSN_CD": "KRX",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        }
        rows = []
        page_params = dict(params)
        tr_cont = ""
        while True:
            headers = self._headers(tr_id)
            if tr_cont:
                headers["tr_cont"] = tr_cont
            r = HTTP.get(url, headers=headers, params=page_params, timeout=15)
            r.raise_for_status()
            data = r.json()
            rows.extend(data.get("output1", []) or [])

            next_fk = str(data.get("ctx_area_fk100") or data.get("CTX_AREA_FK100") or "").strip()
            next_nk = str(data.get("ctx_area_nk100") or data.get("CTX_AREA_NK100") or "").strip()
            response_headers = getattr(r, "headers", {}) or {}
            tr_cont = str(response_headers.get("tr_cont") or response_headers.get("tr-cont") or "").strip()
            if tr_cont not in {"M", "F"} or (not next_fk and not next_nk):
                break
            page_params["CTX_AREA_FK100"] = next_fk
            page_params["CTX_AREA_NK100"] = next_nk
            _kis_order_throttle()
        return rows

    def get_condition_search_list(self, user_id: str) -> list:
        self._sync_circuit_to_client()
        result = self._client.get_condition_search_list(user_id=user_id)
        self._sync_circuit_from_client()
        return result

    def get_condition_search_result(self, user_id: str, condition_no: str, condition_name: str) -> list:
        self._sync_circuit_to_client()
        result = self._client.get_condition_search_result(
            user_id=user_id,
            condition_no=condition_no,
            condition_name=condition_name,
        )
        self._sync_circuit_from_client()
        return result



_CANDIDATE_INDICATOR_KEYS = {"rsi", "rsi2", "sma20", "sma60", "bb_lo", "bb_hi", "macd_hist"}

_VALID_RUN_MODES = {"analysis_only", "live", None}
_ISOLATED_STRATEGY_IDS = {"plunge_bounce_strategy", "heikin_ashi_scalping_strategy"}


def normalize_run_mode(mode: str | None) -> str | None:
    if mode not in _VALID_RUN_MODES:
        raise ValueError(f"Invalid run mode: {mode!r}. Must be one of {_VALID_RUN_MODES}")
    return mode


def check_secrets():
    pass


def init_approval_db() -> None:
    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    with connect_db() as conn:
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
        except Exception:
            pass


def daily_loss_halt_triggered(pnl: int) -> bool:
    settings = get_settings()
    total_capital = _runtime_value("TOTAL_CAPITAL", settings.total_capital)
    max_daily_loss_pct = _runtime_value(
        "MAX_DAILY_LOSS_PCT",
        settings.max_daily_loss_pct,
    )
    if total_capital <= 0:
        return False
    loss_pct = abs(pnl) / total_capital * 100
    return pnl < 0 and loss_pct >= max_daily_loss_pct


def check_daily_loss(pnl: int) -> bool:
    halted = daily_loss_halt_triggered(pnl)
    if halted:
        logger.warning(f"일일 손실 한도 초과: {pnl:+,} KRW — 신규 매수 중단, 보유 포지션 방어는 계속")
    return halted


def queue_approval(
    symbol: str,
    name: str,
    action: str,
    qty: int,
    price: int,
    reason: str = "",
    source: str = "trader",
    strategy_id: str | None = None,
    strategy_version: int | None = None,
    profile_hash: str | None = None,
    source_candidate_id: int | None = None,
) -> int:
    init_approval_db()
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with connect_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO approvals
            (
                created_at, updated_at, symbol, name, action, qty, price, reason, source,
                status, response_msg, strategy_id, strategy_version, profile_hash, source_candidate_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?, ?, ?)
            """,
            (
                now, now, symbol, name, action, qty, price, reason, source,
                strategy_id, strategy_version, profile_hash, source_candidate_id,
            ),
        )
        return cursor.lastrowid


def _is_executable_plan_row(row: dict) -> bool:
    return row.get("action") in {"buy", "sell"} and int(row.get("qty", 0) or 0) > 0


def execute_plan_row(api, context: dict, row: dict) -> dict:
    if not _is_executable_plan_row(row):
        return {**row, "decision": "skip", "ok": True}

    mode = context.get("mode")
    if mode == "analysis_only":
        approval_id = queue_approval(
            row["symbol"],
            row["name"],
            row["action"],
            row["qty"],
            row["price"],
            row.get("reason", ""),
            source="trader",
        )
        return {**row, "decision": "queue", "ok": True, "approval_id": approval_id}

    router = context.get("router")
    if router is None:
        return {**row, "decision": "skip", "ok": False}

    result = router.route(
        row["symbol"],
        row["name"],
        row["action"],
        row["qty"],
        row["price"],
        row.get("reason", ""),
        row.get("indicators", {}),
        strategy_id=row.get("strategy_id") or row.get("source"),
    )
    ok = result.get("ok", False)
    if result.get("status") == "pending" or "approval_id" in result:
        decision = "queue" if ok else "failed"
    else:
        decision = "execute" if ok else "failed"
    ret = {**row, "decision": decision, "ok": ok}
    if "approval_id" in result:
        ret["approval_id"] = result["approval_id"]
    return ret


def _holding_history_from_balance(api, stocks: list[dict]) -> list[dict]:
    holdings = []
    for stock in stocks:
        qty = int(stock.get("hldg_qty", 0) or 0)
        price = int(stock.get("prpr", 0) or 0)
        value = int(stock.get("evlu_amt", 0) or 0)
        if price <= 0 and qty > 0:
            price = round(value / qty)
        symbol = stock.get("pdno", "")
        daily = api.get_daily(symbol, n=120)
        prices = [float(row["stck_clpr"]) for row in daily if row.get("stck_clpr")]
        highs = [float(row["stck_hgpr"]) for row in daily if row.get("stck_hgpr")]
        volumes = [float(row["acml_vol"]) for row in daily if row.get("acml_vol")]
        prices.reverse()
        highs.reverse()
        volumes.reverse()
        holdings.append({
            "symbol": symbol,
            "name": stock.get("prdt_name", symbol),
            "qty": qty,
            "price": price,
            "value": value if value > 0 else qty * price,
            "prices": prices,
            "highs": highs,
            "volumes": volumes,
        })
    return holdings


def build_ai_rebalance_rows(api, balance_data: dict, total_eval: int) -> list[dict]:
    stocks = balance_data.get("output1", [])
    holdings = _holding_history_from_balance(api, stocks)
    ai_plan = generate_ai_weight_plan(holdings, total_eval)
    rows = []
    for position in ai_plan.get("positions", []):
        if is_excluded_symbol(position.get("symbol", "")):
            logger.info(f"[EXCLUDE] Skipping AI rebalance for excluded symbol {position.get('symbol', '')}")
            continue
        action = position.get("rebalance_action", "hold")
        qty = int(position.get("rebalance_qty", 0) or 0)
        if action not in {"buy", "sell"} or qty <= 0:
            continue
        target_weight = float(position.get("target_weight", 0) or 0)
        current_weight = float(position.get("current_weight", 0) or 0)
        reason = (
            f"AI rebalance {current_weight * 100:.1f}% -> {target_weight * 100:.1f}%"
        )
        if position.get("reasoning_kr"):
            reason = f"{reason} | {position['reasoning_kr']}"
        rows.append(PlanRow(
            symbol=str(position.get("symbol", "")),
            name=str(position.get("name") or position.get("symbol", "")),
            action=action,
            qty=qty,
            price=int(position.get("price", 0) or 0),
            reason=reason,
            source="ai_rebalance",
            category="ai_rebalance",
            score=position.get("score"),
            reasons=list(position.get("reasons") or []),
            metadata={
                "target_weight": target_weight,
                "current_weight": current_weight,
                "target_value": position.get("target_value", 0),
                "delta_value": position.get("delta_value", 0),
                "ai_active": bool(ai_plan.get("ai_active")),
            },
        ).to_dict())
    return rows


def build_runtime_plan(
    api,
    balance_data: dict,
    *,
    include_ai_rebalance: bool = False,
    read_cached_candidates: bool = False,
    force_strategy_id: str | None = None,
) -> dict:
    active_strategy_id = force_strategy_id
    active_strategy = None
    try:
        from src.db.repository import load_ai_strategies
        strategies = load_ai_strategies()
        if active_strategy_id:
            active_strategy = next((s for s in strategies if s.get("id") == active_strategy_id or s.get("model") == active_strategy_id), None)
        else:
            active_strategy = next((s for s in strategies if s.get("selected")), None)
            if active_strategy:
                active_strategy_id = active_strategy.get("id") or "seven_split"
            else:
                active_strategy_id = "seven_split"
    except Exception:
        if not active_strategy_id:
            active_strategy_id = "seven_split"

    stocks = balance_data.get("output1", [])
    summary = (balance_data.get("output2") or [{}])[0]
    cash = int(summary.get("prvs_rcdl_excc_amt", 0) or 0)
    if cash == 0:
        cash = int(summary.get("dnca_tot_amt", 0) or 0)
    if cash == 0:
        summary_total = int(summary.get("tot_evlu_amt", 0) or 0)
        summary_stock_eval = int(summary.get("scts_evlu_amt", 0) or 0)
        if summary_total > 0:
            cash = summary_total - summary_stock_eval
    total_eval = int(summary.get("tot_evlu_amt", 0) or 0)
    stock_eval = int(summary.get("scts_evlu_amt", 0) or 0)
    if stock_eval <= 0:
        stock_eval = sum(
            int(stock.get("evlu_amt", 0) or 0)
            for stock in stocks
        )
    capital = operating_capital(total_eval)
    buying_cash = available_buying_cash(cash, stock_eval, total_eval)
    pnl = int(summary.get("evlu_pfls_smtl_amt", 0) or 0)
    isolated_strategy_run = active_strategy_id in _ISOLATED_STRATEGY_IDS

    position_rows = []
    if not isolated_strategy_run:
        for stock in stocks:
            sym = stock.get("pdno", "")
            if is_excluded_symbol(sym):
                logger.info(f"[EXCLUDE] Skipping holding signal for excluded symbol {sym}")
                continue
            name = stock.get("prdt_name", sym)
            rt = float(stock.get("evlu_pfls_rt", 0) or 0)
            daily = api.get_daily(sym, n=60)
            strategy_model = ""
            if active_strategy:
                strategy_model = str(active_strategy.get("model") or "")
                if strategy_model == "none":
                    strategy_model = ""
            signal = generate_signal(stock, daily, strategy_model=strategy_model)
            row = signal_to_plan_row(
                sym,
                name,
                signal,
                source="holding_signal",
                include_hold=True,
                metadata={"return_pct": rt},
                strategy_id=active_strategy_id,
            )
            if row is not None:
                position_rows.append(row)

    halted = daily_loss_halt_triggered(pnl)
    remaining_cash = buying_cash
    candidate_rows = []
    candidate_scan: dict = {"candidates": [], "scan_summary": [], "scanned": 0, "min_score": 2, "scan_error": None}

    if not halted:
        held_symbols = {s.get("pdno", "") for s in stocks}
        
        if read_cached_candidates:
            from src.db.repository import get_latest_scanned_candidates
            db_candidates = get_latest_scanned_candidates(active_strategy_id)
            candidates = []
            for row in db_candidates:
                candidates.append({
                    "ticker": row["symbol"],
                    "name": row["name"],
                    "current_price": row["price"],
                    "score": row["score"],
                    "rule_score": row["rule_score"] if row["rule_score"] is not None else row["score"],
                    "ml_score": row["ml_score"],
                    "final_score": row["final_score"] if row["final_score"] is not None else row["score"],
                    "reasons": row["reasons"].split(",") if row["reasons"] else [],
                    "rsi": row["rsi"],
                    "rsi2": row["rsi2"],
                    "macd_hist": row["macd_hist"],
                    "sma20": row["sma20"],
                    "sma60": row["sma60"],
                    "bb_lo": row.get("bb_lo") or 0.0,
                    "bb_hi": row.get("bb_hi") or 0.0,
                })
            candidates = sorted(candidates, key=lambda c: (-float(c["final_score"]), c["ticker"]))
        else:
            # Isolated strategies must use their own universe only. If the
            # universe is empty, do not fall back to the shared Hanstock list.
            strategy_universe_missing = False
            universe = []
            if active_strategy_id:
                try:
                    from src.db.repository import load_strategy_universe_symbols
                    dedicated = load_strategy_universe_symbols(active_strategy_id)
                    if dedicated:
                        universe = [code for code in dedicated if code not in held_symbols]
                    elif active_strategy_id in _ISOLATED_STRATEGY_IDS:
                        universe = []
                        strategy_universe_missing = True
                except Exception:
                    if active_strategy_id in _ISOLATED_STRATEGY_IDS:
                        universe = []
                        strategy_universe_missing = True
            if not universe and not strategy_universe_missing and active_strategy_id not in _ISOLATED_STRATEGY_IDS:
                universe = build_scan_universe(api, held_symbols)
            if strategy_universe_missing:
                scan_result = {
                    "candidates": [],
                    "scan_summary": [],
                    "scanned": 0,
                    "min_score": 1.0 if active_strategy_id == "plunge_bounce_strategy" else 2,
                    "scan_error": (
                        f"{active_strategy_id} has no dedicated universe. "
                        "Register strategy-specific watchlist symbols first."
                    ),
                }
                candidates = []
            elif active_strategy_id == "plunge_bounce_strategy":
                scan_result = find_candidates(
                    held_symbols,
                    universe=universe,
                    min_score=1.0,
                    ranker="rule_only",
                    api=api,
                    strategy_model="plunge_bounce_strategy",
                )
            else:
                ranker = "gpt_5_mini"
                strategy_model = ""
                strategy_profile = None
                strategy_description = ""
                if active_strategy:
                    model = active_strategy.get("model") or "none"
                    provider = active_strategy.get("provider") or "none"
                    profile = active_strategy.get("profile") or {}
                    weight = float(profile.get("ai_weight", active_strategy.get("weight", 0.0)) or 0.0)

                    strategy_model = model
                    strategy_profile = profile
                    strategy_description = active_strategy.get("description") or ""
                    if provider == "none" or model == "none" or weight == 0.0:
                        ranker = "rule_only"
                    else:
                        ranker = model
                scan_result = find_candidates(
                    held_symbols,
                    universe=universe,
                    ranker=ranker,
                    strategy_model=strategy_model,
                    strategy_profile=strategy_profile,
                    strategy_description=strategy_description,
                    api=api,
                )
            candidates = scan_result.get("candidates", [])

        orders = build_orders(candidates, api.get_quote, len(held_symbols), buying_cash)
        order_by_ticker = {o["ticker"]: o for o in orders}

        for candidate in candidates:
            order = order_by_ticker.get(candidate["ticker"], {})
            row = candidate_order_to_plan_row(candidate, order, source="candidate_order", strategy_id=active_strategy_id)
            indicators = {
                k: v
                for k, v in candidate.items()
                if k in _CANDIDATE_INDICATOR_KEYS and v is not None
            }
            row = {**row, "indicators": indicators}
            candidate_rows.append(row)
            
            # Automatically save scan results to DB for history tracking in automated cycles
            if not read_cached_candidates:
                from src.db.repository import save_scanned_candidate
                save_scanned_candidate(
                    symbol=candidate.get("ticker", candidate.get("symbol", "")),
                    name=candidate.get("name", candidate.get("ticker", "")),
                    score=candidate.get("score", 0),
                    reasons=candidate.get("reasons", []),
                    price=candidate.get("current_price", candidate.get("price", 0)),
                    env=get_settings().trading_env,
                    indicators=indicators
                )
            if order:
                remaining_cash -= int(order.get("estimated_cost", 0) or 0)

        if read_cached_candidates:
            candidate_scan = {
                "candidates": candidates,
                "scan_summary": candidates,
                "scanned": len(candidates),
                "min_score": 2,
                "scan_error": None if candidates else "No cached candidates found in database",
            }
        else:
            candidate_scan = {
                "candidates": candidates,
                "scan_summary": scan_result.get("scan_summary", []),
                "scanned": scan_result.get("scanned", 0),
                "min_score": scan_result.get("min_score", 2),
                "scan_error": scan_result.get("scan_error"),
            }

            # AI 자동 추가적용 로직 (스케줄러 주기적 관리 지원)
            if not halted:
                from src.db.repository import load_watchlist_data, save_watchlist_data
                from src.strategy.seven_split import sync_watchlist_runtime
                try:
                    watchlist_data = load_watchlist_data()
                    if watchlist_data.get("ai_auto_add", False):
                        threshold = float(watchlist_data.get("ai_auto_add_threshold", 3.0))
                        symbols = list(watchlist_data.get("symbols", []))
                        symbol_set = set(symbols)
                        
                        score_by_symbol = {}
                        name_by_symbol = {}
                        for row in scan_result.get("scan_summary", []) or []:
                            sym = row.get("ticker") or row.get("symbol")
                            if sym:
                                score_by_symbol[str(sym)] = float(row.get("score", 0.0) or 0.0)
                                if row.get("name"):
                                    name_by_symbol[str(sym)] = row["name"]
                                    
                        changed = False
                        for cand in candidates:
                            score = float(cand.get("score", 0.0) or 0.0)
                            if score >= threshold:
                                sym = str(cand["ticker"])
                                name_by_symbol.setdefault(sym, cand.get("name") or sym)
                                if sym not in symbol_set:
                                    symbols.append(sym)
                                    symbol_set.add(sym)
                                    changed = True
                                    logger.info(f"[WATCHLIST AUTO-ADD] Added {sym} (score={score})")
                                    
                        kept_symbols = []
                        for sym in symbols:
                            if sym in score_by_symbol and score_by_symbol[sym] < threshold:
                                changed = True
                                logger.info(f"[WATCHLIST AUTO-REMOVE] Removed {sym} (score={score_by_symbol[sym]})")
                                continue
                            kept_symbols.append(sym)
                            
                        if changed:
                            watchlist_data["symbols"] = kept_symbols
                            save_watchlist_data(watchlist_data)
                            sync_watchlist_runtime()
                except Exception as w_err:
                    logger.warning(f"Failed to auto-add high score candidate to watchlist in cycle: {w_err}")

    plan = build_execution_plan(position_rows=position_rows, candidate_rows=candidate_rows)
    ai_rebalance_rows = []
    if include_ai_rebalance and not halted:
        ai_rebalance_rows = build_ai_rebalance_rows(api, balance_data, capital)
        plan.extend(ai_rebalance_rows)

    return {
        "plan": plan,
        "position_plan_rows": position_rows,
        "candidate_plan_rows": candidate_rows,
        "ai_rebalance_rows": ai_rebalance_rows,
        "remaining_cash": remaining_cash,
        "daily_loss_halt": halted,
        "candidate_scan": candidate_scan,
        "cash": cash,
        "buying_cash": buying_cash,
        "operating_capital": capital,
        "held_symbols": {s.get("pdno", "") for s in stocks},
    }


def run(
    mode: str | None = None,
    *,
    include_ai_rebalance: bool = False,
    execution_categories: set[str] | None = None,
    force_strategy_id: str | None = None,
) -> dict:
    settings = get_settings()
    flags = trading_flags(settings)
    check_secrets()
    init_db()
    init_approval_db()

    api = KIStockAPI()
    balance = api.get_balance()

    stocks = balance.get("output1", [])
    summary = (balance.get("output2") or [{}])[0]
    cash = int(summary.get("prvs_rcdl_excc_amt", 0) or 0)
    if cash == 0:
        cash = int(summary.get("dnca_tot_amt", 0) or 0)
    if cash == 0:
        summary_total = int(summary.get("tot_evlu_amt", 0) or 0)
        summary_stock_eval = int(summary.get("scts_evlu_amt", 0) or 0)
        if summary_total > 0:
            cash = summary_total - summary_stock_eval
    total_eval = int(summary.get("tot_evlu_amt", 0) or 0)
    pnl = int(summary.get("evlu_pfls_smtl_amt", 0) or 0)

    logger.info("=" * 60)
    logger.info(
        "Seven Split started | "
        f"DRY_RUN={flags.dry_run} | "
        f"ENABLE_LIVE_TRADING={flags.enable_live_trading} | "
        f"ENV={flags.trading_env}"
    )
    logger.info(
        f"Order submission enabled: {flags.order_submission_enabled} | "
        f"Real orders enabled: {flags.real_orders_enabled}"
    )
    logger.info(f"Cash={cash:,} KRW | Total={total_eval:,} KRW | PnL={pnl:+,} KRW | Holdings={len(stocks)}")

    slack_session_start(
        cash=cash,
        total=total_eval,
        stock_count=len(stocks),
        order_submission_enabled=flags.order_submission_enabled,
        real_orders_enabled=flags.real_orders_enabled,
    )

    daily_loss_halted = check_daily_loss(pnl)

    bp_kwargs = {}
    if include_ai_rebalance:
        bp_kwargs["include_ai_rebalance"] = True
    if force_strategy_id is not None:
        bp_kwargs["force_strategy_id"] = force_strategy_id

    runtime_bundle = build_runtime_plan(api, balance, **bp_kwargs)
    daily_loss_halted = daily_loss_halted or bool(runtime_bundle.get("daily_loss_halt"))

    candidates = runtime_bundle.get("candidate_scan", {}).get("candidates", [])
    if candidates:
        slack_candidates(candidates)

    context: dict = {"mode": mode}
    if mode != "analysis_only":
        context["router"] = OrderRouter(api)

    results = []
    for row in runtime_bundle["plan"]:
        if execution_categories is not None and row.get("category") not in execution_categories:
            results.append({**row, "decision": "skip", "ok": True, "skip_reason": "category filtered"})
            continue
        if daily_loss_halted and row.get("action") == "buy":
            results.append({
                **row,
                "decision": "skip",
                "ok": True,
                "skip_reason": "daily loss halt blocks buy orders only",
            })
            continue
        result_row = execute_plan_row(api, context, row)
        results.append(result_row)

    remaining_cash = runtime_bundle.get("remaining_cash", cash)
    slack_session_end(results=results, cash=remaining_cash, total=total_eval, pnl=pnl)

    logger.info("Seven Split finished")
    return {
        "plan": runtime_bundle["plan"],
        "results": results,
        **{k: v for k, v in runtime_bundle.items() if k != "plan"},
    }


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        logger.exception("Critical error in trader:")
        if hasattr(e, "last_attempt") and e.last_attempt.exception():
            original_err = e.last_attempt.exception()
            slack_error(f"실행 중 치명적인 오류가 발생했습니다: {original_err}")
        else:
            slack_error(f"실행 중 치명적인 오류가 발생했습니다: {e}")
        raise
