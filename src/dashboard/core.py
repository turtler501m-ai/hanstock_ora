import json
import hashlib
import concurrent.futures
import os
import re
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
from src.dashboard.services.api_audit_service import (  # noqa: E402
    ApiAuditMiddleware,
)
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
    account_format_warning,
    env_bool_value,
    env_value_without_inline_comment,
    expand_virtual_env_updates,
    mask_env_value,
    read_env_values,
    serialize_env_value,
    validate_env_value,
    virtual_env_value,
    write_env_values,
)
from src.dashboard.services.response_service import (  # noqa: E402
    SafeJSONResponse,
    json_safe_value as _json_safe_value,
)
from src.dashboard.services.runtime_status_service import dashboard_runtime_info  # noqa: E402
from src.strategy.seven_split import adjust_tick_size  # noqa: E402
from src.utils.logger import logger  # noqa: E402


app = FastAPI(
    title="Seven Split Dashboard",
    version="1.0.0",
    default_response_class=SafeJSONResponse,
)
app.add_middleware(ApiAuditMiddleware)
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
from src.dashboard.settings_schema import (
    AI_ENV_BINDINGS,
    ENV_FIELD_MAP,
    ENV_FIELDS,
    KIS_ENV_BINDINGS,
    STRATEGY_ENV_BINDINGS,
)
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
    return account_format_warning(account)


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
    source = f"{trader.runtime_flags().trading_env}:{trader.config.kistock_account}"
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
    trading_env_fn=lambda: trader.runtime_flags().trading_env,
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
    from src.dashboard.services.cache_service import snapshot_read_through as read
    return read(
        kind,
        builder,
        ttl=DASHBOARD_SNAPSHOT_TTL_SECONDS if ttl is None else ttl,
        env=env or trader.runtime_flags().trading_env,
        account_key=_account_cache_key() if account_scoped else "_global_",
        now_fn=lambda: trader.datetime.now(trader.KST),
        recoverable_errors=DashboardOperationError,
    )

def invalidate_snapshot(kind: str, *, account_scoped: bool = True, env: str | None = None) -> None:
    """주문/승인 등 상태 변경 후 해당 탭 스냅샷을 지워 다음 read에서 즉시 재생성되게 한다."""
    try:
        from src.db.repository import delete_account_snapshot

        env = env or trader.runtime_flags().trading_env
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


def _candidate_cache_service_call(name: str, *args, **kwargs):
    from src.dashboard.services import cache_service
    cache_service._refresh_candidate_dependencies()
    return getattr(cache_service, name)(*args, **kwargs)

def _candidate_strategy_cache_signature(ranker: str): return _candidate_cache_service_call("_candidate_strategy_cache_signature", ranker)
def _get_candidate_cache_path(ranker: str, optimizer: str): return _candidate_cache_service_call("_get_candidate_cache_path", ranker, optimizer)
def _load_candidate_cache(min_score: int, ranker="gpt_5_mini", optimizer="score_tilted_inverse_vol", allow_stale=False): return _candidate_cache_service_call("_load_candidate_cache", min_score, ranker, optimizer, allow_stale)
def _candidate_snapshot_kind(min_score: int, ranker: str, optimizer: str): return _candidate_cache_service_call("_candidate_snapshot_kind", min_score, ranker, optimizer)
def _candidate_envelope_to_result(cached, min_score: int, ranker: str, optimizer: str, *, allow_stale=False): return _candidate_cache_service_call("_candidate_envelope_to_result", cached, min_score, ranker, optimizer, allow_stale=allow_stale)
def _save_candidate_cache(min_score: int, rows, scan_summary, scanned: int, ranker="gpt_5_mini", optimizer="score_tilted_inverse_vol"): return _candidate_cache_service_call("_save_candidate_cache", min_score, rows, scan_summary, scanned, ranker, optimizer)


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
    available_slots = max(0, trader.get_settings().max_positions - held_count)
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
    return mask_env_value(value)


def _validate_env_value(key: str, value: object) -> str:
    return validate_env_value(ENV_FIELD_MAP, key, value)


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


def _quantconnect_service_call(name: str, *args, **kwargs):
    from src.dashboard.services import quantconnect_service
    quantconnect_service._refresh_dependencies()
    return getattr(quantconnect_service, name)(*args, **kwargs)

def _quantconnect_auth_status(credentials): return _quantconnect_service_call("_quantconnect_auth_status", credentials)
def _first_item(value): return _quantconnect_service_call("_first_item", value)
def _quantconnect_errors(*payloads): return _quantconnect_service_call("_quantconnect_errors", *payloads)
def _quantconnect_order_rows(payload): return _quantconnect_service_call("_quantconnect_order_rows", payload)
def _quantconnect_portfolio_state(payload): return _quantconnect_service_call("_quantconnect_portfolio_state", payload)
def _quantconnect_cloud_snapshot(credentials, *, force_refresh=False): return _quantconnect_service_call("_quantconnect_cloud_snapshot", credentials, force_refresh=force_refresh)
def _clear_quantconnect_cloud_cache(): return _quantconnect_service_call("_clear_quantconnect_cloud_cache")
def _quantconnect_live_nodes(payload): return _quantconnect_service_call("_quantconnect_live_nodes", payload)
def _select_quantconnect_live_node(payload, requested_node_id=""): return _quantconnect_service_call("_select_quantconnect_live_node", payload, requested_node_id)
def _wait_for_quantconnect_compile(api, project_id, compile_payload, **kwargs): return _quantconnect_service_call("_wait_for_quantconnect_compile", api, project_id, compile_payload, **kwargs)
def _quantconnect_mnq_status(): return _quantconnect_service_call("_quantconnect_mnq_status")
def _quantconnect_credentials(): return _quantconnect_service_call("_quantconnect_credentials")
def _quantconnect_mnq_deploy(payload=None): return _quantconnect_service_call("_quantconnect_mnq_deploy", payload)
def _quantconnect_mnq_order(payload): return _quantconnect_service_call("_quantconnect_mnq_order", payload)


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
            "ok": trader.runtime_flags().trading_env == "demo",
            "message": f"TRADING_ENV={trader.runtime_flags().trading_env}",
            "critical": True,
        },
        {
            "key": "dry_run_disabled",
            "ok": trader.runtime_flags().dry_run is False,
            "message": f"DRY_RUN={str(trader.runtime_flags().dry_run).lower()}",
            "critical": True,
        },
        {
            "key": "live_trading_disabled",
            "ok": trader.runtime_flags().enable_live_trading is False and trader.runtime_flags().real_orders_enabled is False,
            "message": f"ENABLE_LIVE_TRADING={str(trader.runtime_flags().enable_live_trading).lower()}, real_orders={str(trader.runtime_flags().real_orders_enabled).lower()}",
            "critical": True,
        },
        {
            "key": "demo_order_submission",
            "ok": trader.runtime_flags().order_submission_enabled is True,
            "message": f"ORDER_SUBMISSION_ENABLED={str(trader.runtime_flags().order_submission_enabled).lower()}",
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
            "ok": trader.runtime_flags().require_approval or _auto_approval_enabled(),
            "message": f"REQUIRE_APPROVAL={str(trader.runtime_flags().require_approval).lower()}, auto_approval={str(_auto_approval_enabled()).lower()}",
            "critical": False,
        },
    ]
    critical_ready = all(item["ok"] for item in checks if item["critical"])
    return {
        "ready": critical_ready,
        "mode": "kis_demo_auto",
        "trading_env": trader.runtime_flags().trading_env,
        "dry_run": trader.runtime_flags().dry_run,
        "enable_live_trading": trader.runtime_flags().enable_live_trading,
        "order_submission_enabled": trader.runtime_flags().order_submission_enabled,
        "real_orders_enabled": trader.runtime_flags().real_orders_enabled,
        "checks": checks,
    }


def _runtime_dashboard_info() -> dict:
    return dashboard_runtime_info()



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
        trader.runtime_flags().trading_env,
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
                    env=trader.runtime_flags().trading_env,
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



def _approval_service_call(name: str, *args, **kwargs):
    from src.dashboard.services import approval_service
    approval_service._refresh_dependencies()
    return getattr(approval_service, name)(*args, **kwargs)

def _load_pending_approval(approval_id: int) -> dict:
    return _approval_service_call("_load_pending_approval", approval_id)
def _claim_pending_approval(approval_id: int) -> dict:
    return _approval_service_call("_claim_pending_approval", approval_id)
def _approval_response_msg(result: dict, *, ok: bool) -> str:
    return _approval_service_call("_approval_response_msg", result, ok=ok)
def _current_holding_qty_from_balance(api, symbol: str) -> int:
    return _approval_service_call("_current_holding_qty_from_balance", api, symbol)
def _pending_approval_ids(limit: int = 200, *, exclude_sources: set[str] | None = None) -> list[int]:
    return _approval_service_call("_pending_approval_ids", limit, exclude_sources=exclude_sources)
def _is_approval_already_claimed(exc: Exception) -> bool:
    return _approval_service_call("_is_approval_already_claimed", exc)
def _auto_approve_pending_approvals(limit: int = 200) -> list[dict]:
    return _approval_service_call("_auto_approve_pending_approvals", limit)
def _approve_pending_approval(approval_id: int, approval_label: str = "수동승인") -> dict:
    with _approval_submission_lock:
        return _approve_pending_approval_serialized(approval_id, approval_label)
def _approve_pending_approval_serialized(approval_id: int, approval_label: str, *, approval: dict | None = None) -> dict:
    return _approval_service_call("_approve_pending_approval_serialized", approval_id, approval_label, approval=approval)

for _approval_wrapper_name in (
    "_load_pending_approval",
    "_claim_pending_approval",
    "_approval_response_msg",
    "_current_holding_qty_from_balance",
    "_pending_approval_ids",
    "_is_approval_already_claimed",
):
    getattr(sys.modules[__name__], _approval_wrapper_name)._approval_service_wrapper = True

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
        strategy_id = _resolved_trade_strategy_id(t)
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
            "strategy_id": strategy_id,
            "strategy_version": t.get("strategy_version"),
            "profile_hash": t.get("profile_hash") or "",
            "source_approval_id": t.get("source_approval_id"),
            "account_key": t.get("account_key") or "",
            "fee": t.get("fee"),
            "tax": t.get("tax"),
            "cost_source": t.get("cost_source") or "unavailable",
        }
    return sorted(merged_trades.values(), key=lambda x: x["ts"])


def _resolved_trade_strategy_id(trade: dict) -> str:
    """Recover attribution only when the recorded execution source is unambiguous."""
    strategy_id = str(trade.get("strategy_id") or "").strip()
    if strategy_id:
        return strategy_id
    if str(trade.get("reason") or "").strip().startswith("AI rebalance "):
        from src.strategy_ids import AI_REBALANCE_STRATEGY_ID

        return AI_REBALANCE_STRATEGY_ID
    return ""


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
    show_dry_run = trader.runtime_flags().dry_run or (trader.runtime_flags().trading_env == "demo")
    
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
        return "수동/출처 미확인"
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
        "ai_rebalance": "AI 자산배분 리밸런싱",
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
_KIS_INDEX_CODES = {"KOSPI": "0001", "KOSDAQ": "1001"}


def _safe_index_rows(rows: list[dict]) -> list[dict]:
    """Normalize benchmark observations without breaking the trading-day chain."""
    result: list[dict] = []
    for row in sorted(rows, key=lambda item: str(item.get("date") or "")):
        try:
            close = float(row.get("close") or 0)
        except (TypeError, ValueError):
            continue
        date = str(row.get("date") or "")[:10]
        if len(date) != 10 or close <= 0:
            continue
        result.append({"date": date, "close": close})
    return result


def _load_index_rows() -> dict[str, list[dict]]:
    """Refresh benchmark closes from KIS, then use local DB and guarded Yahoo fallback."""
    global _INDEX_ROWS_CACHE
    cached_at, cached_rows = _INDEX_ROWS_CACHE
    if time.monotonic() - cached_at < 300:
        return cached_rows
    series: dict[str, list[dict]] = {}
    try:
        from src.db.repository import save_daily_charts

        api = _get_api()
        for name, code in _KIS_INDEX_CODES.items():
            rows = api.get_index_daily(code, n=120)
            if rows:
                save_daily_charts(code, rows)
                normalized = _safe_index_rows(rows)
                if normalized:
                    series[name] = normalized
    except Exception as exc:
        logger.info(f"KIS performance benchmark refresh unavailable: {exc}")

    try:
        from src.db.repository import connect_db

        with connect_db() as conn:
            conn.row_factory = sqlite3.Row
            for name, symbols in _INDEX_SYMBOL_ALIASES.items():
                if name in series:
                    continue
                for symbol in symbols:
                    rows = conn.execute(
                        "SELECT date, close FROM daily_charts WHERE symbol=? AND close>0 "
                        "ORDER BY date DESC LIMIT 90",
                        (symbol,),
                    ).fetchall()
                    if rows:
                        normalized = _safe_index_rows([
                            {"date": str(row["date"])[:10], "close": float(row["close"])}
                            for row in reversed(rows)
                        ])
                        if normalized:
                            series[name] = normalized
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
                normalized = _safe_index_rows([
                    {"date": str(index)[:10], "close": float(value)}
                    for index, value in close.dropna().items()
                ])
                if normalized:
                    series[name] = normalized
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


def _daily_holding_change_context(
    trades: list[dict], dates: set[str]
) -> dict[str, dict]:
    """Return prior-close weighted moves for positions held at each session open."""
    if not dates:
        return {}
    valid_trades = []
    symbols: set[str] = set()
    for trade in _account_trades(trades):
        day = str(trade.get("ts") or "")[:10]
        symbol = str(trade.get("symbol") or "").strip()
        action = str(trade.get("action") or "").lower()
        qty = _to_int(trade.get("qty"))
        if len(day) != 10 or not symbol or action not in {"buy", "sell"} or qty <= 0:
            continue
        valid_trades.append((day, symbol, action, qty))
        symbols.add(symbol)
    valid_trades.sort(key=lambda item: item[0])

    price_rows = _load_symbol_price_rows(symbols)
    prices_by_symbol = {
        symbol: {
            str(row.get("date") or "")[:10]: float(row.get("close") or 0)
            for row in rows
            if float(row.get("close") or 0) > 0
        }
        for symbol, rows in price_rows.items()
    }
    ordered_price_dates = {
        symbol: sorted(prices)
        for symbol, prices in prices_by_symbol.items()
    }

    positions: dict[str, int] = {}
    trade_index = 0
    context: dict[str, dict] = {}
    for day in sorted(dates):
        while trade_index < len(valid_trades) and valid_trades[trade_index][0] < day:
            _trade_day, symbol, action, qty = valid_trades[trade_index]
            if action == "buy":
                positions[symbol] = positions.get(symbol, 0) + qty
            else:
                positions[symbol] = max(0, positions.get(symbol, 0) - qty)
            trade_index += 1

        previous_value = 0.0
        change_value = 0.0
        included = 0
        missing = 0
        for symbol, qty in positions.items():
            if qty <= 0:
                continue
            prices = prices_by_symbol.get(symbol, {})
            current = prices.get(day)
            prior_dates = [price_day for price_day in ordered_price_dates.get(symbol, []) if price_day < day]
            previous = prices.get(prior_dates[-1]) if prior_dates else None
            if not current or not previous:
                missing += 1
                continue
            previous_value += qty * previous
            change_value += qty * (current - previous)
            included += 1
        context[day] = {
            "holding_change_pct": (
                round(change_value / previous_value * 100, 2)
                if previous_value > 0 else None
            ),
            "holding_change_symbol_count": included,
            "holding_change_missing_count": missing,
        }
    return context


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
        if str(trade.get("env") or trader.runtime_flags().trading_env) == str(trader.runtime_flags().trading_env)
    ]
    if strategy_id:
        account_trades = [
            trade for trade in account_trades
            if str(trade.get("strategy_id") or "unattributed") == strategy_id
        ]
    else:
        account_trades = [
            trade for trade in account_trades
            if str(trade.get("strategy_id") or "unattributed") != "unattributed"
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
        row["strategy_name"] = _strategy_label(row["strategy_id"])
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
    holding_change_context = _daily_holding_change_context(trades, set(daily))
    daily_rows = [
        {
            "period": key,
            **value,
            **holding_change_context.get(key, {}),
            **market_context.get(key, {}),
        }
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




def _sync_filled_trades_from_history(api, *, days: int = 90, history: list[dict] | None = None) -> dict:
    from src.dashboard.services.order_sync_service import _sync_filled_trades_from_history as sync
    return sync(api, days=days, history=history)


def _order_history_window(days: int = MIN_ORDER_HISTORY_SYNC_DAYS) -> tuple[str, str]:
    from src.dashboard.services.order_sync_service import _order_history_window as window
    return window(days)


def _load_trackable_order_trades(days: int = MIN_ORDER_HISTORY_SYNC_DAYS) -> list[dict]:
    from src.dashboard.services.order_sync_service import _load_trackable_order_trades as load
    return load(days)


def _sync_order_status_from_history(
    api, *, days: int = MIN_ORDER_HISTORY_SYNC_DAYS, history: list[dict] | None = None
) -> dict:
    from src.dashboard.services.order_sync_service import _sync_order_status_from_history as sync
    return sync(api, days=days, history=history)


def _sync_order_status_from_balance(
    api, tracked: list[dict], *, reason: str = "", close_unreserved_sells: bool = False
) -> dict:
    from src.dashboard.services.order_sync_service import _sync_order_status_from_balance as sync
    return sync(api, tracked, reason=reason, close_unreserved_sells=close_unreserved_sells)

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


def _persist_strategy_lookup_candidate_snapshot(
    strategy_id: str,
    result: dict,
    registered_strategies: list[dict],
    optimizer: str = "score_tilted_inverse_vol",
) -> str | None:
    """백그라운드 분석 결과를 전략조회용 최신 스냅샷으로 저장한다."""
    if not isinstance(result, dict):
        return None
    scan = result.get("candidate_scan")
    if not isinstance(scan, dict) or int(scan.get("scanned") or 0) <= 0:
        return None

    strategy = next(
        (item for item in registered_strategies if str(item.get("id")) == str(strategy_id)),
        None,
    )
    rows = []
    for candidate in scan.get("candidates") or []:
        row = dict(candidate)
        row["strategy_id"] = str(strategy_id)
        if strategy:
            row["strategy_version"] = strategy.get("strategy_version")
            row["profile_hash"] = strategy.get("profile_hash")
        rows.append(row)

    min_score = int(scan.get("min_score") or 2)
    return _save_candidate_cache(
        min_score,
        rows,
        list(scan.get("scan_summary") or []),
        int(scan.get("scanned") or 0),
        str(strategy_id),
        optimizer,
    )


def _run_scheduled_cycles_for_strategies(
    mode: str,
    include_ai_rebalance: bool,
    auto_approve: bool,
    strategy_ids: list[str],
    allowed_categories: set[str] | None = None,
) -> dict:
    from src.scheduler import run_scheduled_cycle, _sync_order_status_before_cycle
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

    shared_pre_order_status_sync = (
        _sync_order_status_before_cycle()
        if mode != "analysis_only"
        and any(
            strategy_id in ISOLATED_STRATEGY_IDS
            or strategy_id not in registered_ai_ids
            for strategy_id in requested_strategy_ids
        )
        else None
    )

    runs = []
    errors = []
    for strategy_id in requested_strategy_ids:
        if strategy_id in ISOLATED_STRATEGY_IDS:
            try:
                cycle_kwargs = {
                    "include_ai_rebalance": False,
                    "auto_approve": auto_approve,
                    "force_strategy_id": strategy_id,
                    "allowed_categories": {"candidate"},
                }
                if shared_pre_order_status_sync is not None:
                    cycle_kwargs["pre_order_status_sync"] = shared_pre_order_status_sync
                result = run_scheduled_cycle(mode, **cycle_kwargs)
                if mode == "analysis_only":
                    _persist_strategy_lookup_candidate_snapshot(
                        strategy_id, result, registered_strategies
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
            trader.runtime_flags().trading_env,
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
                cycle_kwargs = {
                    "include_ai_rebalance": include_ai_rebalance,
                    "auto_approve": auto_approve,
                    "force_strategy_id": strategy_id,
                    "allowed_categories": allowed_categories,
                }
                if shared_pre_order_status_sync is not None:
                    cycle_kwargs["pre_order_status_sync"] = shared_pre_order_status_sync
                result = run_scheduled_cycle(mode, **cycle_kwargs)
            if mode == "analysis_only" and allowed_categories == {"candidate"}:
                _persist_strategy_lookup_candidate_snapshot(
                    strategy_id, result, registered_strategies
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
    run_id: str | None = None,
):
    _dashboard_scheduler_service.run(
        _run_scheduled_cycles_for_strategies,
        mode=mode,
        include_ai_rebalance=include_ai_rebalance,
        auto_approve=auto_approve,
        strategy_ids=strategy_ids,
        allowed_categories=allowed_categories,
        run_id=run_id,
    )
