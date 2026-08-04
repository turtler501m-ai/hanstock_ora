# -*- coding: utf-8 -*-
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse
import src.dashboard.core as _core
from src.dashboard.core import *
globals().update({k: v for k, v in _core.__dict__.items() if not k.startswith('__')})

router = APIRouter(tags=["settings"])

_kis_websocket_client = None
_kis_websocket_lock = threading.Lock()


def _kis_websocket_status() -> dict:
    client = _kis_websocket_client
    running = bool(client and client.running and client.is_alive())
    return {
        "enabled": bool(getattr(trader.config, "kis_websocket_enabled", False)),
        "running": running,
        "trading_env": trader.runtime_flags().trading_env,
        "hts_id": getattr(trader.config, "kistock_hts_id", "") or "",
        "subscriptions": sorted([f"{tr_id}:{tr_key}" for tr_id, tr_key in getattr(client, "active_subscriptions", set())]) if client else [],
        "reconnect_count": int(getattr(client, "reconnect_count", 0) or 0) if client else 0,
        "last_message_at": getattr(client, "last_message_at", None) if client else None,
        "last_error": getattr(client, "last_error", "") if client else "",
        "last_quotes": getattr(client, "last_quotes", {}) if client else {},
        "last_orderbooks": getattr(client, "last_orderbooks", {}) if client else {},
    }


def _start_kis_websocket() -> dict:
    global _kis_websocket_client
    from src.online_access import require_online_access

    require_online_access("KIS WebSocket")
    with _kis_websocket_lock:
        if _kis_websocket_client and _kis_websocket_client.running and _kis_websocket_client.is_alive():
            return {"ok": True, **_kis_websocket_status()}
        from src.api.kis_websocket import KISWebSocketClient

        _kis_websocket_client = KISWebSocketClient()
        _kis_websocket_client.start()
        return {"ok": True, **_kis_websocket_status()}


def _stop_kis_websocket() -> dict:
    global _kis_websocket_client
    with _kis_websocket_lock:
        if _kis_websocket_client:
            _kis_websocket_client.stop()
            _kis_websocket_client = None
        return {"ok": True, **_kis_websocket_status()}


def start_kis_websocket_if_enabled():
    if bool(getattr(trader.config, "kis_websocket_enabled", False)):
        _start_kis_websocket()


def start_snapshot_refresher_if_enabled():
    # DASHBOARD_SNAPSHOT_REFRESH_ENABLED=true일 때만 백그라운드로 DB 스냅샷을 데운다.
    try:
        start_snapshot_refresher()
    except Exception:
        pass


def start_auto_approval_sweeper_on_boot():
    # 자동승인 토글이 켜져 있으면 cron이 만든 대기 승인도 주기적으로 일괄 처리한다.
    try:
        start_auto_approval_sweeper()
    except Exception:
        pass


def run_dashboard_startup_tasks() -> None:
    start_kis_websocket_if_enabled()
    start_snapshot_refresher_if_enabled()
    start_auto_approval_sweeper_on_boot()


def _current_env_field_value(key: str, raw_values: dict[str, str]) -> str:
    if key in raw_values:
        return raw_values.get(key, "")
    runtime_values = {
        "TRADING_ENV": getattr(trader.config, "trading_env", trader.runtime_flags().trading_env),
        "DRY_RUN": str(bool(getattr(trader.config, "dry_run", trader.runtime_flags().dry_run))).lower(),
        "ENABLE_LIVE_TRADING": str(bool(getattr(trader.config, "enable_live_trading", trader.runtime_flags().enable_live_trading))).lower(),
        "REQUIRE_APPROVAL": str(bool(getattr(trader.config, "require_approval", trader.runtime_flags().require_approval))).lower(),
        "ONLINE_ACCESS_BLOCKED": str(bool(getattr(trader.config, "online_access_blocked", False))).lower(),
        "SPLIT_N": getattr(trader.config, "split_n", trader.SPLIT_N),
        "STOP_LOSS_PCT": getattr(trader.config, "stop_loss_pct", trader.STOP_LOSS_PCT),
        "TAKE_PROFIT": getattr(trader.config, "take_profit", trader.TAKE_PROFIT),
        "RSI_BUY": getattr(trader.config, "rsi_buy", trader.RSI_BUY),
        "RSI_SELL": getattr(trader.config, "rsi_sell", trader.RSI_SELL),
        "TRAILING_STOP_ACTIVATION_PCT": getattr(trader.config, "trailing_stop_activation_pct", 10),
        "TRAILING_STOP_PCT": getattr(trader.config, "trailing_stop_pct", 6),
        "TRAILING_STOP_LOOKBACK": getattr(trader.config, "trailing_stop_lookback", 20),
        "TRADE_VALUE_SURGE_RATIO": getattr(trader.config, "trade_value_surge_ratio", 1.5),
        "FIRST_WAVE_MIN_PCT": getattr(trader.config, "first_wave_min_pct", 12),
        "FIRST_WAVE_PULLBACK_MIN_PCT": getattr(trader.config, "first_wave_pullback_min_pct", 3),
        "FIRST_WAVE_PULLBACK_MAX_PCT": getattr(trader.config, "first_wave_pullback_max_pct", 12),
        "TOTAL_CAPITAL": getattr(trader.config, "total_capital", trader.get_settings().total_capital),
        "ACCOUNT_INITIAL_CAPITAL": getattr(trader.config, "account_initial_capital", 0),
        "MAX_POSITIONS": getattr(trader.config, "max_positions", trader.get_settings().max_positions),
        "MAX_SINGLE_WEIGHT": getattr(trader.config, "max_single_weight", trader.MAX_SINGLE_WEIGHT),
        "CASH_BUFFER": getattr(trader.config, "cash_buffer", trader.CASH_BUFFER),
        "MAX_DAILY_LOSS_PCT": getattr(trader.config, "max_daily_loss_pct", trader.MAX_DAILY_LOSS_PCT),
        "SCAN_UNIVERSE_SIZE": getattr(trader.config, "scan_universe_size", trader.SCAN_UNIVERSE_SIZE),
        "KIS_CIRCUIT_COOLDOWN_SECONDS": getattr(trader.config, "kis_circuit_cooldown_seconds", ""),
        "TRADE_DB_PATH": getattr(trader.config, "trade_db_path", ""),
        "ACTIVE_MODEL_VERSION": getattr(trader.config, "active_model_version", ""),
        "AI_STRATEGY_ENABLED": str(bool(getattr(trader.config, "ai_strategy_enabled", False))).lower(),
        "AI_SCORE_WEIGHT": getattr(trader.config, "ai_score_weight", 0.4),
        "AI_MIN_MODEL_CONFIDENCE": getattr(trader.config, "ai_min_model_confidence", 0.6),
        "AI_REQUIRE_BACKTEST_PASS": str(bool(getattr(trader.config, "ai_require_backtest_pass", True))).lower(),
        "AI_AUTO_APPROVE": str(bool(getattr(trader.config, "ai_auto_approve", False))).lower(),
        "OPENAI_MODEL": getattr(trader.config, "openai_model", "gpt-5-mini"),
        "OPENAI_TIMEOUT_SECONDS": getattr(trader.config, "openai_timeout_seconds", 20.0),
        "AI_CANDIDATE_LIMIT": getattr(trader.config, "ai_candidate_limit", 5),
        "OPENAI_API_KEY": getattr(trader.config, "openai_api_key", ""),
        "SLACK_WEBHOOK_URL": getattr(trader.config, "slack_webhook_url", ""),
        "MISTOCK_SLACK_WEBHOOK_URL": getattr(trader.config, "mistock_slack_webhook_url", ""),
        "TELEGRAM_API_ID": getattr(trader.config, "telegram_api_id", "") or "",
        "TELEGRAM_API_HASH": getattr(trader.config, "telegram_api_hash", "") or "",
        "TELEGRAM_TARGET_CHANNELS": getattr(trader.config, "telegram_target_channels", "") or "",
        "KISTOCK_HTS_ID": getattr(trader.config, "kistock_hts_id", "") or "",
        "KIS_WEBSOCKET_ENABLED": str(bool(getattr(trader.config, "kis_websocket_enabled", False))).lower(),
        "KIS_CONDITION_SEARCH_ENABLED": str(bool(getattr(trader.config, "kis_condition_search_enabled", False))).lower(),
        "KIS_CONDITION_USER_ID": getattr(trader.config, "kis_condition_user_id", "") or "",
        "KIS_CONDITION_SEQ": getattr(trader.config, "kis_condition_seq", "") or "",
        "KIS_CONDITION_NAME": getattr(trader.config, "kis_condition_name", "") or "",
        "MISTOCK_EXCHANGE_MAP": os.environ.get("MISTOCK_EXCHANGE_MAP", ""),
        "MISTOCK_UNIVERSE": ",".join(sys.modules["src.mistock.config"].config.universe_list) if "src.mistock.config" in sys.modules else os.environ.get("MISTOCK_UNIVERSE", ""),
    }
    value = runtime_values.get(key, "")
    return "" if value is None else str(value)


@router.get("/api/config")
def get_config():
    from src.strategy.technical_readiness import build_technical_strategy_readiness

    return {
        "trading_env": trader.runtime_flags().trading_env,
        "dry_run": trader.runtime_flags().dry_run,
        "enable_live_trading": trader.runtime_flags().enable_live_trading,
        "require_approval": trader.runtime_flags().require_approval,
        "online_access_blocked": bool(getattr(trader.config, "online_access_blocked", False)),
        "order_submission_enabled": trader.runtime_flags().order_submission_enabled,
        "real_orders_enabled": trader.runtime_flags().real_orders_enabled,
        "kistock_account": trader.config.kistock_account,
        "split_n": trader.SPLIT_N,
        "stop_loss_pct": trader.STOP_LOSS_PCT,
        "take_profit": trader.TAKE_PROFIT,
        "rsi_buy": trader.RSI_BUY,
        "rsi_sell": trader.RSI_SELL,
        "trailing_stop_activation_pct": trader.config.trailing_stop_activation_pct,
        "trailing_stop_pct": trader.config.trailing_stop_pct,
        "trailing_stop_lookback": trader.config.trailing_stop_lookback,
        "trade_value_surge_ratio": trader.config.trade_value_surge_ratio,
        "first_wave_min_pct": trader.config.first_wave_min_pct,
        "first_wave_pullback_min_pct": trader.config.first_wave_pullback_min_pct,
        "first_wave_pullback_max_pct": trader.config.first_wave_pullback_max_pct,
        "total_capital": trader.get_settings().total_capital,
        "account_initial_capital": getattr(trader.config, "account_initial_capital", 0),
        "max_positions": trader.get_settings().max_positions,
        "max_single_weight": trader.MAX_SINGLE_WEIGHT,
        "cash_buffer": trader.CASH_BUFFER,
        "max_daily_loss_pct": trader.MAX_DAILY_LOSS_PCT,
        "watchlist": trader.WATCHLIST,
        "scan_universe_size": trader.SCAN_UNIVERSE_SIZE,
        "kospi_universe_size": len(trader.KOSPI_UNIVERSE),
        "strategy_sources": [
            "RSI recovery + MACD confirmation",
            "Bollinger mean reversion",
            "Trend pullback with short RSI",
            "20-day breakout with volume",
            "FinRL-X inspired weight-centric allocation",
        ],
        "ai_analysis": _ai_analysis_config(),
        "technical_strategy_readiness": build_technical_strategy_readiness(),
    }


@router.get("/api/technical-strategy/readiness")
def technical_strategy_readiness():
    from src.strategy.technical_readiness import build_technical_strategy_readiness

    return build_technical_strategy_readiness()




@router.get("/api/env")
def get_env_settings():
    env_path = _public_value("ENV_PATH", ENV_PATH)
    values = _read_env_values(env_path)
    fields = []
    for field in ENV_FIELDS:
        key = field["key"]
        value = _virtual_env_value(key, values) if field.get("virtual") else _current_env_field_value(key, values)
        is_secret = bool(field.get("secret"))
        display_type = "text" if is_secret else field["type"]
        item = {
            "key": key,
            "label": field["label"],
            "type": display_type,
            "options": field.get("options", []),
            "hint": field.get("hint", ""),
            "secret": False,
            "virtual": bool(field.get("virtual")),
            "has_value": bool(value),
            "value": value,
            "masked": "",
        }
        fields.append(item)
    # Get live exchange rate metrics for display
    try:
        import src.utils.exchange_rate as ex_rate
        from datetime import datetime, timezone, timedelta
        current_rate = ex_rate.get_usd_krw_rate()
        last_fetch = ex_rate._USD_KRW_LAST_FETCH
        last_fetch_str = "미수집"
        if last_fetch > 0:
            last_fetch_str = datetime.fromtimestamp(last_fetch, timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
        rate_info = {
            "current_rate": round(current_rate, 2),
            "last_fetch_time": last_fetch_str
        }
    except Exception:
        rate_info = {
            "current_rate": 1380.0,
            "last_fetch_time": "미수집"
        }
        
    return {
        "path": str(env_path),
        "exists": env_path.exists(),
        "requires_restart": True,
        "fields": fields,
        "rate_info": rate_info,
    }




@router.post("/api/env")
def update_env_settings(payload: dict = Body(...)):
    raw_updates = payload.get("values")
    if not isinstance(raw_updates, dict):
        raise HTTPException(status_code=400, detail="values must be an object")

    updates: dict[str, str] = {}
    for key, value in raw_updates.items():
        if key not in ENV_FIELD_MAP:
            raise HTTPException(status_code=400, detail=f"{key} is not editable")
        field = ENV_FIELD_MAP[key]
        if field["type"] == "secret" and str(value).strip() == "":
            continue
        updates[key] = _validate_env_value(key, value)

    if updates:
        updates = _expand_virtual_env_updates(updates)
        _write_env_values(updates, _public_value("ENV_PATH", ENV_PATH))
        _apply_runtime_env_updates(updates)
        _apply_strategy_env_updates(updates)
        if _env_bool_value(updates, "ONLINE_ACCESS_BLOCKED", False):
            _stop_kis_websocket()
    return {
        "ok": True,
        "updated": sorted(updates.keys()),
        "requires_restart": False,
    }




@router.post("/api/circuit-breaker/reset")
def reset_circuit_breaker():
    KIStockAPI.reset_circuit()
    return {"ok": True, "circuit_breaker": KIStockAPI.circuit_status()}




@router.post("/api/auto-approval")
def set_auto_approval(payload: dict = Body(...)):
    enabled = bool(payload.get("enabled"))
    _save_auto_approval(enabled)
    processed = _auto_approve_pending_approvals() if enabled else []
    return {"ok": True, "enabled": enabled, "processed": processed, "processed_count": len(processed)}




@router.post("/api/runtime/order-mode")
def set_runtime_order_mode(payload: dict = Body(...)):
    key = str(payload.get("key", "")).strip()
    enabled = bool(payload.get("enabled"))
    updates = _runtime_order_mode_updates(key, enabled)
    _write_env_values(updates, _public_value("ENV_PATH", ENV_PATH))
    _apply_runtime_env_updates(updates)
    return {
        "ok": True,
        "updated": sorted(updates.keys()),
        "trading_env": trader.runtime_flags().trading_env,
        "dry_run": trader.runtime_flags().dry_run,
        "enable_live_trading": trader.runtime_flags().enable_live_trading,
        "online_access_blocked": bool(getattr(trader.config, "online_access_blocked", False)),
        "order_submission_enabled": trader.runtime_flags().order_submission_enabled,
        "real_orders_enabled": trader.runtime_flags().real_orders_enabled,
        "requires_restart": False,
    }


@router.get("/api/kis/condition-search/list")
def get_kis_condition_search_list(user_id: str | None = None):
    from src.online_access import require_online_access

    require_online_access("KIS condition search")
    lookup_user_id = (user_id or getattr(trader.config, "kis_condition_user_id", "") or getattr(trader.config, "kistock_hts_id", "") or "").strip()
    if not lookup_user_id:
        raise HTTPException(status_code=400, detail="KIS condition user_id or KISTOCK_HTS_ID is required")
    api = KIStockAPI()
    return {"ok": True, "user_id": lookup_user_id, "conditions": api.get_condition_search_list(lookup_user_id)}


@router.get("/api/kis/condition-search/result")
def get_kis_condition_search_result(
    seq: str | None = None,
    name: str | None = None,
    user_id: str | None = None,
):
    from src.online_access import require_online_access

    require_online_access("KIS condition search")
    lookup_user_id = (user_id or getattr(trader.config, "kis_condition_user_id", "") or getattr(trader.config, "kistock_hts_id", "") or "").strip()
    condition_seq = (seq or getattr(trader.config, "kis_condition_seq", "") or "").strip()
    condition_name = (name or getattr(trader.config, "kis_condition_name", "") or "").strip()
    if not lookup_user_id:
        raise HTTPException(status_code=400, detail="KIS condition user_id or KISTOCK_HTS_ID is required")
    if not condition_seq or not condition_name:
        raise HTTPException(status_code=400, detail="KIS condition seq and name are required")
    api = KIStockAPI()
    codes = api.get_condition_search_result(lookup_user_id, condition_seq, condition_name)
    return {"ok": True, "user_id": lookup_user_id, "seq": condition_seq, "name": condition_name, "codes": codes, "count": len(codes)}


@router.get("/api/kis/websocket/status")
def get_kis_websocket_status():
    return {"ok": True, **_kis_websocket_status()}


@router.post("/api/kis/websocket/start")
def start_kis_websocket():
    from src.online_access import require_online_access

    require_online_access("KIS WebSocket")
    if not bool(getattr(trader.config, "kis_websocket_enabled", False)):
        raise HTTPException(status_code=409, detail="KIS_WEBSOCKET_ENABLED is false")
    if not (getattr(trader.config, "kistock_hts_id", "") or trader.config.kistock_account):
        raise HTTPException(status_code=400, detail="KISTOCK_HTS_ID or KISTOCK_ACCOUNT is required")
    return _start_kis_websocket()


@router.post("/api/kis/websocket/stop")
def stop_kis_websocket():
    return _stop_kis_websocket()


@router.post("/api/kis/websocket/subscribe")
def subscribe_kis_websocket(payload: dict = Body(...)):
    from src.online_access import require_online_access

    require_online_access("KIS WebSocket subscription")
    client = _kis_websocket_client
    if not client:
        raise HTTPException(status_code=409, detail="KIS WebSocket is not running")
    symbol = str(payload.get("symbol") or "").strip()
    stream = str(payload.get("stream") or "quote").strip().lower()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if stream == "quote":
        client.subscribe_quote(symbol)
    elif stream in {"orderbook", "askbid"}:
        client.subscribe_orderbook(symbol)
    else:
        raise HTTPException(status_code=400, detail="stream must be quote or orderbook")
    return {"ok": True, **_kis_websocket_status()}


@router.post("/api/kis/orders/cancel")
def cancel_kis_stock_order(payload: dict = Body(...)):
    from src.online_access import require_online_access

    require_online_access("KIS order cancellation")
    order_no = str(payload.get("order_no") or payload.get("original_order_no") or "").strip()
    if not order_no:
        raise HTTPException(status_code=400, detail="order_no is required")
    api = KIStockAPI()
    result = api.cancel_order(
        order_no,
        qty=_to_int(payload.get("qty")),
        order_division=str(payload.get("order_division") or "00"),
        original_order_branch=str(payload.get("original_order_branch") or ""),
        exchange_id=str(payload.get("exchange_id") or "KRX"),
        cancel_all=bool(payload.get("cancel_all", True)),
    )
    return {"ok": result.get("rt_cd") == "0", "result": result}


@router.post("/api/kis/orders/revise")
def revise_kis_stock_order(payload: dict = Body(...)):
    from src.online_access import require_online_access

    require_online_access("KIS order revision")
    order_no = str(payload.get("order_no") or payload.get("original_order_no") or "").strip()
    qty = _to_int(payload.get("qty"))
    price = _to_int(payload.get("price"))
    if not order_no:
        raise HTTPException(status_code=400, detail="order_no is required")
    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be greater than 0")
    result = KIStockAPI().revise_order(
        order_no,
        qty=qty,
        price=price,
        order_division=str(payload.get("order_division") or "00"),
        original_order_branch=str(payload.get("original_order_branch") or ""),
        exchange_id=str(payload.get("exchange_id") or "KRX"),
    )
    return {"ok": result.get("rt_cd") == "0", "result": result}


@router.get("/api/kis/rehearsal")
def get_kis_rehearsal():
    checks = []
    required = {
        "KISTOCK_APP_KEY": bool(str(getattr(trader.config, "kistock_app_key", "") or "").strip()),
        "KISTOCK_APP_SECRET": bool(str(getattr(trader.config, "kistock_app_secret", "") or "").strip()),
        "KISTOCK_ACCOUNT": bool(str(getattr(trader.config, "kistock_account", "") or "").strip()),
    }
    for key, ok in required.items():
        checks.append({"key": key, "ok": ok, "critical": True})

    checks.append({"key": "DRY_RUN", "ok": bool(trader.runtime_flags().dry_run), "critical": False})
    checks.append({"key": "ORDER_SUBMISSION_ENABLED", "ok": bool(trader.runtime_flags().order_submission_enabled), "critical": False})
    checks.append({"key": "WEBSOCKET_CONFIGURED", "ok": bool(getattr(trader.config, "kistock_hts_id", "") or trader.config.kistock_account), "critical": False})
    checks.append({"key": "CONDITION_SEARCH_CONFIGURED", "ok": bool(
        getattr(trader.config, "kis_condition_seq", "")
        and getattr(trader.config, "kis_condition_name", "")
        and (getattr(trader.config, "kis_condition_user_id", "") or getattr(trader.config, "kistock_hts_id", ""))
    ), "critical": False})

    sample_order = {
        "CANO": str(getattr(trader.config, "kistock_account", "") or "")[:8],
        "ACNT_PRDT_CD": str(getattr(trader.config, "kistock_account", "") or "")[8:] or "01",
        "PDNO": "005930",
        "ORD_DVSN": "01",
        "ORD_QTY": "1",
        "ORD_UNPR": "0",
        "EXCG_ID_DVSN_CD": "KRX",
    }
    critical_ok = all(item["ok"] for item in checks if item["critical"])
    return {
        "ok": critical_ok,
        "trading_env": trader.runtime_flags().trading_env,
        "dry_run": bool(trader.runtime_flags().dry_run),
        "order_submission_enabled": bool(trader.runtime_flags().order_submission_enabled),
        "real_orders_enabled": bool(trader.runtime_flags().real_orders_enabled),
        "checks": checks,
        "sample_order_payload": sample_order,
        "websocket": _kis_websocket_status(),
    }


@router.post("/api/config/reset-database")
def reset_database_and_clear_cache():
    import shutil
    from datetime import datetime
    from pathlib import Path
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []
    
    # 1. 국내 주식 거래 DB 백업 및 초기화
    db_path_str = getattr(trader.config, "trade_db_path", ".runtime/trades.sqlite")
    if db_path_str:
        db_path = Path(db_path_str)
        if db_path.exists():
            backup_path = db_path.with_name(f"{db_path.name}.backup_{timestamp}")
            try:
                shutil.move(str(db_path), str(backup_path))
                results.append(f"국내주식 DB 백업 완료: {backup_path.name}")
            except Exception as e:
                results.append(f"국내주식 DB 백업 실패: {str(e)}")
                
    # 2. 미국 주식 거래 DB 백업 및 초기화
    try:
        from src.mistock.config import config as mistock_config
        mistock_db_path_str = getattr(mistock_config, "trade_db_path", ".runtime/mistock/trades.sqlite")
        if mistock_db_path_str:
            mistock_db_path = Path(mistock_db_path_str)
            if mistock_db_path.exists():
                mistock_backup_path = mistock_db_path.with_name(f"{mistock_db_path.name}.backup_{timestamp}")
                shutil.move(str(mistock_db_path), str(mistock_backup_path))
                results.append(f"미국주식 DB 백업 완료: {mistock_backup_path.name}")
    except Exception as e:
        results.append(f"미국주식 DB 초기화 실패: {str(e)}")

    # 3. KIS 토큰 캐시 파일 일괄 제거
    token_files = ["data/kis_token.json", "data/kis_token_01.json", "data/kis_token_mistock_demo.json"]
    for t_file in token_files:
        p = Path(t_file)
        if p.exists():
            try:
                p.unlink()
                results.append(f"토큰 캐시 제거 완료: {p.name}")
            except Exception as e:
                results.append(f"토큰 캐시 제거 실패 ({p.name}): {str(e)}")
                
    # 4. 잔고 스냅샷 및 DB 캐시 제거
    cache_files = [".runtime/balance_snapshot.json", ".runtime/db_cache.sqlite"]
    for c_file in cache_files:
        p = Path(c_file)
        if p.exists():
            try:
                p.unlink()
                results.append(f"캐시 파일 제거 완료: {p.name}")
            except Exception as e:
                results.append(f"캐시 파일 제거 실패 ({p.name}): {str(e)}")

    # 5. 메모리 내 캐시 털기
    try:
        _clear_balance_cache()
        results.append("메모리 잔고 캐시 초기화 완료")
    except Exception:
        pass
        
    try:
        import src.mistock.trader as mistock_trader
        mistock_trader._kis_client_cache = None
        results.append("미국주식 클라이언트 캐시 초기화 완료")
    except Exception:
        pass
        
    try:
        global _cloud_trades_cache, _cloud_trades_cache_time
        _cloud_trades_cache = None
        _cloud_trades_cache_time = 0
        results.append("클라우드 거래 이력 캐시 초기화 완료")
    except Exception:
        pass

    # DB 초기화를 위해 재호출
    try:
        trader.init_db()
        results.append("새로운 거래 DB 테이블 생성 완료")
    except Exception as e:
        results.append(f"새 거래 DB 초기화 실패: {str(e)}")
        
    return {
        "ok": True,
        "message": "데이터베이스 및 캐시 완전 초기화가 성공적으로 수행되었습니다.",
        "details": results
    }
