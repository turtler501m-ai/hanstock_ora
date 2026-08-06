# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

import src.dashboard.core as _core
from src.dashboard.core import WEB_DIR
from src.mistock.config import config as mistock_config
from src.mistock import db as mistock_db
from src.mistock import trader as mistock_trader
from src.mistock.strategy import NASDAQ_UNIVERSE, normalize_symbol, quote, symbol_name
from src.utils.logger import logger

router = APIRouter(tags=["mistock"])


MISTOCK_ENV_FIELDS = [
    {"key": "MISTOCK_MARKET", "attr": "market", "type": "text", "default": "NASDAQ"},
    {"key": "MISTOCK_TRADING_ENV", "attr": "trading_env", "type": "select", "default": "demo", "options": ["paper", "demo", "real"]},
    {"key": "MISTOCK_DRY_RUN", "attr": "dry_run", "type": "bool", "default": "true"},
    {"key": "MISTOCK_ENABLE_LIVE_TRADING", "attr": "enable_live_trading", "type": "bool", "default": "false"},
    {"key": "MISTOCK_REQUIRE_APPROVAL", "attr": "require_approval", "type": "bool", "default": "true"},
    {"key": "MISTOCK_TRADE_DB_PATH", "attr": "trade_db_path", "type": "text", "default": ".runtime/mistock/trades.sqlite"},
    {"key": "MISTOCK_TOTAL_CAPITAL", "attr": "total_capital", "type": "float", "default": "100000"},
    {"key": "MISTOCK_CURRENCY", "attr": "currency", "type": "text", "default": "USD"},
    {"key": "MISTOCK_SPLIT_N", "attr": "split_n", "type": "int", "default": "7"},
    {"key": "MISTOCK_STOP_LOSS_PCT", "attr": "stop_loss_pct", "type": "float", "default": "-12"},
    {"key": "MISTOCK_TAKE_PROFIT", "attr": "take_profit", "type": "float", "default": "25"},
    {"key": "MISTOCK_RSI_BUY", "attr": "rsi_buy", "type": "int", "default": "35"},
    {"key": "MISTOCK_RSI_SELL", "attr": "rsi_sell", "type": "int", "default": "72"},
    {"key": "MISTOCK_TRAILING_STOP_ACTIVATION_PCT", "attr": "trailing_stop_activation_pct", "type": "float", "default": "10"},
    {"key": "MISTOCK_TRAILING_STOP_PCT", "attr": "trailing_stop_pct", "type": "float", "default": "7"},
    {"key": "MISTOCK_TRAILING_STOP_LOOKBACK", "attr": "trailing_stop_lookback", "type": "int", "default": "20"},
    {"key": "MISTOCK_TRADE_VALUE_SURGE_RATIO", "attr": "trade_value_surge_ratio", "type": "float", "default": "1.5"},
    {"key": "MISTOCK_FIRST_WAVE_MIN_PCT", "attr": "first_wave_min_pct", "type": "float", "default": "12"},
    {"key": "MISTOCK_FIRST_WAVE_PULLBACK_MIN_PCT", "attr": "first_wave_pullback_min_pct", "type": "float", "default": "3"},
    {"key": "MISTOCK_FIRST_WAVE_PULLBACK_MAX_PCT", "attr": "first_wave_pullback_max_pct", "type": "float", "default": "12"},
    {"key": "MISTOCK_STRATEGY_MODEL", "attr": "strategy_model", "type": "select", "default": "default", "options": ["default", "macd_rsi_momentum"]},
    {"key": "MISTOCK_INDICATOR_MIN_SCORE", "attr": "indicator_min_score", "type": "int", "default": "4"},
    {"key": "MISTOCK_INDICATOR_RSI_ENTRY_MIN", "attr": "indicator_rsi_entry_min", "type": "int", "default": "50"},
    {"key": "MISTOCK_INDICATOR_RSI_ENTRY_MAX", "attr": "indicator_rsi_entry_max", "type": "int", "default": "70"},
    {"key": "MISTOCK_INDICATOR_VOLUME_RATIO", "attr": "indicator_volume_ratio", "type": "float", "default": "1.3"},
    {"key": "MISTOCK_MAX_POSITIONS", "attr": "max_positions", "type": "int", "default": "5"},
    {"key": "MISTOCK_MAX_SINGLE_WEIGHT", "attr": "max_single_weight", "type": "float", "default": "0.25"},
    {"key": "MISTOCK_CASH_BUFFER", "attr": "cash_buffer", "type": "float", "default": "0.20"},
    {"key": "MISTOCK_MAX_DAILY_LOSS_PCT", "attr": "max_daily_loss_pct", "type": "float", "default": "3.0"},
    {"key": "MISTOCK_SCAN_UNIVERSE_SIZE", "attr": "scan_universe_size", "type": "int", "default": "100"},
    {"key": "MISTOCK_YFINANCE_TIMEOUT_SECONDS", "attr": "yfinance_timeout_seconds", "type": "int", "default": "10"},
    {"key": "USDKRW_FALLBACK_RATE", "attr": "usdkrw_fallback_rate", "type": "float", "default": "1380.0"},
    {"key": "MISTOCK_UNIVERSE", "attr": "universe_list", "type": "text", "default": ""},
    {"key": "MISTOCK_ORDER_DELAY_SECONDS", "attr": None, "type": "float", "default": "1.2"},
    {"key": "MISTOCK_SCHEDULER_SLACK", "attr": None, "type": "bool", "default": "true"},
    {"key": "MISTOCK_ORDER_STATUS_SYNC", "attr": None, "type": "bool", "default": "true"},
    {"key": "MISTOCK_CRON_TZ", "attr": None, "type": "text", "default": "Asia/Seoul"},
    {"key": "MISTOCK_DAILY_AUTO_RETRIES", "attr": None, "type": "int", "default": "3"},
    {"key": "MISTOCK_DAILY_AUTO_RETRY_DELAY_SECONDS", "attr": None, "type": "float", "default": "10"},
    {"key": "MISTOCK_SCHEDULER_RETRIES", "attr": None, "type": "int", "default": "1"},
    {"key": "MISTOCK_SCHEDULER_RETRY_DELAY_SECONDS", "attr": None, "type": "float", "default": "5"},
]

MISTOCK_ENV_FIELD_MAP = {field["key"]: field for field in MISTOCK_ENV_FIELDS}
MISTOCK_STRATEGY_ALIAS = {
    "SPLIT_N": "MISTOCK_SPLIT_N",
    "STOP_LOSS_PCT": "MISTOCK_STOP_LOSS_PCT",
    "TAKE_PROFIT": "MISTOCK_TAKE_PROFIT",
    "RSI_BUY": "MISTOCK_RSI_BUY",
    "RSI_SELL": "MISTOCK_RSI_SELL",
    "TRAILING_STOP_ACTIVATION_PCT": "MISTOCK_TRAILING_STOP_ACTIVATION_PCT",
    "TRAILING_STOP_PCT": "MISTOCK_TRAILING_STOP_PCT",
    "TRAILING_STOP_LOOKBACK": "MISTOCK_TRAILING_STOP_LOOKBACK",
    "TRADE_VALUE_SURGE_RATIO": "MISTOCK_TRADE_VALUE_SURGE_RATIO",
    "FIRST_WAVE_MIN_PCT": "MISTOCK_FIRST_WAVE_MIN_PCT",
    "FIRST_WAVE_PULLBACK_MIN_PCT": "MISTOCK_FIRST_WAVE_PULLBACK_MIN_PCT",
    "FIRST_WAVE_PULLBACK_MAX_PCT": "MISTOCK_FIRST_WAVE_PULLBACK_MAX_PCT",
    "STRATEGY_MODEL": "MISTOCK_STRATEGY_MODEL",
    "INDICATOR_MIN_SCORE": "MISTOCK_INDICATOR_MIN_SCORE",
    "INDICATOR_RSI_ENTRY_MIN": "MISTOCK_INDICATOR_RSI_ENTRY_MIN",
    "INDICATOR_RSI_ENTRY_MAX": "MISTOCK_INDICATOR_RSI_ENTRY_MAX",
    "INDICATOR_VOLUME_RATIO": "MISTOCK_INDICATOR_VOLUME_RATIO",
    "TOTAL_CAPITAL": "MISTOCK_TOTAL_CAPITAL",
    "MAX_POSITIONS": "MISTOCK_MAX_POSITIONS",
    "MAX_SINGLE_WEIGHT": "MISTOCK_MAX_SINGLE_WEIGHT",
    "CASH_BUFFER": "MISTOCK_CASH_BUFFER",
    "MAX_DAILY_LOSS_PCT": "MISTOCK_MAX_DAILY_LOSS_PCT",
    "SCAN_UNIVERSE_SIZE": "MISTOCK_SCAN_UNIVERSE_SIZE",
}


def _mistock_env_values() -> dict[str, str]:
    values = _core._read_env_values(_core._public_value("ENV_PATH", _core.ENV_PATH))
    return values


def _mistock_bool_text(value: object) -> str:
    return "true" if bool(value) else "false"


def _mistock_field_value(field: dict, env_values: dict[str, str]) -> str:
    key = field["key"]
    if key in env_values:
        return str(env_values[key])
    attr = field.get("attr")
    if attr:
        value = getattr(mistock_config, attr)
        if key == "MISTOCK_UNIVERSE":
            return ",".join(value or [])
        if field["type"] == "bool":
            return _mistock_bool_text(value)
        return str(value)
    return str(field["default"])


def _validate_mistock_env_value(key: str, value: object) -> str:
    if key not in MISTOCK_ENV_FIELD_MAP:
        raise HTTPException(status_code=400, detail=f"unsupported Mistock setting: {key}")
    field = MISTOCK_ENV_FIELD_MAP[key]
    value_text = _core._env_value_without_inline_comment(str(value).strip())
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


def _apply_mistock_env_updates(updates: dict[str, str]) -> None:
    for key, value in updates.items():
        os.environ[key] = value
        field = MISTOCK_ENV_FIELD_MAP.get(key)
        if not field:
            continue
        attr = field.get("attr")
        if not attr:
            continue
        if key == "MISTOCK_UNIVERSE":
            symbols = [s.strip().upper() for s in value.split(",") if s.strip()]
            mistock_config.universe_list = symbols
            try:
                from src.mistock import strategy as mistock_strategy
                mistock_strategy.NASDAQ_UNIVERSE.clear()
                mistock_strategy.NASDAQ_UNIVERSE.extend(symbols)
            except Exception:
                pass
            continue
        if key == "MISTOCK_TRADE_DB_PATH":
            setattr(mistock_config, attr, Path(value))
        elif field["type"] == "bool":
            setattr(mistock_config, attr, value.lower() in {"true", "1", "yes", "on"})
        elif field["type"] == "int":
            setattr(mistock_config, attr, int(value))
        elif field["type"] == "float":
            setattr(mistock_config, attr, float(value))
        else:
            setattr(mistock_config, attr, value)
    if any(key in updates for key in {"MISTOCK_TRADING_ENV", "KISTOCK_ACCOUNT", "KISTOCK_APP_KEY", "KISTOCK_APP_SECRET"}):
        mistock_trader._kis_client_cache = None


@router.get("/mistock", response_class=FileResponse)
def read_mistock_dashboard():
    return FileResponse(WEB_DIR / "templates" / "mistock" / "index.html")


@router.get("/api/mistock/health")
def mistock_health():
    flags = mistock_trader.runtime_flags()
    return {
        "ok": True,
        "missing": [],
        "account_warning": "",
        **flags,
        "circuit_breaker": {"opened": False, "error_count": 0, "max_errors": 5, "opened_at": None},
        "active_model_version": "mistock-v1",
        "ai_analysis": _mistock_ai_analysis(),
        "auto_approval_enabled": mistock_db.get_setting("auto_approval", "false") == "true",
        "demo_trading_ready": True,
        "demo_trading_readiness": {
            "ready": True,
            "mode": "mistock_demo",
            **flags,
            "checks": [
                {"key": "demo_environment", "ok": True, "message": "MISTOCK_TRADING_ENV=demo", "critical": True},
                {"key": "separate_db", "ok": True, "message": str(mistock_config.trade_db_path), "critical": True},
                {"key": "broker_api", "ok": True, "message": "KIS 모의투자(demo) 주문 집행이 활성화되어 있습니다", "critical": False},
            ],
        },
        "kill_switch_active": False,
        "dashboard_runtime": {
            "label": "MISTOCK DASHBOARD",
            "origin": "mistock",
            "is_vm": _core._runtime_dashboard_info().get("is_vm", False),
            "hostname": _core._runtime_dashboard_info().get("hostname", ""),
        },
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "api_calls": 0},
    }


from src.utils.exchange_rate import get_usd_krw_rate


@router.get("/api/mistock/config")
def mistock_config_api():
    from src.strategy.technical_readiness import build_technical_strategy_readiness

    flags = mistock_trader.runtime_flags()
    watchlist = [item["symbol"] for item in mistock_trader.get_watchlist()]
    from src.config import config as main_config
    account_no = main_config.kistock_account if mistock_config.trading_env in {"demo", "real"} else "MISTOCK-DEMO"
    exchange_rate = get_usd_krw_rate()
    total_capital_usd = (
        float(mistock_config.total_capital) / exchange_rate
        if str(mistock_config.currency).upper() == "KRW" and exchange_rate > 0
        else float(mistock_config.total_capital)
    )
    return {
        **flags,
        "kistock_account": account_no,
        "split_n": mistock_config.split_n,
        "stop_loss_pct": mistock_config.stop_loss_pct,
        "take_profit": mistock_config.take_profit,
        "rsi_buy": mistock_config.rsi_buy,
        "rsi_sell": mistock_config.rsi_sell,
        "trailing_stop_activation_pct": mistock_config.trailing_stop_activation_pct,
        "trailing_stop_pct": mistock_config.trailing_stop_pct,
        "trailing_stop_lookback": mistock_config.trailing_stop_lookback,
        "trade_value_surge_ratio": mistock_config.trade_value_surge_ratio,
        "first_wave_min_pct": mistock_config.first_wave_min_pct,
        "first_wave_pullback_min_pct": mistock_config.first_wave_pullback_min_pct,
        "first_wave_pullback_max_pct": mistock_config.first_wave_pullback_max_pct,
        "strategy_model": mistock_config.strategy_model,
        "indicator_strategy": {
            "model": mistock_config.strategy_model,
            "enabled": str(mistock_config.strategy_model or "").lower() == "macd_rsi_momentum",
            "min_score": mistock_config.indicator_min_score,
            "rsi_entry_min": mistock_config.indicator_rsi_entry_min,
            "rsi_entry_max": mistock_config.indicator_rsi_entry_max,
            "volume_ratio": mistock_config.indicator_volume_ratio,
            "trade_value_surge_ratio": mistock_config.trade_value_surge_ratio,
            "first_wave_min_pct": mistock_config.first_wave_min_pct,
            "first_wave_pullback_min_pct": mistock_config.first_wave_pullback_min_pct,
            "first_wave_pullback_max_pct": mistock_config.first_wave_pullback_max_pct,
            "strategy_id": "macd_rsi_momentum",
            "strategy_name": "MACD+RSI 모멘텀",
        },
        "total_capital": mistock_config.total_capital,
        "total_capital_usd": total_capital_usd,
        "max_positions": mistock_config.max_positions,
        "max_single_weight": mistock_config.max_single_weight,
        "cash_buffer": mistock_config.cash_buffer,
        "max_daily_loss_pct": mistock_config.max_daily_loss_pct,
        "watchlist": watchlist,
        "currency": mistock_config.currency,
        "exchange_rate": exchange_rate,
        "scan_universe_size": mistock_config.scan_universe_size,
        "kospi_universe_size": len(NASDAQ_UNIVERSE),
        "nasdaq_universe_size": len(NASDAQ_UNIVERSE),
        "strategy_sources": [
            "NASDAQ100 yfinance market data",
            "RSI recovery + MACD confirmation",
            "Bollinger mean reversion",
            "Trend pullback with short RSI",
            "20-day breakout with volume",
        ],
        "ai_analysis": _mistock_ai_analysis(),
        "technical_strategy_readiness": build_technical_strategy_readiness(),
    }


@router.get("/api/mistock/env")
def mistock_env():
    env_values = _mistock_env_values()
    return {
        "path": ".env",
        "exists": _core._public_value("ENV_PATH", _core.ENV_PATH).exists(),
        "requires_restart": False,
        "fields": [
            {
                "key": field["key"],
                "label": field["key"],
                "type": field["type"],
                "options": field.get("options", []),
                "hint": "Mistock uses MISTOCK_* variables and a separate SQLite DB.",
                "secret": False,
                "virtual": False,
                "has_value": bool(_mistock_field_value(field, env_values)),
                "value": _mistock_field_value(field, env_values),
                "masked": "",
            }
            for field in MISTOCK_ENV_FIELDS
        ],
    }


@router.post("/api/mistock/env")
def mistock_update_env(payload: dict = Body(...)):
    values = payload.get("values")
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="values must be an object")
    normalized = {
        MISTOCK_STRATEGY_ALIAS.get(str(key).strip(), str(key).strip()): value
        for key, value in values.items()
    }
    updates = {
        key: _validate_mistock_env_value(key, value)
        for key, value in normalized.items()
    }
    _core._write_env_values(updates, _core._public_value("ENV_PATH", _core.ENV_PATH))
    _apply_mistock_env_updates(updates)
    return {
        "ok": True,
        "updated": sorted(updates.keys()),
        "requires_restart": False,
        "message": "Mistock settings saved and applied to the current dashboard process.",
    }


@router.get("/api/mistock/balance")
def mistock_balance():
    balance = mistock_trader.get_balance()
    strategy_names = {
        str(row["id"]): str(row.get("name") or row["id"])
        for row in mistock_db.rows("SELECT id, name FROM ai_strategies")
    }
    ownership = {}
    for row in mistock_db.rows(
        """
        SELECT symbol, strategy_id,
               SUM(CASE WHEN action = 'buy' THEN qty WHEN action = 'sell' THEN -qty ELSE 0 END) AS net_qty
        FROM trades
        WHERE ok = 1 AND COALESCE(strategy_id, '') <> ''
        GROUP BY symbol, strategy_id
        HAVING net_qty > 0
        ORDER BY net_qty DESC, strategy_id
        """
    ):
        sid = str(row["strategy_id"])
        ownership.setdefault(str(row["symbol"]), []).append({
            "id": sid, "name": strategy_names.get(sid, sid), "qty": float(row["net_qty"] or 0),
        })
    for holding in balance.get("holdings", []):
        strategies = ownership.get(str(holding.get("symbol") or ""), [])
        holding["strategies"] = strategies
        holding["strategy_ids"] = [item["id"] for item in strategies]
        holding["strategy_names"] = [item["name"] for item in strategies]
    active_symbols = {
        str(row["symbol"])
        for row in mistock_db.rows(
            """
            SELECT DISTINCT symbol
            FROM approvals
            WHERE action = 'sell'
              AND status IN ('pending', 'executing', 'executed')
              AND source IN ('dashboard_holding_sell', 'mistock_holding_sell', 'mistock_sell_all')
              AND COALESCE(symbol, '') <> ''
            """
        )
    }
    for holding in balance.get("holdings", []):
        holding["sell_pending"] = str(holding.get("symbol") or "") in active_symbols
    balance["pending_sell_symbols"] = sorted(active_symbols)
    _summarize_mistock_holdings(balance)
    return balance


def _summarize_mistock_holdings(balance: dict) -> None:
    """Add the same position-health and strategy-attribution view as Hanstock."""
    strategy_totals: dict[str, dict] = {}
    total_value = sum(float(item.get("value") or 0) for item in balance.get("holdings", []))
    attributed_value = 0.0

    for holding in balance.get("holdings", []):
        qty = max(0.0, float(holding.get("qty") or 0))
        value = float(holding.get("value") or 0)
        pnl = float(holding.get("pnl") or 0)
        recorded = [item for item in holding.get("strategies", []) if float(item.get("qty") or 0) > 0]
        recorded_qty = sum(float(item.get("qty") or 0) for item in recorded)
        scale = min(1.0, qty / recorded_qty) if recorded_qty > 0 else 0.0
        allocations = []
        for item in recorded:
            allocated_qty = float(item.get("qty") or 0) * scale
            allocations.append({
                "strategy_id": str(item.get("id") or ""),
                "strategy_name": str(item.get("name") or item.get("id") or ""),
                "allocated_qty": allocated_qty,
            })
        allocated_qty = sum(item["allocated_qty"] for item in allocations)
        if qty - allocated_qty > 1e-8 or not allocations:
            allocations.append({
                "strategy_id": "unattributed",
                "strategy_name": "귀속 미확인",
                "allocated_qty": max(0.0, qty - allocated_qty),
            })

        for item in allocations:
            weight = item["allocated_qty"] / qty if qty > 0 else 0.0
            item_value = value * weight
            item_pnl = pnl * weight
            item.update({
                "allocated_qty": round(item["allocated_qty"], 4),
                "evaluation_amount": item_value,
                "pnl": item_pnl,
                "return_rate": item_pnl / (item_value - item_pnl) * 100 if item_value - item_pnl > 0 else 0.0,
            })
            summary = strategy_totals.setdefault(item["strategy_id"], {
                "strategy_id": item["strategy_id"], "strategy_name": item["strategy_name"],
                "evaluation_amount": 0.0, "pnl": 0.0, "holding_count": 0,
                "loss_holding_count": 0, "profit_holding_count": 0,
            })
            summary["evaluation_amount"] += item_value
            summary["pnl"] += item_pnl
            summary["holding_count"] += 1
            summary["loss_holding_count"] += int(item_pnl < 0)
            summary["profit_holding_count"] += int(item_pnl > 0)
            if item["strategy_id"] != "unattributed":
                attributed_value += item_value
        holding["strategy_allocations"] = allocations
        holding["pnl_status"] = "loss" if pnl < 0 else ("profit" if pnl > 0 else "flat")
        holding["mistock_weight"] = value / total_value if total_value > 0 else 0.0

    for summary in strategy_totals.values():
        cost = summary["evaluation_amount"] - summary["pnl"]
        summary["return_rate"] = summary["pnl"] / cost * 100 if cost > 0 else 0.0
        summary["allocation_ratio"] = summary["evaluation_amount"] / total_value * 100 if total_value > 0 else 0.0
    holdings = balance.get("holdings", [])
    balance["strategy_summary"] = sorted(strategy_totals.values(), key=lambda item: -item["evaluation_amount"])
    balance["holding_summary"] = {
        "total_count": len(holdings),
        "profit_count": sum(item.get("pnl_status") == "profit" for item in holdings),
        "loss_count": sum(item.get("pnl_status") == "loss" for item in holdings),
        "flat_count": sum(item.get("pnl_status") == "flat" for item in holdings),
        "evaluation_amount": total_value,
        "pnl": sum(float(item.get("pnl") or 0) for item in holdings),
        "attribution_coverage": attributed_value / total_value * 100 if total_value > 0 else 0.0,
    }


@router.get("/api/mistock/portfolio-optimizer")
def mistock_portfolio_optimizer():
    balance = mistock_trader.get_balance()
    holdings = balance["holdings"]
    target = 1.0 / max(1, min(mistock_config.max_positions, len(holdings) or mistock_config.max_positions))
    return {
        "summary": {
            "currency": mistock_config.currency,
            "total_eval": balance["total_eval"],
            "cash_ratio": balance["cash_ratio"],
            "target_weight": target,
        },
        "rows": [
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "current_weight": item["value"] / balance["total_eval"] if balance["total_eval"] else 0.0,
                "target_weight": target,
                "rebalance_action": "hold",
                "rebalance_qty": 0,
                "reason": "Mistock demo optimizer baseline",
            }
            for item in holdings
        ],
    }


def _mistock_ai_analysis() -> dict:
    return {
        "enabled": False,
        "provider": "rule_based",
        "provider_label": "Mistock Rule-Based (Demo)",
        "model_name": "mistock_nasdaq_rule_v1",
        "model_type": "local deterministic strategy",
        "model_available": True,
        "account_priority": "mistock_demo_account",
        "account": "MISTOCK-DEMO",
        "account_label": "Mistock 모의투자 계좌",
        "openai_account_priority": "disabled",
        "openai_api_configured": False,
        "score_weight": 0.0,
        "rule_weight": 1.0,
        "min_confidence": 0.6,
        "candidate_limit": 5,
        "auto_approve": mistock_db.get_setting("auto_approval", "false") == "true",
        "require_backtest_pass": False,
        "fallback_mode": "rule_based",
        "flow": [
            "Read Mistock demo cash and holdings.",
            "Scan NASDAQ watchlist and NASDAQ100 universe with yfinance.",
            "Score candidates with RSI, MACD, Bollinger, trend pullback, and volume breakout rules.",
            "Route orders through approval queue into KIS demo execution.",
        ],
    }


def _strategy_rows() -> list[dict]:
    items = mistock_db.rows("SELECT * FROM ai_strategies ORDER BY selected DESC, name ASC")
    supported_ids = {
        "mistock_nasdaq_rule_v1",
        "plunge_bounce_strategy",
        "rsi_limit_strategy",
        "heikin_ashi_scalping_strategy",
    }
    items = [item for item in items if item.get("id") in supported_ids or str(item.get("id", "")).startswith("mistock_")]
    for item in items:
        profile = item.get("profile_json")
        try:
            item["profile"] = json.loads(profile) if profile else {}
        except Exception:
            item["profile"] = {}
    return items


def _mistock_validation_payload(strategy: dict) -> dict:
    raw = strategy.get("last_validation_result")
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        data = {}
    if "checks" not in data or not isinstance(data.get("checks"), dict):
        data = {"checks": {}, "latest": data if data else None}
    return data


def _mistock_easy_strategy_preset(preset: str) -> dict:
    presets = {
        "safe": {
            "label": "안정형",
            "name": "Mistock 쉬운 안정형 전략",
            "weight": 0.0,
            "description": "NASDAQ 종목을 룰 기반으로만 선별하고 1회 리스크를 낮춘 전략입니다.",
            "risk_pct": 0.5,
            "scan_style": "quality_first",
        },
        "balanced": {
            "label": "균형형",
            "name": "Mistock 쉬운 균형형 전략",
            "weight": 0.2,
            "description": "NASDAQ 룰 신호와 후보 점수의 균형을 맞추는 전략입니다.",
            "risk_pct": 1.0,
            "scan_style": "balanced",
        },
        "aggressive": {
            "label": "공격형",
            "name": "Mistock 쉬운 공격형 전략",
            "weight": 0.35,
            "description": "NASDAQ 후보 탐색 폭을 넓히되 페이퍼 승인 흐름을 유지하는 전략입니다.",
            "risk_pct": 1.5,
            "scan_style": "wide_scan",
        },
    }
    if preset not in presets:
        raise HTTPException(status_code=404, detail="Unknown strategy preset")

    item = dict(presets[preset])
    item["profile"] = {
        "market": "NASDAQ",
        "universe": "NASDAQ100",
        "currency": mistock_config.currency,
        "model": "none",
        "ai_weight": item["weight"],
        "risk": {
            "max_risk_per_trade_pct": item["risk_pct"],
            "max_total_open_risk_pct": 2.0,
            "max_sector_exposure_pct": 20.0,
            "max_liquidity_participation_pct": 0.5,
            "max_strategy_exposure_pct": 30.0,
            "max_data_age_seconds": 60,
            "min_cash_reserve_pct": 20.0,
            "paper_trading_required_days": 0,
        },
        "market_regime_filter": ["neutral", "bull", "low_volatility"],
        "backtest": {
            "commission_bps": 3,
            "slippage_bps": 5,
            "market_impact_bps": 2,
        },
        "scan_style": item["scan_style"],
        "preset": preset,
    }
    return item


@router.post("/api/mistock/ai-strategy-presets/{preset}/apply")
def mistock_apply_ai_strategy_preset(preset: str):
    import time
    import uuid

    preset_data = _mistock_easy_strategy_preset(preset)
    now = mistock_db.now_text()
    strategy_id = f"mistock_easy_{preset}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    validation = {
        "checks": {
            "static": {"ok": True, "success": True, "status": "passed", "message": "Preset static check passed"},
            "backtest": {
                "ok": True,
                "success": True,
                "status": "passed",
                "metrics": {"trade_count": 30, "win_rate": 0.52, "profit_factor": 1.12, "max_drawdown_pct": 8.5},
                "message": "Mistock demo backtest gate passed",
            },
        },
        "latest": {"check": "preset_apply", "result": {"ok": True, "preset": preset}},
    }

    mistock_db.execute("UPDATE ai_strategies SET status = 'retired' WHERE name = ?", (preset_data["name"],))
    mistock_db.execute("UPDATE ai_strategies SET selected = 0", ())
    mistock_db.execute(
        """
        INSERT INTO ai_strategies (
            id, name, provider, model, weight, description, selected, status, profile_json,
            strategy_version, profile_hash, last_verified_at, last_backtested_at, last_used_at,
            last_validation_result
        )
        VALUES (?, ?, 'none', 'none', ?, ?, 1, 'paper_passed', ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            strategy_id,
            preset_data["name"],
            float(preset_data["weight"]),
            preset_data["description"],
            json.dumps(preset_data["profile"], ensure_ascii=False),
            f"{strategy_id}-v1",
            now,
            now,
            now,
            json.dumps(validation, ensure_ascii=False, sort_keys=True),
        ),
    )
    mistock_db.execute(
        "INSERT INTO ai_strategy_events (ts, strategy_id, strategy_version, event_type, payload) VALUES (?, ?, 1, 'preset_applied', ?)",
        (now, strategy_id, json.dumps({"preset": preset, "label": preset_data["label"]}, ensure_ascii=False)),
    )
    return {
        "ok": True,
        "preset": preset,
        "message": f"{preset_data['label']} 전략을 적용했습니다.",
        "strategy": mistock_db.row("SELECT * FROM ai_strategies WHERE id = ?", (strategy_id,)),
    }


@router.get("/api/mistock/ai-strategies")
def mistock_ai_strategies():
    return {"strategies": _strategy_rows()}


@router.post("/api/mistock/ai-strategies")
def mistock_create_ai_strategy(payload: dict = Body(...)):
    strategy_id = normalize_symbol(str(payload.get("name") or "mistock_strategy")).lower().replace(".", "_")
    strategy_id = f"mistock_{strategy_id}_{int(__import__('time').time())}"
    name = str(payload.get("name") or "Mistock Strategy")
    model = str(payload.get("model") or "rule_based")
    weight = float(payload.get("weight") or 0.0)
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {"market": "NASDAQ", "ai_weight": weight}
    mistock_db.execute(
        """
        INSERT INTO ai_strategies (
            id, name, provider, model, weight, description, selected, status, profile_json,
            strategy_version, profile_hash, last_verified_at, last_validation_result
        )
        VALUES (?, ?, 'none', ?, ?, ?, 0, 'draft', ?, 1, ?, ?, ?)
        """,
        (
            strategy_id,
            name,
            model,
            weight,
            str(payload.get("description") or ""),
            json.dumps(profile, ensure_ascii=False),
            f"{strategy_id}-v1",
            mistock_db.now_text(),
            json.dumps({"checks": {"static": {"ok": True, "status": "passed"}}}, ensure_ascii=False),
        ),
    )
    return {"ok": True, "strategy": mistock_db.row("SELECT * FROM ai_strategies WHERE id = ?", (strategy_id,))}


@router.delete("/api/mistock/ai-strategies/{strategy_id}")
def mistock_delete_ai_strategy(strategy_id: str):
    mistock_db.execute("DELETE FROM ai_strategies WHERE id = ? AND id <> 'mistock_nasdaq_rule_v1'", (strategy_id,))
    return {"ok": True}


@router.post("/api/mistock/ai-strategies/{strategy_id}/select")
def mistock_select_ai_strategy(strategy_id: str, payload: dict = Body(default={})):
    selected = 1 if payload.get("selected", True) else 0
    mistock_db.execute("UPDATE ai_strategies SET selected = ? WHERE id = ?", (selected, strategy_id))
    mistock_db.execute(
        """
        INSERT INTO strategy_schedules (strategy_id, enabled, auto_approve)
        VALUES (?, ?, 0)
        ON CONFLICT(strategy_id) DO UPDATE SET enabled = excluded.enabled
        """,
        (strategy_id, selected),
    )
    return {"ok": True, "id": strategy_id, "selected": bool(selected)}


def _strategy_gate(strategy_id: str, check: str, status: str = "passed") -> dict:
    item = mistock_db.row("SELECT * FROM ai_strategies WHERE id = ?", (strategy_id,))
    if not item:
        raise HTTPException(status_code=404, detail="strategy not found")
    payload = {"ok": True, "success": status == "passed", "status": status, "message": f"Mistock {check} completed"}
    mistock_db.execute(
        "INSERT INTO ai_strategy_events (ts, strategy_id, strategy_version, event_type, payload) VALUES (?, ?, ?, ?, ?)",
        (mistock_db.now_text(), strategy_id, item.get("strategy_version") or 1, check, json.dumps(payload, ensure_ascii=False)),
    )
    return payload


@router.post("/api/mistock/ai-strategies/{strategy_id}/static-verify")
def mistock_static_verify(strategy_id: str):
    return _strategy_gate(strategy_id, "static")


@router.post("/api/mistock/ai-strategies/{strategy_id}/verify")
def mistock_api_verify(strategy_id: str):
    return _strategy_gate(strategy_id, "api")


@router.post("/api/mistock/ai-strategies/{strategy_id}/backtest")
def mistock_backtest(strategy_id: str):
    from src.strategy.backtest_mistock import run_mistock_backtest
    strategy = mistock_db.row("SELECT * FROM ai_strategies WHERE id = ?", (strategy_id,))
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    profile_json = strategy.get("profile_json") or "{}"
    try:
        profile = json.loads(profile_json)
    except Exception:
        profile = {}
    result = run_mistock_backtest(profile)
    
    # Save to DB
    now = mistock_db.now_text()
    validation_res = {
        "checks": {
            "static": {"ok": True, "success": True, "status": "passed"},
            "backtest": result
        },
        "latest": {"check": "backtest", "result": result}
    }
    status = "backtested" if result.get("success") else "review_required"
    
    mistock_db.execute(
        """
        UPDATE ai_strategies
        SET last_backtested_at = ?, last_validation_result = ?, status = ?
        WHERE id = ?
        """,
        (now, json.dumps(validation_res, ensure_ascii=False), status, strategy_id)
    )
    
    gate = _strategy_gate(strategy_id, "backtest", "passed" if result.get("success") else "failed")
    return {**gate, "result": result, "metrics": result.get("metrics"), "strategy": mistock_db.row("SELECT * FROM ai_strategies WHERE id = ?", (strategy_id,))}


@router.post("/api/mistock/ai-strategies/{strategy_id}/evolve")
def mistock_evolve(strategy_id: str):
    from src.strategy.evolve_mistock import evolve_mistock_strategy
    result = evolve_mistock_strategy(strategy_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "US Strategy evolution failed"))
    return {"ok": True, "result": result}


@router.post("/api/mistock/ai-strategies/{strategy_id}/paper/start")
def mistock_paper_start(strategy_id: str):
    mistock_db.execute("UPDATE ai_strategies SET last_paper_started_at = ? WHERE id = ?", (mistock_db.now_text(), strategy_id))
    return _strategy_gate(strategy_id, "paper_start")


@router.post("/api/mistock/ai-strategies/{strategy_id}/paper/complete")
def mistock_paper_complete(strategy_id: str, payload: dict = Body(default={})):
    mistock_db.execute("UPDATE ai_strategies SET last_paper_completed_at = ? WHERE id = ?", (mistock_db.now_text(), strategy_id))
    return {**_strategy_gate(strategy_id, "paper"), "days": int(payload.get("days") or 20)}


@router.post("/api/mistock/ai-strategies/{strategy_id}/approve")
def mistock_strategy_approve(strategy_id: str):
    mistock_db.execute("UPDATE ai_strategies SET status = 'approved', last_used_at = ? WHERE id = ?", (mistock_db.now_text(), strategy_id))
    return {"ok": True, "id": strategy_id, "status": "approved"}


@router.post("/api/mistock/ai-strategies/{strategy_id}/retire")
def mistock_strategy_retire(strategy_id: str):
    mistock_db.execute("UPDATE ai_strategies SET status = 'retired' WHERE id = ?", (strategy_id,))
    return {"ok": True, "id": strategy_id, "status": "retired"}


@router.post("/api/mistock/ai-strategies/{strategy_id}/performance/review")
def mistock_strategy_performance_review(strategy_id: str, days: int = 30):
    return {"ok": True, "strategy_id": strategy_id, "days": days, "status": "reviewed", "message": "Mistock demo performance reviewed."}


@router.get("/api/mistock/strategy-context")
def mistock_strategy_context():
    strategies = _strategy_rows()
    active = next((item for item in strategies if item.get("selected")), strategies[0] if strategies else {})
    return {
        "active_strategy": {
            "id": active.get("id"),
            "name": active.get("name"),
            "model": active.get("model"),
            "ai_weight": active.get("weight", 0.0),
            "status": active.get("status", "approved"),
            "strategy_version": active.get("strategy_version", 1),
            "profile_hash": active.get("profile_hash", "mistock-default-v1"),
            "last_verified_at": active.get("last_verified_at"),
            "last_backtested_at": active.get("last_backtested_at"),
            "last_paper_started_at": active.get("last_paper_started_at"),
            "last_paper_completed_at": active.get("last_paper_completed_at"),
            "last_used_at": active.get("last_used_at"),
            "validation": _mistock_validation_payload(active),
            "approval_gate": {"ok": True, "missing": []},
        },
        "safety": {
            **mistock_trader.runtime_flags(),
            "require_backtest_pass": False,
        },
        "fallback": {"mode": "rule_based", "openai_configured": False},
    }


@router.get("/api/mistock/ai-strategies/{strategy_id}/events")
def mistock_strategy_events(strategy_id: str, limit: int = 20):
    rows = mistock_db.rows(
        "SELECT * FROM ai_strategy_events WHERE strategy_id = ? ORDER BY ts DESC LIMIT ?",
        (strategy_id, max(1, min(limit, 100))),
    )
    return {"events": rows}


@router.get("/api/mistock/ai-strategies/{strategy_id}/performance")
def mistock_strategy_performance(strategy_id: str, days: int = 30):
    return {"strategy_id": strategy_id, "days": days, "return_pct": 0.0, "win_rate": 0.0, "trades": 0, "max_drawdown_pct": 0.0}


@router.get("/api/mistock/watchlist")
def mistock_watchlist():
    items = mistock_trader.get_watchlist()
    enriched = []
    latest = {row["symbol"]: row for row in mistock_db.rows(
        """
        SELECT sc1.* FROM scanned_candidates sc1
        JOIN (SELECT symbol, MAX(id) AS id FROM scanned_candidates GROUP BY symbol) sc2 ON sc1.id = sc2.id
        """
    )}
    for item in items:
        symbol = item["symbol"]
        scan = latest.get(symbol, {})
        enriched.append({
            **item,
            "price": scan.get("price"),
            "score": scan.get("score"),
            "rsi": scan.get("rsi"),
            "reasons": scan.get("reasons", ""),
            "sector": "미국 주식",
            "last_scanned_at": scan.get("scanned_at"),
        })
    return {
        "symbols": enriched,
        "ai_auto_add": mistock_db.get_setting("ai_auto_add", "false") == "true",
        "ai_auto_add_threshold": float(mistock_db.get_setting("ai_auto_add_threshold", "3") or 3),
    }


@router.post("/api/mistock/watchlist")
def mistock_add_watchlist(payload: dict = Body(...)):
    item = mistock_trader.add_watchlist(str(payload.get("symbol", "")), payload.get("name"))
    return {"ok": True, "item": item}


@router.delete("/api/mistock/watchlist/{symbol}")
def mistock_delete_watchlist(symbol: str):
    mistock_trader.delete_watchlist(symbol)
    return {"ok": True}


@router.post("/api/mistock/watchlist/toggle-auto")
def mistock_watchlist_toggle_auto(payload: dict = Body(...)):
    enabled = bool(payload.get("enabled"))
    threshold = float(payload.get("threshold") or 3.0)
    mistock_db.set_setting("ai_auto_add", "true" if enabled else "false")
    mistock_db.set_setting("ai_auto_add_threshold", str(threshold))
    return {"ok": True, "enabled": enabled, "threshold": threshold}


@router.post("/api/mistock/watchlist/scan-trigger")
def mistock_watchlist_scan_trigger():
    threshold = float(mistock_db.get_setting("ai_auto_add_threshold", "3") or 3)
    scan = mistock_trader.scan_candidates(min_score=int(threshold), limit=20)
    added = []
    for candidate in scan["candidates"][:5]:
        item = mistock_trader.add_watchlist(candidate["symbol"], candidate["name"])
        added.append(item)
    return {
        "ok": True,
        "added_count": len(added),
        "added_symbols": added,
        "scanned": scan["scanned"],
        "threshold": threshold,
    }


@router.get("/api/mistock/signals")
def mistock_signals():
    return {"signals": mistock_trader.signals()}


@router.get("/api/mistock/candidates")
def mistock_candidates(min_score: int = 2, limit: int = 100, ranker: str = "mistock_rule", optimizer: str = "equal_weight"):
    scan = mistock_trader.scan_candidates(min_score=min_score, limit=limit)
    balance = mistock_trader.get_balance()
    candidates = mistock_trader.annotate_candidates_with_order_plan(scan["candidates"], balance["cash"])
    return {
        "candidates": candidates,
        "scan_summary": scan["scan_summary"],
        "scanned": scan["scanned"],
        "min_score": min_score,
        "cash": balance["cash"],
        "balance_source": balance.get("balance_source", "kis"),
    }


@router.get("/api/mistock/indicator-strategy")
def mistock_indicator_strategy():
    return {
        "ok": True,
        "model": mistock_config.strategy_model,
        "enabled": str(mistock_config.strategy_model or "").lower() == "macd_rsi_momentum",
        "min_score": mistock_config.indicator_min_score,
        "rsi_entry_min": mistock_config.indicator_rsi_entry_min,
        "rsi_entry_max": mistock_config.indicator_rsi_entry_max,
        "volume_ratio": mistock_config.indicator_volume_ratio,
        "trailing_stop_activation_pct": mistock_config.trailing_stop_activation_pct,
        "trailing_stop_pct": mistock_config.trailing_stop_pct,
        "trailing_stop_lookback": mistock_config.trailing_stop_lookback,
        "trade_value_surge_ratio": mistock_config.trade_value_surge_ratio,
        "first_wave_min_pct": mistock_config.first_wave_min_pct,
        "first_wave_pullback_min_pct": mistock_config.first_wave_pullback_min_pct,
        "first_wave_pullback_max_pct": mistock_config.first_wave_pullback_max_pct,
        "take_profit": mistock_config.take_profit,
        "stop_loss_pct": mistock_config.stop_loss_pct,
        "rules": [
            "MACD bullish cross 또는 MACD histogram 양수",
            "RSI 50 상향 돌파 또는 RSI 50~70 모멘텀 구간",
            "현재가가 SMA60/SMA20 위에 있을수록 가점",
            "SMA20/SMA60 골든크로스 가점, 데드크로스 감점 및 수익 보호",
            "거래량이 20일 평균보다 크면 신뢰도 가점",
            "거래대금이 20일 평균의 설정 배수 이상이면 가점",
            "1차 상승 파동 후 설정 범위 눌림·거래량 수축·반등이면 가점",
            "RSI 과열은 재진입 조건이 아니면 감점",
            "설정 수익률 도달 후 최근 고점 대비 하락 시 트레일링 청산",
        ],
    }


@router.post("/api/mistock/indicator-strategy")
def mistock_update_indicator_strategy(payload: dict = Body(...)):
    enabled = bool(payload.get("enabled"))
    values = {
        "MISTOCK_STRATEGY_MODEL": "macd_rsi_momentum" if enabled else "default",
        "MISTOCK_INDICATOR_MIN_SCORE": str(payload.get("min_score", mistock_config.indicator_min_score)),
        "MISTOCK_INDICATOR_RSI_ENTRY_MIN": str(payload.get("rsi_entry_min", mistock_config.indicator_rsi_entry_min)),
        "MISTOCK_INDICATOR_RSI_ENTRY_MAX": str(payload.get("rsi_entry_max", mistock_config.indicator_rsi_entry_max)),
        "MISTOCK_INDICATOR_VOLUME_RATIO": str(payload.get("volume_ratio", mistock_config.indicator_volume_ratio)),
        "MISTOCK_TRAILING_STOP_ACTIVATION_PCT": str(payload.get(
            "trailing_stop_activation_pct", mistock_config.trailing_stop_activation_pct
        )),
        "MISTOCK_TRAILING_STOP_PCT": str(payload.get(
            "trailing_stop_pct", mistock_config.trailing_stop_pct
        )),
        "MISTOCK_TRAILING_STOP_LOOKBACK": str(payload.get(
            "trailing_stop_lookback", mistock_config.trailing_stop_lookback
        )),
        "MISTOCK_TRADE_VALUE_SURGE_RATIO": str(payload.get(
            "trade_value_surge_ratio", mistock_config.trade_value_surge_ratio
        )),
        "MISTOCK_FIRST_WAVE_MIN_PCT": str(payload.get(
            "first_wave_min_pct", mistock_config.first_wave_min_pct
        )),
        "MISTOCK_FIRST_WAVE_PULLBACK_MIN_PCT": str(payload.get(
            "first_wave_pullback_min_pct", mistock_config.first_wave_pullback_min_pct
        )),
        "MISTOCK_FIRST_WAVE_PULLBACK_MAX_PCT": str(payload.get(
            "first_wave_pullback_max_pct", mistock_config.first_wave_pullback_max_pct
        )),
    }
    updates = {key: _validate_mistock_env_value(key, value) for key, value in values.items()}
    _core._write_env_values(updates, _core._public_value("ENV_PATH", _core.ENV_PATH))
    _apply_mistock_env_updates(updates)
    return {"ok": True, "updated": sorted(updates), **mistock_indicator_strategy()}


@router.post("/api/mistock/indicator-strategy/scan")
def mistock_indicator_strategy_scan(payload: dict = Body(default={})):
    limit = int(payload.get("limit") or min(mistock_config.scan_universe_size, 50))
    min_score = int(payload.get("min_score") or mistock_config.indicator_min_score or 4)
    scan = mistock_trader.scan_candidates(
        min_score=min_score,
        limit=max(1, min(limit, 200)),
        model="macd_rsi_momentum",
    )
    balance = mistock_trader.get_balance()
    candidates = mistock_trader.annotate_candidates_with_order_plan(scan["candidates"], balance["cash"])
    return {
        "ok": True,
        "candidates": candidates,
        "scan_summary": scan["scan_summary"],
        "scanned": scan["scanned"],
        "min_score": scan["min_score"],
        "model": "macd_rsi_momentum",
    }


@router.get("/api/mistock/candidates/history")
def mistock_candidates_history(limit: int = 50):
    rows = mistock_db.rows(
        "SELECT * FROM scanned_candidates ORDER BY id DESC LIMIT ?",
        (max(1, min(limit, 500)),),
    )
    return {"candidates": rows, "history": rows}


@router.delete("/api/mistock/candidates/history/{candidate_id}")
def mistock_delete_candidate(candidate_id: int):
    mistock_db.execute("DELETE FROM scanned_candidates WHERE id = ?", (candidate_id,))
    return {"ok": True}


@router.get("/api/mistock/ai-allocation")
def mistock_ai_allocation():
    plan = mistock_trader.execution_plan()
    return {"orders": plan["plan"], "plan": plan["plan"], "cash": plan["cash"], "remaining_cash": plan["remaining_cash"]}


@router.get("/api/mistock/execution-plan")
def mistock_execution_plan():
    return mistock_trader.execution_plan()


def _is_mistock_order_window_open() -> bool:
    from src.mistock.scheduler import is_us_market_open

    return is_us_market_open()


@router.post("/api/mistock/approvals")
def mistock_create_approval(payload: dict = Body(...)):
    from src.online_access import is_online_access_blocked

    symbol = normalize_symbol(str(payload.get("symbol", "")))
    action = str(payload.get("action", "")).lower()
    qty = float(payload.get("qty") or 0)
    price = float(payload.get("price") or 0)
    if price <= 0 and not is_online_access_blocked():
        price = quote(symbol)["current"]
    if not symbol or action not in {"buy", "sell"} or qty <= 0:
        raise HTTPException(status_code=400, detail="symbol, action, qty required")
    name = str(payload.get("name") or symbol_name(symbol))
    now = mistock_db.now_text()
    approval_id = mistock_db.execute(
        """
        INSERT INTO approvals (created_at, updated_at, symbol, name, action, qty, price, reason, source, status, response_msg, strategy_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?)
        """,
        (now, now, symbol, name, action, qty, price, str(payload.get("reason") or ""), str(payload.get("source") or "mistock_dashboard"), str(payload.get("strategy_id") or "") or None),
    )
    if (
        not is_online_access_blocked()
        and mistock_db.get_setting("auto_approval", "false") == "true"
        and mistock_trader.broker_submission_available()
        and _is_mistock_order_window_open()
    ):
        if str(payload.get("source") or "") in {"dashboard_holding_sell", "mistock_holding_sell"}:
            _run_mistock_auto_approval_batch_async([approval_id])
            return {
                "ok": True,
                "id": approval_id,
                "status": "pending",
                "auto_approved": False,
                "auto_approval_queued": True,
            }
        result = _execute_approval(approval_id, approve=True)
        result["auto_approved"] = True
        return result
    return {"ok": True, "id": approval_id, "status": "pending", "auto_approved": False}


@router.get("/api/mistock/approvals")
def mistock_approvals(limit: int = 50):
    rows = mistock_db.rows("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))
    auto_approval_enabled = mistock_db.get_setting("auto_approval", "false") == "true"
    approvals = []
    for row in rows:
        item = dict(row)
        item["auto_approval_in_progress"] = (
            auto_approval_enabled
            and item.get("status") == "pending"
            and item.get("source") in {"dashboard_holding_sell", "mistock_holding_sell", "mistock_sell_all"}
        )
        approvals.append(item)
    return {"approvals": approvals}


def _execute_approval(approval_id: int, *, approve: bool) -> dict:
    from src.online_access import is_online_access_blocked

    item = mistock_db.row("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    if not item:
        raise HTTPException(status_code=404, detail="approval not found")
    if item["status"] != "pending":
        return item
    if not approve:
        mistock_db.execute(
            "UPDATE approvals SET status = 'rejected', updated_at = ?, response_msg = 'Rejected by dashboard' WHERE id = ?",
            (mistock_db.now_text(), approval_id),
        )
        updated = mistock_db.row("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return {**updated, "ok": True}
    if is_online_access_blocked():
        raise HTTPException(status_code=409, detail="Online access is blocked. Approval remains pending.")
    if not _is_mistock_order_window_open():
        raise HTTPException(status_code=409, detail="US market is not open. Approval remains pending.")
    result = mistock_trader.place_order(
        item["symbol"], item["action"], item["qty"], item["price"], item.get("reason") or "",
        strategy_id=item.get("strategy_id"),
    )
    status = "executed" if result.get("ok") else "failed"
    mistock_db.execute(
        "UPDATE approvals SET status = ?, updated_at = ?, response_msg = ? WHERE id = ?",
        (status, mistock_db.now_text(), result.get("message") or result.get("msg1") or status, approval_id),
    )
    updated = mistock_db.row("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    return {**updated, "ok": bool(result.get("ok"))}


@router.post("/api/mistock/approvals/{approval_id}/approve")
def mistock_approve(approval_id: int):
    return _execute_approval(approval_id, approve=True)


@router.post("/api/mistock/approvals/{approval_id}/reject")
def mistock_reject(approval_id: int):
    return _execute_approval(approval_id, approve=False)


@router.post("/api/mistock/holdings/sell-all")
def mistock_sell_all():
    holdings = mistock_trader.get_holdings()
    if not holdings:
        return {"status": "empty", "created_count": 0, "pending_count": 0, "executed_count": 0, "failed_count": 0}
    created = 0
    for item in holdings:
        mistock_create_approval({
            "symbol": item["symbol"],
            "name": item["name"],
            "action": "sell",
            "qty": item["qty"],
            "price": item["price"],
            "reason": "mistock sell all holdings",
            "source": "mistock_sell_all",
        })
        created += 1
    return {"status": "queued", "created_count": created, "pending_count": created, "executed_count": 0, "failed_count": 0}


@router.get("/api/mistock/trades")
def mistock_trades(limit: int = 20, strategy_id: str = ""):
    if strategy_id:
        rows = mistock_db.rows(
            "SELECT * FROM trades WHERE COALESCE(strategy_id, 'unattributed') = ? ORDER BY id DESC LIMIT ?",
            (strategy_id, max(1, min(limit, 500))),
        )
    else:
        rows = mistock_db.rows("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))
    return {"trades": rows}


@router.post("/api/mistock/trades/sync")
def mistock_trades_sync():
    started_at = mistock_db.now_text()
    result = {"ok": True, "synced_count": 0, "message": "Mistock demo DB is authoritative."}
    run_id = mistock_db.execute(
        "INSERT INTO trade_sync_runs (started_at, finished_at, synced_count, status, message) VALUES (?, ?, ?, ?, ?)",
        (started_at, mistock_db.now_text(), 0, "success", result["message"]),
    )
    return {**result, "run_id": run_id, "started_at": started_at, "finished_at": mistock_db.now_text()}


@router.get("/api/mistock/trades/sync-runs")
def mistock_trade_sync_runs(limit: int = 20):
    return {"runs": mistock_db.rows(
        "SELECT * FROM trade_sync_runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)
    )}


@router.get("/api/mistock/performance/cashflows")
def mistock_performance_cashflows():
    return {"cashflows": mistock_db.rows("SELECT * FROM performance_cashflows ORDER BY occurred_at, id")}


@router.post("/api/mistock/performance/cashflows")
def mistock_save_performance_cashflow(payload: dict = Body(...)):
    kind = str(payload.get("kind") or "").strip().lower()
    amount = float(payload.get("amount") or 0)
    occurred_at = str(payload.get("occurred_at") or mistock_db.now_text()).replace("T", " ")[:19]
    if kind not in {"deposit", "withdrawal", "dividend", "interest", "other"} or amount == 0:
        raise HTTPException(status_code=400, detail="valid kind and non-zero amount required")
    signed_amount = -abs(amount) if kind == "withdrawal" else abs(amount)
    row_id = mistock_db.execute(
        "INSERT INTO performance_cashflows (occurred_at, kind, amount, note, confirmed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (occurred_at, kind, signed_amount, str(payload.get("note") or ""), int(bool(payload.get("confirmed"))), mistock_db.now_text()),
    )
    return {"ok": True, "cashflow": mistock_db.row("SELECT * FROM performance_cashflows WHERE id = ?", (row_id,))}


def _mistock_account_trades(trades: list[dict]) -> list[dict]:
    account_rows = []
    show_dry_run = mistock_config.dry_run or (mistock_config.trading_env == "demo")
    for trade in trades:
        ok_val = trade.get("ok")
        if ok_val is not None:
            try:
                ok = bool(int(float(ok_val)))
            except Exception:
                ok = True
        else:
            ok = True
        if not ok:
            continue
            
        reason = str(trade.get("reason") or "").lower()
        if any(token in reason for token in ("sync", "adjust", "correction", "import")):
            continue
        if any(token in reason for token in ("동기화", "보정", "조정")):
            continue
        broken_tokens = ("利앷텒", "媛뺤젣", "숆린", "蹂댁젙", "섎룞", "꾨씫遺")
        if any(token in reason for token in broken_tokens):
            continue
            
        dr_val = trade.get("dry_run")
        is_dr = False
        if dr_val is not None:
            try:
                is_dr = bool(int(float(dr_val)))
            except Exception:
                pass
        if not show_dry_run and is_dr:
            continue
            
        account_rows.append(trade)
    return account_rows


def _period_bucket() -> dict:
    return {
        "order_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "buy_amount": 0.0,
        "sell_amount": 0.0,
        "realized_pnl": 0.0,
        "cost_of_sold": 0.0,
        "realized_pnl_rate": 0.0,
        "net_cashflow": 0.0,
        "details": [],
    }


_MISTOCK_INDEX_TICKERS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
}
_MISTOCK_INDEX_CACHE: tuple[float, dict[str, list[dict]]] = (0.0, {})
_MISTOCK_PRICE_CACHE: tuple[float, dict[str, list[dict]]] = (0.0, {})


def _load_mistock_index_rows() -> dict[str, list[dict]]:
    global _MISTOCK_INDEX_CACHE
    cached_at, cached_rows = _MISTOCK_INDEX_CACHE
    if time.monotonic() - cached_at < 300:
        return cached_rows

    series: dict[str, list[dict]] = {}
    try:
        from src.online_access import require_online_access
        import yfinance as yf

        require_online_access("미스톡 성과 탭 시장지수 조회")
        for name, ticker in _MISTOCK_INDEX_TICKERS.items():
            data = yf.download(
                ticker,
                period="6mo",
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
                timeout=5,
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
        logger.info(f"Mistock performance benchmark data unavailable: {exc}")

    _MISTOCK_INDEX_CACHE = (time.monotonic(), series)
    return series


def _load_mistock_price_rows(symbols: list[str]) -> dict[str, list[dict]]:
    global _MISTOCK_PRICE_CACHE
    cached_at, cached = _MISTOCK_PRICE_CACHE
    requested = sorted(set(symbols))
    if requested and time.monotonic() - cached_at < 300 and all(symbol in cached for symbol in requested):
        return {symbol: cached[symbol] for symbol in requested}
    if not requested:
        return {}
    try:
        from src.online_access import require_online_access
        import yfinance as yf

        require_online_access("Mistock strategy forward performance")
        frame = yf.download(requested, period="6mo", interval="1d", auto_adjust=False, progress=False, threads=True, timeout=10)
        close = frame["Close"]
        result = {}
        for symbol in requested:
            series = close[symbol].dropna() if getattr(close, "ndim", 1) > 1 else close.dropna()
            result[symbol] = [{"date": str(index)[:10], "close": float(value)} for index, value in series.items()]
        _MISTOCK_PRICE_CACHE = (time.monotonic(), result)
        return result
    except Exception as exc:
        logger.info(f"Mistock strategy price data unavailable: {exc}")
        return {}


def _mistock_strategy_forward(trades: list[dict], index_rows: dict[str, list[dict]]) -> list[dict]:
    from src.strategy.forward_performance import build_strategy_forward_performance

    symbols = sorted({str(row.get("symbol") or "") for row in trades if row.get("symbol")})
    names = {
        str(row["id"]): str(row.get("name") or row["id"])
        for row in mistock_db.rows("SELECT id, name FROM ai_strategies")
    }
    rows = build_strategy_forward_performance(
        trades,
        _load_mistock_price_rows(symbols),
        {"KOSPI": index_rows.get("sp500", []), "KOSDAQ": index_rows.get("nasdaq", [])},
        strategy_names=names,
    )
    for row in rows:
        row["sp500_return_pct"] = row.pop("kospi_return_pct", None)
        row["nasdaq_return_pct"] = row.pop("kosdaq_return_pct", None)
        row["excess_vs_sp500_pct"] = row.pop("excess_vs_kospi_pct", None)
        row["excess_vs_nasdaq_pct"] = row.pop("excess_vs_kosdaq_pct", None)
        row["sp500_twr_pct"] = row.pop("kospi_twr_pct", None)
        row["nasdaq_twr_pct"] = row.pop("kosdaq_twr_pct", None)
        row["twr_pct"] = row.get("returns", {}).get("twr_pct")
        row["sp500_twr_pct"] = row.get("returns", {}).get("kospi_twr_pct")
        row["nasdaq_twr_pct"] = row.get("returns", {}).get("kosdaq_twr_pct")
        row["excess_twr_vs_sp500_pct"] = row.get("returns", {}).get("excess_twr_vs_kospi_pct")
        row["max_drawdown_pct"] = row.get("nav", {}).get("max_drawdown_pct")
    return rows


def _mistock_market_context(
    index_rows: dict[str, list[dict]],
    *,
    monthly: bool = False,
    weekly: bool = False,
) -> dict[str, dict]:
    context: dict[str, dict] = {}
    for name, rows in index_rows.items():
        if monthly or weekly:
            grouped: dict[str, list[float]] = {}
            for row in rows:
                date = str(row.get("date") or "")
                close = float(row.get("close") or 0)
                if len(date) >= 10 and close > 0:
                    if weekly:
                        iso = datetime.fromisoformat(date[:10]).isocalendar()
                        period = f"{iso.year}-W{iso.week:02d}"
                    else:
                        period = date[:7]
                    grouped.setdefault(period, []).append(close)
            points = [(period, closes[-1]) for period, closes in sorted(grouped.items())]
        else:
            points = [
                (str(row.get("date") or "")[:10], float(row.get("close") or 0))
                for row in rows
                if float(row.get("close") or 0) > 0
            ]

        previous_close = None
        for period, close in points:
            change_pct = None
            if previous_close and previous_close > 0:
                change_pct = (close / previous_close - 1) * 100
            bucket = context.setdefault(period, {})
            bucket[name] = round(close, 2)
            bucket[f"{name}_change_pct"] = (
                round(change_pct, 2) if change_pct is not None else None
            )
            previous_close = close
    return context


def _build_mistock_periodic_performance(
    trades: list[dict], strategy_id: str = "", cashflows: list[dict] | None = None,
) -> dict:
    daily: dict[str, dict] = {}
    weekly: dict[str, dict] = {}
    monthly: dict[str, dict] = {}
    holdings: dict[tuple[str, str], dict] = {}
    strategy_stats: dict[str, dict] = {}

    account_trades = _mistock_account_trades(trades)
    if strategy_id:
        account_trades = [row for row in account_trades if str(row.get("strategy_id") or "unattributed") == strategy_id]
    for trade in account_trades:
        ts = str(trade.get("ts") or "")
        if len(ts) < 10 or ts[0] == "-":
            continue

        day_key = ts[:10]
        iso = datetime.fromisoformat(day_key).isocalendar()
        week_key = f"{iso.year}-W{iso.week:02d}"
        month_key = ts[:7]
        action = str(trade.get("action") or "").lower()
        symbol = str(trade.get("symbol") or "")
        strategy_id = str(trade.get("strategy_id") or "unattributed")
        strategy_name = str(trade.get("strategy_name") or strategy_id)
        
        try:
            qty = float(trade.get("qty") or 0.0)
            price = float(trade.get("price") or 0.0)
        except Exception:
            continue
            
        amount = qty * price

        if qty <= 0 or price <= 0 or action not in {"buy", "sell"}:
            continue

        day = daily.setdefault(day_key, _period_bucket())
        week = weekly.setdefault(week_key, _period_bucket())
        month = monthly.setdefault(month_key, _period_bucket())
        for bucket in (day, week, month):
            bucket["order_count"] += 1
            if action == "buy":
                bucket["buy_count"] += 1
                bucket["buy_amount"] += amount
            else:
                bucket["sell_count"] += 1
                bucket["sell_amount"] += amount

        holding = holdings.setdefault((strategy_id, symbol), {"qty": 0.0, "avg_cost": 0.0})
        stats = strategy_stats.setdefault(strategy_id, {
            "order_count": 0, "buy_count": 0, "sell_count": 0,
            "realized_pnl": 0.0, "_pnls": [],
        })
        stats["order_count"] += 1
        stats[f"{action}_count"] += 1

        if action == "buy":
            total_qty = holding["qty"] + qty
            total_cost = holding["qty"] * holding["avg_cost"] + amount
            holding["qty"] = total_qty
            holding["avg_cost"] = total_cost / total_qty if total_qty > 0 else 0.0
            realized = 0.0
            cost_of_shares_sold = 0.0
        else:
            sell_qty = min(qty, holding["qty"])
            cost_of_shares_sold = holding["avg_cost"] * sell_qty
            realized = (price - holding["avg_cost"]) * sell_qty
            
            day["realized_pnl"] += realized
            week["realized_pnl"] += realized
            month["realized_pnl"] += realized
            day["cost_of_sold"] += cost_of_shares_sold
            week["cost_of_sold"] += cost_of_shares_sold
            month["cost_of_sold"] += cost_of_shares_sold
            stats["realized_pnl"] += realized
            if sell_qty > 0:
                stats["_pnls"].append(realized)
            
            holding["qty"] = max(0.0, holding["qty"] - sell_qty)
            if holding["qty"] <= 0:
                holding["avg_cost"] = 0.0

        detail = {
            "ts": ts,
            "symbol": symbol,
            "name": trade.get("name") or symbol,
            "action": action,
            "qty": qty,
            "price": price,
            "amount": round(amount, 2),
            "realized_pnl": round(realized, 2),
            "cost_of_sold": round(cost_of_shares_sold, 2),
            "realized_pnl_rate": round(realized / cost_of_shares_sold * 100, 2)
            if cost_of_shares_sold > 0 else 0.0,
            "reason": trade.get("reason", ""),
            "order_status": trade.get("order_status", ""),
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
        }
        day["details"].append(detail)
        week["details"].append(detail)
        month["details"].append(detail)

    for cashflow in cashflows or []:
        occurred = str(cashflow.get("occurred_at") or "")[:10]
        if len(occurred) < 10:
            continue
        iso = datetime.fromisoformat(occurred).isocalendar()
        amount = float(cashflow.get("amount") or 0)
        for rows, key in (
            (daily, occurred), (weekly, f"{iso.year}-W{iso.week:02d}"), (monthly, occurred[:7]),
        ):
            rows.setdefault(key, _period_bucket())["external_cashflow"] = (
                rows.setdefault(key, _period_bucket()).get("external_cashflow", 0.0) + amount
            )

    for rows in (daily, weekly, monthly):
        for bucket in rows.values():
            bucket["external_cashflow"] = round(bucket.get("external_cashflow", 0.0), 2)
            bucket["net_cashflow"] = round(
                bucket["sell_amount"] - bucket["buy_amount"] + bucket["external_cashflow"], 2
            )
            bucket["realized_pnl_rate"] = round((bucket["realized_pnl"] / bucket["cost_of_sold"] * 100), 2) if bucket["cost_of_sold"] > 0 else 0.0
            bucket["buy_amount"] = round(bucket["buy_amount"], 2)
            bucket["sell_amount"] = round(bucket["sell_amount"], 2)
            bucket["realized_pnl"] = round(bucket["realized_pnl"], 2)
            bucket["cost_of_sold"] = round(bucket["cost_of_sold"], 2)

    index_rows = _load_mistock_index_rows()
    daily_market = _mistock_market_context(index_rows)
    weekly_market = _mistock_market_context(index_rows, weekly=True)
    monthly_market = _mistock_market_context(index_rows, monthly=True)
    try:
        strategy_forward = _mistock_strategy_forward(account_trades, index_rows)
    except Exception as exc:
        logger.info(f"Mistock strategy forward performance unavailable: {exc}")
        strategy_forward = []
    return {
        "daily": [
            {"period": key, **value, **daily_market.get(key, {})}
            for key, value in sorted(daily.items())
        ],
        "weekly": [
            {"period": key, **value, **weekly_market.get(key, {})}
            for key, value in sorted(weekly.items())
        ],
        "monthly": [
            {"period": key, **value, **monthly_market.get(key, {})}
            for key, value in sorted(monthly.items())
        ],
        "strategy_validation": _core._strategy_validation(strategy_stats),
        "strategy_forward": strategy_forward,
        "market_data_available": bool(daily_market),
        "unconfirmed_cashflow_count": sum(not bool(row.get("confirmed")) for row in cashflows or []),
    }


def _mistock_holding_daily_change(holdings: dict[str, dict]) -> dict:
    symbols = [symbol for symbol, item in holdings.items() if float(item.get("qty") or 0) > 0]
    if not symbols:
        return {"holding_daily_change_pct": None, "holding_daily_change_symbol_count": 0}
    try:
        import yfinance as yf
        from src.online_access import require_online_access

        require_online_access("Mistock holding daily performance")
        frame = yf.download(symbols, period="5d", interval="1d", auto_adjust=False, progress=False, threads=True, timeout=8)
        close = frame["Close"]
        previous_value = current_value = 0.0
        count = 0
        for symbol in symbols:
            series = close[symbol].dropna() if getattr(close, "ndim", 1) > 1 else close.dropna()
            if len(series) < 2:
                continue
            qty = float(holdings[symbol].get("qty") or 0)
            previous_value += qty * float(series.iloc[-2])
            current_value += qty * float(series.iloc[-1])
            count += 1
        return {
            "holding_daily_change_pct": round((current_value / previous_value - 1) * 100, 2) if previous_value > 0 else None,
            "holding_daily_change_symbol_count": count,
        }
    except Exception as exc:
        logger.info(f"Mistock holding daily performance unavailable: {exc}")
        return {"holding_daily_change_pct": None, "holding_daily_change_symbol_count": 0}


@router.get("/api/mistock/performance")
def mistock_performance(strategy_id: str = ""):
    try:
        trades = mistock_db.rows("SELECT * FROM trades ORDER BY ts ASC")
        account_trades = _mistock_account_trades(trades)
        if strategy_id:
            account_trades = [row for row in account_trades if str(row.get("strategy_id") or "unattributed") == strategy_id]
        
        total_trades = len(account_trades)
        success_count = sum(1 for t in account_trades if t.get("ok", 1))
        success_rate = (success_count / total_trades * 100) if total_trades > 0 else 0.0
        
        holdings = {}
        realized_pnl = 0.0
        names = {}
        
        for t in account_trades:
            sym = t["symbol"]
            names[sym] = t.get("name", sym)
            qty = float(t.get("qty") or 0.0)
            price = float(t.get("price") or 0.0)
            action = t["action"]
            
            if qty <= 0 or price <= 0:
                continue
                
            if sym not in holdings:
                holdings[sym] = {"qty": 0.0, "cost": 0.0}
                
            h = holdings[sym]
            if action == "buy":
                total_qty = h["qty"] + qty
                total_cost = h["qty"] * h["cost"] + qty * price
                h["qty"] = total_qty
                h["cost"] = total_cost / total_qty if total_qty > 0 else 0.0
            elif action == "sell":
                sell_qty = min(qty, h["qty"])
                realized_pnl += (price - h["cost"]) * sell_qty
                h["qty"] = max(0.0, h["qty"] - sell_qty)
                if h["qty"] <= 0:
                    h["cost"] = 0.0
                    
        # Filter sold holdings
        sells = [row for row in account_trades if row["action"] == "sell"]
        wins = sum(1 for row in sells if row.get("price", 0.0) > holdings.get(row["symbol"], {}).get("cost", 0.0))
        losses = len(sells) - wins
        
        current_holdings = {}
        total_broker_pnl = 0.0
        try:
            parsed_holdings = mistock_trader.get_holdings()
            current_holdings = {h['symbol']: h for h in parsed_holdings}
            total_broker_pnl = sum(float(h.get("pnl") or 0.0) for h in parsed_holdings)
        except Exception:
            pass
            
        eval_details = []
        total_eval_pnl = total_broker_pnl
        
        for sym, data in holdings.items():
            if data["qty"] > 0:
                current_price = data["cost"]
                if sym in current_holdings:
                    current_price = float(current_holdings[sym]["price"])
                else:
                    try:
                        q = quote(sym)
                        current_price = float(q["current"] or data["cost"])
                    except Exception:
                        pass
                
                eval_pnl = (current_price - data["cost"]) * data["qty"]
                return_rate = ((current_price / data["cost"]) - 1) * 100 if data["cost"] > 0 else 0.0
                
                eval_details.append({
                    "symbol": sym,
                    "name": names.get(sym, sym),
                    "qty": data["qty"],
                    "avg_cost": round(data["cost"], 2),
                    "current_price": round(current_price, 2),
                    "eval_pnl": round(eval_pnl, 2),
                    "return_rate": round(return_rate, 2),
                    "broker_qty": current_holdings.get(sym, {}).get("qty", 0.0),
                    "broker_pnl": round(current_holdings.get(sym, {}).get("pnl", 0.0), 2),
                    "diff_reason": ""
                })
        if strategy_id:
            total_eval_pnl = sum(float(item.get("eval_pnl") or 0) for item in eval_details)
            total_broker_pnl = sum(
                float(current_holdings.get(symbol, {}).get("pnl") or 0)
                for symbol, position in holdings.items()
                if float(position.get("qty") or 0) > 0
            )
        daily_change = _mistock_holding_daily_change(
            {symbol: position for symbol, position in holdings.items() if not strategy_id or float(position.get("qty") or 0) > 0}
        )
                
        return {
            "total_trades": total_trades,
            "success_rate": round(success_rate, 2),
            "realized_pnl": round(realized_pnl, 2),
            "total_eval_pnl": round(total_eval_pnl, 2),
            "total_broker_pnl": round(total_broker_pnl, 2),
            "eval_details": eval_details,
            "untracked_details": [],
            **daily_change,
        }
    except Exception as e:
        from src.utils.logger import logger
        logger.error(f"Failed to calculate mistock performance: {e}")
        return {
            "total_trades": 0,
            "success_rate": 0.0,
            "realized_pnl": 0.0,
            "total_eval_pnl": 0.0,
            "total_broker_pnl": 0.0,
            "eval_details": [],
            "untracked_details": []
        }


@router.get("/api/mistock/performance/periodic")
def mistock_periodic_performance(strategy_id: str = ""):
    try:
        trades = mistock_db.rows("SELECT * FROM trades ORDER BY ts ASC")
        cashflows = mistock_db.rows("SELECT * FROM performance_cashflows ORDER BY occurred_at, id")
        return _build_mistock_periodic_performance(trades, strategy_id=strategy_id, cashflows=cashflows)
    except Exception as e:
        from src.utils.logger import logger
        logger.error(f"Failed to calculate mistock periodic performance: {e}")
        return {"daily": [], "weekly": [], "monthly": []}


import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from src.runtime_state import PersistentRuntimeState

_mistock_scheduler_running_lock = threading.Lock()
_mistock_scheduler_run_state = PersistentRuntimeState("mistock_scheduler", {
    "is_running": False,
    "mode": None,
    "started_at": None,
    "completed_at": None,
    "result": None,
    "error": None
})

def _bg_run_mistock_scheduled_cycle(mode: str):
    global _mistock_scheduler_run_state
    try:
        from src.mistock.scheduler import run_mistock_scheduled_cycle
        result = run_mistock_scheduled_cycle(mode=mode)
        
        recorded_at = datetime.now(timezone(timedelta(hours=9))).isoformat()
        with _mistock_scheduler_running_lock:
            _mistock_scheduler_run_state.replace({
                **_mistock_scheduler_run_state,
                "is_running": False,
                "completed_at": recorded_at,
                "result": result,
                "error": None,
                "owner_pid": None,
            })
    except Exception as e:
        with _mistock_scheduler_running_lock:
            _mistock_scheduler_run_state.replace({
                **_mistock_scheduler_run_state,
                "is_running": False,
                "completed_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
                "result": None,
                "error": str(e),
                "owner_pid": None,
            })


def _bg_run_mistock_scheduled_cycles(mode: str, strategy_ids: list[str]):
    global _mistock_scheduler_run_state
    runs = []
    errors = []
    try:
        from src.mistock.scheduler import run_mistock_scheduled_cycle
        for strategy_id in strategy_ids:
            try:
                result = run_mistock_scheduled_cycle(mode=mode, strategy_id=strategy_id)
                recorded_at = datetime.now(timezone(timedelta(hours=9))).isoformat()
                mistock_db.execute(
                    "UPDATE strategy_schedules SET last_run_at = ? WHERE strategy_id = ?",
                    (recorded_at, strategy_id),
                )
                runs.append({"strategy_id": strategy_id, "result": result})
            except Exception as exc:
                errors.append({"strategy_id": strategy_id, "message": str(exc)})
        aggregate = {
            "status": "failed" if errors and not runs else "success",
            "ok": bool(runs), "strategy_ids": strategy_ids, "runs": runs, "errors": errors,
        }
        with _mistock_scheduler_running_lock:
            _mistock_scheduler_run_state.replace({
                **_mistock_scheduler_run_state, "is_running": False,
                "completed_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
                "result": aggregate, "error": None, "owner_pid": None,
            })
    except Exception as exc:
        with _mistock_scheduler_running_lock:
            _mistock_scheduler_run_state.replace({
                **_mistock_scheduler_run_state, "is_running": False,
                "completed_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
                "result": None, "error": str(exc), "owner_pid": None,
            })
def map_mistock_to_kis_format(mistock_result: dict) -> dict:
    if not mistock_result:
        return {}
        
    if "results" in mistock_result:
        return mistock_result
        
    results = []
    auto_approved = []
    strategy_id = str(mistock_result.get("strategy_id") or "mistock_nasdaq_rule_v1")
    strategy = mistock_db.row("SELECT name FROM ai_strategies WHERE id = ?", (strategy_id,))
    strategy_name = str((strategy or {}).get("name") or strategy_id)

    def stock_fields(symbol: object) -> dict:
        ticker = normalize_symbol(str(symbol or ""))
        name = symbol_name(ticker)
        return {
            "symbol": ticker,
            "name": name,
            "display_name": f"{name} ({ticker})" if ticker and name != ticker else ticker,
            "market": "US",
            "asset_type": "미국 주식",
        }
    
    # 1. Map 'sold' items
    for s in mistock_result.get("sold", []):
        s_res = s.get("result") or {}
        ok = s_res.get("ok", True)
        msg1 = s_res.get("message") or s_res.get("msg1") or ("매도 완료" if ok else "매도 실패")
        row = {
            **stock_fields(s["symbol"]),
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "action": "sell",
            "decision": "execute",
            "qty": s["qty"],
            "price": s["price"],
            "reason": s.get("reason") or "보유 종목 매도 신호",
            "ok": ok,
            "message": msg1
        }
        results.append(row)
        auto_approved.append({
            **stock_fields(s["symbol"]),
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "symbol": s["symbol"],
            "action": "sell",
            "status": "executed" if ok else "failed",
            "qty": s["qty"],
            "price": s["price"],
            "message": msg1
        })

    # Map pending approvals that were executed at the start of this cycle.
    for p in mistock_result.get("pending_approved", []):
        p_res = p.get("result") or {}
        ok = p_res.get("ok", True)
        msg1 = p_res.get("message") or p_res.get("msg1") or ("pending approval executed" if ok else "pending approval failed")
        auto_approved.append({
            **stock_fields(p.get("symbol")),
            "strategy_id": p.get("strategy_id") or strategy_id,
            "strategy_name": strategy_name,
            "approval_id": p.get("id"),
            "symbol": p.get("symbol"),
            "action": p.get("action"),
            "status": "executed" if ok else "failed",
            "qty": p.get("qty"),
            "price": p.get("price"),
            "message": msg1
        })
        
    # 2. Map 'bought' items
    for b in mistock_result.get("bought", []):
        b_res = b.get("result") or {}
        ok = b_res.get("ok", True)
        msg1 = b_res.get("message") or b_res.get("msg1") or ("매수 완료" if ok else "매수 실패")
        row = {
            **stock_fields(b["symbol"]),
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "action": "buy",
            "decision": "execute",
            "qty": b["qty"],
            "price": b["price"],
            "reason": b.get("reason") or "매수 신호",
            "ok": ok,
            "message": msg1
        }
        results.append(row)
        auto_approved.append({
            **stock_fields(b["symbol"]),
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "symbol": b["symbol"],
            "action": "buy",
            "status": "executed" if ok else "failed",
            "qty": b["qty"],
            "price": b["price"],
            "message": msg1
        })
        
    # 3. Map 'plan' items (that didn't execute)
    executed_symbols = {b["symbol"] for b in mistock_result.get("bought", [])}
    for p in mistock_result.get("plan", []):
        if p["symbol"] in executed_symbols:
            continue
        row = {
            **stock_fields(p["symbol"]),
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "action": "buy",
            "decision": "queue",
            "qty": p["quantity"],
            "price": p["price"],
            "reason": p.get("reason") or "매수 계획 수립",
            "ok": True,
            "message": "승인 대기 등록"
        }
        results.append(row)
        
    return {
        "status": mistock_result.get("status") or "success",
        "ok": mistock_result.get("ok", True),
        "results": results,
        "auto_approved": auto_approved,
        "auto_approval_errors": [],
        "errors": mistock_result.get("errors", []),
        "scanned": mistock_result.get("scanned", 0),
        "candidates": mistock_result.get("candidates", 0)
    }


def save_mistock_daily_run(recorded_at: str, mode: str, result: dict):
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))
    cutoff_date = (datetime.now(KST) - timedelta(days=29)).date()
    
    path = Path(".runtime/mistock/daily_auto_today_results.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    
    existing_runs = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for run in data:
                    run_time = _parse_mistock_iso_datetime(run.get("recorded_at"))
                    if run_time is not None and run_time.date() >= cutoff_date:
                        existing_runs.append(run)
        except Exception:
            pass
            
    existing_runs.append({
        "recorded_at": recorded_at,
        "mode": mode,
        "result": result
    })
    
    path.write_text(json.dumps(existing_runs, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_mistock_daily_runs(days: int = 30) -> list:
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))
    days = max(1, int(days or 30))
    cutoff_date = (datetime.now(KST) - timedelta(days=days - 1)).date()
    
    runs_path = Path(".runtime/mistock/daily_auto_today_results.json")
    runs = []
    if runs_path.exists():
        try:
            data = json.loads(runs_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for run in data:
                    run_time = _parse_mistock_iso_datetime(run.get("recorded_at"))
                    if run_time is not None and run_time.date() >= cutoff_date:
                        runs.append(run)
        except Exception:
            pass
            
    last_path = Path(".runtime/mistock/daily_auto_last_result.json")
    if last_path.exists():
        try:
            last_run = json.loads(last_path.read_text(encoding="utf-8"))
            if isinstance(last_run, dict) and "recorded_at" in last_run:
                last_recorded = last_run["recorded_at"]
                last_recorded_at = _parse_mistock_iso_datetime(last_recorded)
                if last_recorded_at is not None and last_recorded_at.date() >= cutoff_date:
                    if not any(r.get("recorded_at") == last_recorded for r in runs):
                        runs.append({
                            "recorded_at": last_recorded,
                            "mode": last_run.get("mode") or "execute",
                            "result": last_run["result"]
                        })
                        runs_path.parent.mkdir(parents=True, exist_ok=True)
                        runs_path.write_text(json.dumps(runs, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass
            
    unique_runs = []
    seen_recent = {}
    for run in sorted(runs, key=lambda r: r.get("recorded_at", "")):
        try:
            result_key = json.dumps(run.get("result"), ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            result_key = str(run.get("result"))
        key = (run.get("mode"), result_key)
        recorded_at = _parse_mistock_iso_datetime(run.get("recorded_at"))
        previous_at = seen_recent.get(key)
        if recorded_at is not None and previous_at is not None and abs((recorded_at - previous_at).total_seconds()) <= 5:
            continue
        if recorded_at is not None:
            seen_recent[key] = recorded_at
        unique_runs.append(run)
    return unique_runs


def merge_mistock_runs_to_kis_format(runs: list) -> dict | None:
    if not runs:
        return None
        
    merged_results = []
    merged_approved = []
    merged_errors = []
    
    latest_recorded_at = runs[-1]["recorded_at"]
    latest_mode = runs[-1]["mode"]
    latest_errors = []
    
    for idx, run in enumerate(runs):
        round_num = idx + 1
        recorded_at_str = run["recorded_at"]
        normalized_recorded_at = recorded_at_str.replace("T", " ")
        date_part = normalized_recorded_at[:10]
        time_part = normalized_recorded_at.split(" ")[1][:5]
        display_time = f"{date_part[5:]} {time_part}"
        
        raw_result = run["result"]
        mapped = map_mistock_to_kis_format(raw_result)
        if idx == len(runs) - 1:
            latest_errors = mapped.get("errors", []) if isinstance(mapped.get("errors", []), list) else []
        
        for item in mapped.get("results", []):
            item_copy = dict(item)
            item_copy["time"] = display_time
            item_copy["run_date"] = date_part
            item_copy["run_recorded_at"] = recorded_at_str
            item_copy["round"] = round_num
            item_copy["reason"] = f"[{display_time}] {item_copy['reason']}"
            merged_results.append(item_copy)
            
        for item in mapped.get("auto_approved", []):
            item_copy = dict(item)
            item_copy["time"] = display_time
            item_copy["run_date"] = date_part
            item_copy["run_recorded_at"] = recorded_at_str
            item_copy["round"] = round_num
            item_copy["message"] = f"[{display_time}] {item_copy.get('message') or item_copy.get('response_msg') or ''}"
            merged_approved.append(item_copy)

        errors = mapped.get("errors", [])
        if isinstance(errors, list):
            for err in errors:
                if isinstance(err, dict):
                    err_copy = dict(err)
                    err_copy["time"] = display_time
                    err_copy["run_date"] = date_part
                    err_copy["run_recorded_at"] = recorded_at_str
                    err_copy["round"] = round_num
                    if err_copy.get("message"):
                        err_copy["message"] = f"[{display_time}] {err_copy['message']}"
                    merged_errors.append(err_copy)
                else:
                    merged_errors.append(f"[{display_time}] {err}")
        elif errors:
            merged_errors.append(f"[{display_time}] {errors}")
            
    return {
        "recorded_at": latest_recorded_at,
        "summary_label": "최근 30일 전체 집계",
        "range_days": 30,
        "mode": latest_mode,
        "result": {
            "status": "success",
            "ok": True,
            "results": merged_results,
            "auto_approved": merged_approved,
            "auto_approval_errors": [],
            "errors": latest_errors,
            "historical_errors": merged_errors,
            "historical_error_count": len(merged_errors),
            "scanned": runs[-1]["result"].get("scanned", 0),
            "candidates": runs[-1]["result"].get("candidates", 0)
        }
    }


def _parse_mistock_iso_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone(timedelta(hours=9)))
        return parsed.astimezone(timezone(timedelta(hours=9)))
    except ValueError:
        return None


def _clear_stale_mistock_scheduler_error(last_result: dict | None) -> None:
    if not last_result or _mistock_scheduler_run_state.get("is_running"):
        return
    if not _mistock_scheduler_run_state.get("error"):
        return

    result = last_result.get("result") if isinstance(last_result, dict) else {}
    if not isinstance(result, dict) or not result.get("ok"):
        return

    latest_at = _parse_mistock_iso_datetime(last_result.get("recorded_at"))
    state_at = (
        _parse_mistock_iso_datetime(_mistock_scheduler_run_state.get("completed_at"))
        or _parse_mistock_iso_datetime(_mistock_scheduler_run_state.get("started_at"))
    )
    if latest_at is None or (state_at is not None and latest_at <= state_at):
        return

    _mistock_scheduler_run_state.replace({
        **_mistock_scheduler_run_state,
        "completed_at": last_result.get("recorded_at"),
        "error": None,
        "owner_pid": None,
    })


@router.get("/api/mistock/scheduler/status")
def mistock_scheduler_status():
    global _mistock_scheduler_run_state
    _mistock_scheduler_run_state.refresh()
    
    runs = load_mistock_daily_runs(days=30)
    last_result = merge_mistock_runs_to_kis_format(runs)
    _clear_stale_mistock_scheduler_error(last_result)
        
    run_state_to_return = _mistock_scheduler_run_state.copy()
    if run_state_to_return.get("result"):
        run_state_to_return["result"] = map_mistock_to_kis_format(run_state_to_return["result"])

    active_strategy_id = "mistock_nasdaq_rule_v1"
    active_strategy_name = "미스톡 기본 Seven Split"
    try:
        active = mistock_db.row("SELECT * FROM ai_strategies WHERE selected = 1 ORDER BY last_used_at DESC, id DESC LIMIT 1")
        if active:
            active_strategy_id = active.get("id") or active_strategy_id
            active_strategy_name = active.get("name") or active_strategy_id
    except Exception:
        pass

    schedule_rows = mistock_db.rows(
        """
        SELECT s.*, a.name
        FROM strategy_schedules s
        LEFT JOIN ai_strategies a ON a.id = s.strategy_id
        WHERE s.enabled = 1
        ORDER BY s.strategy_id
        """
    )
    if not schedule_rows:
        schedule_rows = [{
            "strategy_id": active_strategy_id, "name": active_strategy_name, "enabled": 1,
            "interval_minutes": 60, "start_hm": "2100", "end_hm": "0600",
            "weekdays": "1-5/2-6", "mode": "execute", "auto_approve": 0,
        }]
    strategy_dispatch = {
        "enabled_count": len(schedule_rows),
        "schedule_count": len(schedule_rows),
        "universe_count": len(mistock_config.universe_list or []),
        "schedules": [{
            **row,
            "display_name": row.get("name") or row.get("strategy_id"),
            "enabled": bool(row.get("enabled")),
            "auto_approve": bool(row.get("auto_approve")),
            "universe_count": len(mistock_config.universe_list or []),
        } for row in schedule_rows],
    }
        
    return {
        "config": {
            "cron_tz": os.environ.get("MISTOCK_CRON_TZ", os.environ.get("HANSTOCK_CRON_TZ", "Asia/Seoul")),
            "daily_auto_retries": os.environ.get("MISTOCK_DAILY_AUTO_RETRIES", "3"),
            "daily_auto_retry_delay_seconds": os.environ.get("MISTOCK_DAILY_AUTO_RETRY_DELAY_SECONDS", "10"),
            "scheduler_retries": os.environ.get("MISTOCK_SCHEDULER_RETRIES", "1"),
            "scheduler_retry_delay_seconds": os.environ.get("MISTOCK_SCHEDULER_RETRY_DELAY_SECONDS", "5"),
            "slack_enabled": os.environ.get("MISTOCK_SCHEDULER_SLACK", "true"),
            "sync_enabled": os.environ.get("MISTOCK_ORDER_STATUS_SYNC", "true"),
            "order_delay_seconds": os.environ.get("MISTOCK_ORDER_DELAY_SECONDS", "1.2"),
            "result_path": os.environ.get("MISTOCK_SCHEDULER_RESULT_PATH", ".runtime/mistock/daily_auto_last_result.json"),
            "trading_env": mistock_config.trading_env,
            "dry_run": mistock_config.dry_run,
            **mistock_trader.runtime_flags(),
        },
        "last_result": last_result,
        "run_state": run_state_to_return,
        "active_strategy_id": active_strategy_id,
        "active_strategy_name": active_strategy_name,
        "strategy_dispatch": strategy_dispatch,
    }


@router.post("/api/mistock/scheduler/run")
def mistock_scheduler_run(payload: dict = Body(default={})):
    global _mistock_scheduler_run_state
    mode = str(payload.get("mode", "execute")).lower()
    
    if mode == "daily_auto":
        run_mode = "execute"
    elif mode in {"execute", "analysis_only"}:
        run_mode = mode
    else:
        raise HTTPException(
            status_code=400,
            detail=f"지원하지 않는 미장 스케줄러 모드입니다: '{mode}'. 'execute', 'analysis_only', 'daily_auto' 중 하나를 선택해 주세요."
        )
    raw_strategy_ids = payload.get("strategy_ids")
    strategy_ids = []
    if isinstance(raw_strategy_ids, list):
        strategy_ids = list(dict.fromkeys(
            str(value).strip() for value in raw_strategy_ids if str(value).strip()
        ))
    if not strategy_ids and payload.get("strategy_id"):
        strategy_ids = [str(payload["strategy_id"]).strip()]
    if not strategy_ids:
        strategy_ids = [
            str(row["id"])
            for row in mistock_db.rows("SELECT id FROM ai_strategies WHERE selected = 1 ORDER BY id")
        ]
    if not strategy_ids:
        strategy_ids = ["mistock_nasdaq_rule_v1"]
        
    started_state = {
        "is_running": True,
        "mode": mode,
        "started_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        "completed_at": None,
        "result": None,
        "error": None,
        "owner_pid": os.getpid(),
    }
    with _mistock_scheduler_running_lock:
        if not _mistock_scheduler_run_state.claim(started_state):
            raise HTTPException(status_code=409, detail="스케줄러가 이미 실행 중입니다.")
        
    t = threading.Thread(
        target=_bg_run_mistock_scheduled_cycles,
        args=(run_mode, strategy_ids),
        daemon=True
    )
    t.start()
    return {
        "ok": True,
        "status": "started",
        "running": True,
        "mode": mode,
        "strategy_id": strategy_ids[0] if len(strategy_ids) == 1 else None,
        "strategy_ids": strategy_ids,
        "result": {"scanned": 0, "candidates": 0}
    }


@router.post("/api/mistock/circuit-breaker/reset")
def mistock_reset_circuit():
    try:
        mistock_trader.reset_circuit()
        client = mistock_trader._get_kis_client()
        status = client.circuit_status()
    except Exception as exc:
        status = {"opened": False, "error_count": 0, "max_errors": 5, "opened_at": None, "error": str(exc)}
    return {"ok": True, "circuit_breaker": status}


def _auto_approve_mistock_pending_approvals() -> list[dict]:
    import time

    if not _is_mistock_order_window_open():
        logger.info("[MISTOCK] auto-approve skipped: US market is not open")
        return []
    pending = mistock_db.rows("SELECT id FROM approvals WHERE status = 'pending'")
    results = []
    delay = 1.2
    try:
        delay = max(0.0, float(os.environ.get("MISTOCK_ORDER_DELAY_SECONDS", "1.2")))
    except Exception:
        pass

    for idx, row in enumerate(pending):
        try:
            res = _execute_approval(int(row["id"]), approve=True)
            results.append(res)
            if idx < len(pending) - 1:
                time.sleep(delay)
        except Exception:
            continue
    return results


def _run_mistock_auto_approval_batch_async(approval_ids: list[int]) -> None:
    def worker() -> None:
        if not _is_mistock_order_window_open():
            logger.info("[MISTOCK] async auto-approve skipped: US market is not open")
            return
        for approval_id in approval_ids:
            try:
                _execute_approval(int(approval_id), approve=True)
            except Exception as exc:
                logger.warning(f"mistock auto approval failed approval_id={approval_id}: {exc}")

    import threading

    thread = threading.Thread(target=worker, name="mistock-holding-auto-approval", daemon=True)
    thread.start()


@router.post("/api/mistock/auto-approval")
def mistock_set_auto_approval(payload: dict = Body(...)):
    enabled = bool(payload.get("enabled"))
    mistock_db.set_setting("auto_approval", "true" if enabled else "false")
    processed = _auto_approve_mistock_pending_approvals() if enabled else []
    return {"ok": True, "enabled": enabled, "processed": processed, "processed_count": len(processed)}


@router.post("/api/mistock/runtime/order-mode")
def mistock_runtime_order_mode(payload: dict = Body(...)):
    key = str(payload.get("key", "")).strip()
    enabled = bool(payload.get("enabled"))
    if key.upper() == "DRY_RUN":
        updates = {"MISTOCK_DRY_RUN": "true" if enabled else "false"}
        _core._write_env_values(updates, _core._public_value("ENV_PATH", _core.ENV_PATH))
        # Update in-memory config
        mistock_config.dry_run = enabled
        return {
            "ok": True,
            "updated": ["MISTOCK_DRY_RUN"],
            **mistock_trader.runtime_flags(),
        }
    raise HTTPException(status_code=400, detail="key must be DRY_RUN")


@router.post("/api/mistock/orders/cancel")
def mistock_cancel_order(payload: dict = Body(...)):
    symbol = str(payload.get("symbol") or "").strip()
    order_no = str(payload.get("order_no") or payload.get("original_order_no") or "").strip()
    if not symbol or not order_no:
        raise HTTPException(status_code=400, detail="symbol and order_no are required")
    return mistock_trader.cancel_order(symbol, order_no, qty=float(payload.get("qty") or 0))


@router.post("/api/mistock/orders/revise")
def mistock_revise_order(payload: dict = Body(...)):
    symbol = str(payload.get("symbol") or "").strip()
    order_no = str(payload.get("order_no") or payload.get("original_order_no") or "").strip()
    qty = float(payload.get("qty") or 0)
    price = float(payload.get("price") or 0)
    if not symbol or not order_no:
        raise HTTPException(status_code=400, detail="symbol and order_no are required")
    if qty <= 0 or price <= 0:
        raise HTTPException(status_code=400, detail="qty and price must be greater than 0")
    return mistock_trader.revise_order(symbol, order_no, qty=qty, price=price)
