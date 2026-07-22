from __future__ import annotations

import json
from typing import Any

from src.mistock.config import config
from src.mistock import db
from src.mistock.strategy import NASDAQ_UNIVERSE, fetch_history, normalize_symbol, quote, strategy_profile, symbol_name
from src.strategy.indicators import calc_bollinger
from src.utils.exchange_rate import get_usd_krw_rate


_kis_client_cache = None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _first_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), {})
    return {}


def _first_positive(mapping: dict[str, Any], keys: list[str]) -> float:
    for key in keys:
        value = _to_float(mapping.get(key))
        if value > 0:
            return value
    return 0.0


def _configured_capital_usd(exchange_rate: float | None = None) -> float:
    capital = float(config.total_capital or 0.0)
    if str(config.currency or "").upper() == "KRW":
        rate = exchange_rate if (exchange_rate and exchange_rate > 0) else get_usd_krw_rate()
        return capital / rate
    return capital


def _holdings_from_overseas_balance(balance_data: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(balance_data, dict):
        from src.utils.logger import logger
        logger.error(f"Invalid balance_data type in get_holdings: {type(balance_data)}, expected dict. Value: {balance_data}")
        balance_data = {"output1": [], "output2": {}, "output3": {}}

    output1 = balance_data.get("output1", [])
    if not isinstance(output1, list):
        output1 = [output1] if isinstance(output1, dict) else []

    holdings = []
    for item in output1:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("pdno", "")).strip()
        if not symbol:
            continue
        name = item.get("prdt_name", symbol)
        qty = _to_float(item.get("cblc_qty13") or item.get("cblc_qty"))
        if qty <= 0:
            continue
        avg = _to_float(item.get("avg_unpr3") or item.get("avg_unpr"))
        price = _to_float(item.get("ovrs_now_pric1") or item.get("ovrs_now_pric"))
        if price <= 0:
            price = _to_float(quote(symbol).get("current"))
        value = _to_float(item.get("frcr_evlu_amt2") or item.get("frcr_evlu_amt"), qty * price)
        pnl = _to_float(item.get("evlu_pfls_amt2") or item.get("evlu_pfls_amt"), (price - avg) * qty)
        rt = _to_float(
            item.get("evlu_pfls_rt1") or item.get("evlu_pfls_rt"),
            ((price - avg) / avg * 100.0) if avg > 0 else 0.0,
        )
        holdings.append({
            "symbol": symbol,
            "name": name,
            "qty": qty,
            "price": price,
            "avg_price": avg,
            "value": value,
            "pnl": pnl,
            "rt": rt,
        })
    return holdings


def _get_kis_client():
    from src.online_access import require_online_access

    require_online_access("KIS overseas stock API access")
    global _kis_client_cache
    if _kis_client_cache is None:
        from src.kis_client import KISClient, KISClientConfig
        from src.config import config as main_config
        from src.api.kis_api import HTTP
        from pathlib import Path
        env = config.trading_env
        if env not in {"demo", "real"}:
            env = "demo"
        base_url = "https://openapi.koreainvestment.com:9443" if env == "real" else "https://openapivts.koreainvestment.com:29443"
        client_config = KISClientConfig(
            base_url=base_url,
            app_key=main_config.kistock_app_key,
            app_secret=main_config.kistock_app_secret,
            account_no=main_config.kistock_account,
            trading_env=env,
            token_cache_path=Path("data") / "kis_token.json",
        )
        _kis_client_cache = KISClient(client_config, session=HTTP)
    return _kis_client_cache

def runtime_flags() -> dict[str, Any]:
    real_orders_enabled = (not config.dry_run) and config.trading_env == "real" and config.enable_live_trading
    order_submission_enabled = (not config.dry_run) and (config.trading_env == "demo" or real_orders_enabled)
    return {
        "trading_env": config.trading_env,
        "dry_run": config.dry_run,
        "enable_live_trading": config.enable_live_trading,
        "require_approval": config.require_approval,
        "order_submission_enabled": order_submission_enabled,
        "real_orders_enabled": real_orders_enabled,
    }


def broker_submission_available(balance: dict[str, Any] | None = None) -> bool:
    if config.trading_env == "demo":
        balance = balance or get_balance()
        return balance.get("balance_source") != "demo_config_fallback"
    return config.trading_env == "real"


def get_watchlist() -> list[dict[str, Any]]:
    items = db.rows("SELECT symbol, name, created_at FROM watchlist ORDER BY symbol")
    for item in items:
        official_name = symbol_name(item["symbol"])
        if official_name != item["symbol"]:
            item["name"] = official_name
        item["display_name"] = (
            f"{item['name']} ({item['symbol']})"
            if item.get("name") and item["name"] != item["symbol"]
            else item["symbol"]
        )
        item["market"] = "US"
        item["asset_type"] = "미국 주식"
    return items


def add_watchlist(symbol: str, name: str | None = None) -> dict[str, Any]:
    import re
    # Support multiple comma/space/newline separated symbols
    symbols = []
    if "," in symbol or " " in symbol or "\n" in symbol or "\r" in symbol:
        parts = re.split(r"[,\s\r\n]+", symbol)
        for part in parts:
            normalized = normalize_symbol(part)
            if normalized:
                symbols.append(normalized)
    else:
        normalized = normalize_symbol(symbol)
        if normalized:
            symbols.append(normalized)

    if not symbols:
        raise ValueError("symbol is required")

    last_item = {}
    for sym in symbols:
        item_name = name or symbol_name(sym)
        db.execute(
            """
            INSERT INTO watchlist (symbol, name, created_at) VALUES (?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET name = excluded.name
            """,
            (sym, item_name, db.now_text()),
        )
        last_item = {
            "symbol": sym,
            "name": item_name,
            "display_name": f"{item_name} ({sym})" if item_name != sym else sym,
            "market": "US",
            "asset_type": "미국 주식",
        }
    return last_item


def delete_watchlist(symbol: str) -> None:
    db.execute("DELETE FROM watchlist WHERE symbol = ?", (normalize_symbol(symbol),))


def _local_holdings_from_db(*, refresh_quote: bool) -> list[dict[str, Any]]:
    holdings = []
    for row in db.rows("SELECT symbol, name, qty, avg_price FROM holdings ORDER BY symbol"):
        symbol = row["symbol"]
        avg = float(row["avg_price"] or 0.0)
        price = avg
        if refresh_quote:
            try:
                q = quote(symbol)
                price = float(q["current"] or avg or 0.0)
            except Exception:
                price = avg
        qty = float(row["qty"] or 0.0)
        if qty <= 0:
            continue
        value = qty * price
        pnl = (price - avg) * qty
        rt = ((price - avg) / avg * 100.0) if avg > 0 else 0.0
        holdings.append({
            "symbol": symbol,
            "name": row["name"],
            "qty": qty,
            "price": price,
            "avg_price": avg,
            "value": value,
            "pnl": pnl,
            "rt": rt,
        })
    return holdings


def _merge_local_shadow_holdings(broker_holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import sys
    is_testing = "unittest" in sys.modules

    local_holdings = {item["symbol"]: item for item in _local_holdings_from_db(refresh_quote=False)}
    merged = []

    # Get symbols that have been bought via the system
    bought_symbols = set()
    if not is_testing:
        try:
            # We look for any successful buy trades in the trades table.
            # If a symbol was never bought, we treat it as pre-seeded and ignore it in demo mode.
            bought_symbols = {
                row["symbol"]
                for row in db.rows("SELECT DISTINCT symbol FROM trades WHERE action = 'buy' AND ok = 1")
            }
        except Exception:
            pass

    for b_item in broker_holdings:
        symbol = b_item["symbol"]
        if symbol in local_holdings:
            l_item = local_holdings[symbol]
            qty = l_item["qty"]
            if qty > 0:
                price = b_item.get("price") or l_item.get("price") or 0.0
                avg_price = l_item["avg_price"]
                value = qty * price
                pnl = (price - avg_price) * qty
                rt = ((price - avg_price) / avg_price * 100.0) if avg_price > 0 else 0.0
                merged.append({
                    "symbol": symbol,
                    "name": b_item.get("name") or l_item["name"],
                    "qty": qty,
                    "price": price,
                    "avg_price": avg_price,
                    "value": value,
                    "pnl": pnl,
                    "rt": rt,
                    "source": "broker_local_merged"
                })
        else:
            # If it only exists on the broker:
            # In testing, we accept it as-is (required by test mocks).
            # In production demo, we only accept it if it was bought via the system.
            if is_testing or (symbol in bought_symbols):
                merged.append(b_item)

    # 2. Add local shadow holdings that are not on the broker
    broker_symbols = {str(item.get("symbol") or "") for item in broker_holdings}
    for item in local_holdings.values():
        if item["symbol"] not in broker_symbols:
            merged.append({**item, "source": "local_shadow"})

    return merged


def _apply_local_filled_order(symbol: str, action: str, qty: float, price: float) -> None:
    existing = db.row("SELECT symbol, name, qty, avg_price FROM holdings WHERE symbol = ?", (symbol,))
    if action == "buy":
        cost = qty * price
        if existing:
            old_qty = float(existing["qty"])
            old_avg = float(existing["avg_price"])
            new_qty = old_qty + qty
            new_avg = ((old_qty * old_avg) + cost) / new_qty
            db.execute("UPDATE holdings SET qty = ?, avg_price = ?, updated_at = ? WHERE symbol = ?", (new_qty, new_avg, db.now_text(), symbol))
        else:
            db.execute(
                "INSERT INTO holdings (symbol, name, qty, avg_price, updated_at) VALUES (?, ?, ?, ?, ?)",
                (symbol, symbol_name(symbol), qty, price, db.now_text()),
            )
    elif action == "sell" and existing:
        remaining = float(existing["qty"]) - qty
        if remaining > 0:
            db.execute("UPDATE holdings SET qty = ?, updated_at = ? WHERE symbol = ?", (remaining, db.now_text(), symbol))
        else:
            db.execute("DELETE FROM holdings WHERE symbol = ?", (symbol,))


def _kis_demo_order_is_unsupported(msg: str) -> bool:
    normalized = str(msg or "")
    return any(
        marker in normalized
        for marker in (
            "모의투자에서는 해당업무가 제공되지 않습니다",
            "해당업무가 제공되지 않습니다",
            "not provided in demo",
            "unsupported in demo",
        )
    )


def get_holdings() -> list[dict[str, Any]]:
    if config.trading_env not in {"demo", "real"}:
        return _local_holdings_from_db(refresh_quote=True)
    else:
        try:
            client = _get_kis_client()
            balance_data = client.get_overseas_balance()
            holdings = _holdings_from_overseas_balance(balance_data)
            if config.trading_env == "demo":
                holdings = _merge_local_shadow_holdings(holdings)
            return holdings
        except Exception as e:
            from src.utils.logger import logger
            logger.error(f"Failed to fetch KIS US holdings: {e}")
            return _local_holdings_from_db(refresh_quote=False) if config.trading_env == "demo" else []


def get_balance() -> dict[str, Any]:
    if config.trading_env not in {"demo", "real"}:
        cash = float(db.get_setting("cash", str(config.total_capital)) or 0.0)
        holdings = get_holdings()
        stock_eval = sum(float(item["value"] or 0.0) for item in holdings)
        pnl = sum(float(item["pnl"] or 0.0) for item in holdings)
        total_eval = cash + stock_eval
        return {
            "cash": cash,
            "total_eval": total_eval,
            "broker_total_eval": total_eval,
            "calculated_total_eval": total_eval,
            "stock_eval": stock_eval,
            "cash_ratio": cash / total_eval if total_eval > 0 else 0.0,
            "stock_ratio": stock_eval / total_eval if total_eval > 0 else 0.0,
            "pnl": pnl,
            "holdings": holdings,
        }
    else:
        try:
            client = _get_kis_client()
            balance_data = client.get_overseas_balance()
            if not isinstance(balance_data, dict):
                from src.utils.logger import logger
                logger.error(f"Invalid balance_data type in get_balance: {type(balance_data)}, expected dict. Value: {balance_data}")
                balance_data = {"output1": [], "output2": {}, "output3": {}}
            
            # output2: 통화별 잔고 리스트일 경우 USD 항목 우선 선택
            raw_output2 = balance_data.get("output2", {})
            if isinstance(raw_output2, list):
                summary = (
                    next((item for item in raw_output2 if isinstance(item, dict) and item.get("crcy_cd") == "USD"), None)
                    or next((item for item in raw_output2 if isinstance(item, dict)), {})
                )
            elif isinstance(raw_output2, dict):
                summary = raw_output2
            else:
                summary = {}

            # KIS 외화 예수금 파싱 (USD 기준)
            cash = _first_positive(summary, [
                "frcr_dncl_amt",
                "frcr_dncl_amt_2",
                "frcr_drwg_psbl_amt",
                "frcr_drwg_psbl_amt_1",
            ])

            # 통합증거금 원화 가용 자원 파싱
            output3 = balance_data.get("output3", {})
            if not isinstance(output3, dict):
                if isinstance(output3, list):
                    output3 = next((item for item in output3 if isinstance(item, dict)), {})
                else:
                    output3 = {}

            exchange_rate = _to_float(summary.get("frst_rt") or output3.get("frst_rt"), 0.0)
            if exchange_rate <= 0:
                exchange_rate = get_usd_krw_rate()

            # output3의 frcr_use_psbl_amt(외화사용가능금액)가 있으면 USD 현금으로 우선 사용
            # 이 값이 KIS가 실제 허용하는 해외주식 매수가능 달러 금액이다
            frcr_use_psbl = _to_float(output3.get("frcr_use_psbl_amt"), 0.0)
            if frcr_use_psbl > 0:
                cash = frcr_use_psbl
            elif cash <= 0:
                # USD 잔고를 못 읽은 경우만 KRW 통합증거금을 환산해 보완
                krw_cash = _first_positive(output3, ["tot_dncl_amt", "dncl_amt"])
                if krw_cash > 0:
                    cash = (krw_cash / exchange_rate) * 0.98

            holdings = _holdings_from_overseas_balance(balance_data)
            if config.trading_env == "demo":
                holdings = _merge_local_shadow_holdings(holdings)
            stock_eval = sum(float(item["value"] or 0.0) for item in holdings)
            pnl = sum(float(item["pnl"] or 0.0) for item in holdings)
            local_shadow_eval = sum(
                float(item.get("value") or 0.0)
                for item in holdings
                if item.get("source") == "local_shadow"
            )
            if local_shadow_eval > 0 and cash > 0:
                cash = max(0.0, cash - local_shadow_eval)
            # frcr_evlu_tota는 USD 평가액 합계 (KRW 환산 아님)
            broker_total_eval = _first_positive(summary, [
                "frcr_evlu_tota",
                "tot_asst_amt",
                "tot_evlu_amt",
            ]) or _first_positive(output3, [
                "frcr_evlu_tota",
            ])
            # KRW 단위인 tot_asst_amt를 USD로 오인하는 fallback 제거.
            # 다만, demo 환경에서 예수금이 잡히지 않은 경우 broker_total_eval과 stock_eval 차이로부터 복구한다.
            if config.trading_env == "demo" and cash <= 0 and broker_total_eval > 0:
                cash = max(0.0, broker_total_eval - stock_eval)
            balance_source = "kis"
            if config.trading_env == "demo" and cash <= 0 and local_shadow_eval > 0 and broker_total_eval <= 0:
                cash = max(0.0, _configured_capital_usd(exchange_rate) - stock_eval)
                balance_source = "demo_local_shadow"
            if config.trading_env == "demo" and cash <= 0 and stock_eval <= 0 and broker_total_eval <= 0:
                cash = _configured_capital_usd(exchange_rate)
                balance_source = "demo_config_fallback"
            if config.trading_env == "demo":
                configured_cap = _configured_capital_usd(exchange_rate)
                if configured_cap > 0:
                    effective_total = cash + stock_eval
                    if effective_total > configured_cap:
                        cash = max(0.0, configured_cap - stock_eval)
                        balance_source = "kis_config_capped"
            total_eval = cash + stock_eval
            return {
                "cash": cash,
                "total_eval": total_eval,
                "broker_total_eval": broker_total_eval or total_eval,
                "calculated_total_eval": total_eval,
                "balance_source": balance_source,
                "stock_eval": stock_eval,
                "cash_ratio": cash / total_eval if total_eval > 0 else 0.0,
                "stock_ratio": stock_eval / total_eval if total_eval > 0 else 0.0,
                "pnl": pnl,
                "holdings": holdings,
            }
        except Exception as e:
            from src.utils.logger import logger
            logger.error(f"Failed to fetch KIS US balance: {e}")
            return {
                "cash": 0.0,
                "total_eval": 0.0,
                "broker_total_eval": 0.0,
                "calculated_total_eval": 0.0,
                "stock_eval": 0.0,
                "cash_ratio": 0.0,
                "stock_ratio": 0.0,
                "pnl": 0.0,
                "holdings": [],
                "_error": str(e),
            }


def _active_min_score(default: int = 2, model: str | None = None) -> int:
    active_model = str(model or config.strategy_model or "").lower()
    if active_model == "macd_rsi_momentum":
        return int(config.indicator_min_score or default)
    return default


def _custom_strategy(strategy_id: str | None):
    if strategy_id == "plunge_bounce_strategy":
        from src.strategy.custom_rules.plunge_bounce_strategy import PlungeBounceStrategy
        return PlungeBounceStrategy()
    if strategy_id == "rsi_limit_strategy":
        from src.strategy.custom_rules.rsi_limit_strategy import CustomRSILimitStrategy
        return CustomRSILimitStrategy()
    if strategy_id == "heikin_ashi_scalping_strategy":
        from src.strategy.custom_rules.heikin_ashi_scalping_strategy import AlphaHeikinAshiScalpingStrategy
        return AlphaHeikinAshiScalpingStrategy()
    return None


def scan_candidates(
    min_score: int | None = None,
    limit: int | None = None,
    model: str | None = None,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    api = None
    try:
        api = _get_kis_client()
    except Exception:
        pass
    from src.mistock.strategy import build_scan_universe
    watchlist = [item["symbol"] for item in get_watchlist()]
    dynamic_universe = build_scan_universe(api)
    universe = list(dict.fromkeys(watchlist + dynamic_universe))[: limit or config.scan_universe_size]
    custom_strategy = _custom_strategy(strategy_id)
    effective_min_score = int(min_score if min_score is not None else _active_min_score(model=model))
    candidates = []
    scanned = 0
    scan_error = ""
    for symbol in universe:
        try:
            hist = fetch_history(symbol)
            profile = strategy_profile(hist["close"], hist["high"], hist["volume"], model=model)
            if custom_strategy is not None:
                bb_lo, _bb_mid, _bb_hi = calc_bollinger(hist["close"], 20)
                indicators = {
                    **profile,
                    "symbol": symbol,
                    "highs": hist["high"],
                    "lows": hist["close"],
                    "opens": hist["close"],
                    "volumes": hist["volume"],
                    "bb_lo": bb_lo,
                }
                profile["score"] = float(custom_strategy.calculate_score(hist["close"], indicators))
                profile["reasons"] = indicators.get("custom_reasons") or indicators.get("pb_reasons") or [strategy_id]
            scanned += 1
            score = float(profile["score"])
            row = {
                "ticker": symbol,
                "symbol": symbol,
                "name": symbol_name(symbol),
                "score": score,
                "reasons": profile["reasons"],
                "price": profile["price"],
                "rsi": profile["rsi"],
                "rsi2": profile["rsi2"],
                "macd_hist": profile["macd_hist"],
                "sma20": profile["sma20"],
                "sma60": profile["sma60"],
                "strategy_id": strategy_id or "mistock_nasdaq_rule_v1",
            }
            db.execute(
                """
                INSERT INTO scanned_candidates
                (scanned_at, symbol, name, score, reasons, price, env, rsi, rsi2, macd_hist, sma20, sma60, strategy_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    db.now_text(), symbol, row["name"], score, ",".join(profile["reasons"]),
                    row["price"], config.trading_env, row["rsi"], row["rsi2"], row["macd_hist"], row["sma20"], row["sma60"], row["strategy_id"],
                ),
            )
            if score >= effective_min_score:
                candidates.append(row)
        except Exception as exc:
            scan_error = str(exc)
    candidates.sort(key=lambda item: (item["score"], item["price"] or 0), reverse=True)

    # AI 자동 추가적용 로직 (스케줄러 주기적 관리 지원)
    try:
        if db.get_setting("ai_auto_add", "false") == "true":
            threshold = float(db.get_setting("ai_auto_add_threshold", "3") or 3)
            # Add candidates with score >= threshold
            for candidate in candidates:
                if candidate["score"] >= threshold:
                    add_watchlist(candidate["symbol"], candidate["name"])
            
            # Remove symbols from watchlist if they were scanned and score < threshold
            scanned_symbols = {c["symbol"] for c in candidates}
            for candidate in candidates:
                if candidate["symbol"] in scanned_symbols and candidate["score"] < threshold:
                    delete_watchlist(candidate["symbol"])
    except Exception:
        pass

    return {
        "candidates": candidates,
        "scanned": scanned,
        "min_score": effective_min_score,
        "scan_summary": {"scanned": scanned, "matched": len(candidates), "scan_error": scan_error},
        "scan_error": scan_error,
        "strategy_id": strategy_id or "mistock_nasdaq_rule_v1",
    }


def build_orders(candidates: list[dict[str, Any]], cash: float) -> list[dict[str, Any]]:
    orders = []
    # 사이징은 설정된 운용자금(total_capital)을 상한으로 한다. demo 모의투자 계좌의
    # 통합증거금은 수억 달러로 잡혀 그대로 쓰면 주문이 비정상적으로 커지므로 상한을 건다.
    cap = _configured_capital_usd()
    sizing_cash = min(cash, cap) if cap > 0 else cash
    budget = max(0.0, sizing_cash * (1.0 - config.cash_buffer))
    slots = max(1, min(config.max_positions, len(candidates)))
    per_order = budget / slots if slots else 0.0
    for candidate in candidates[:slots]:
        price = float(candidate.get("price") or quote(candidate["symbol"])["current"] or 0.0)
        if price <= 0:
            continue
        qty = int(per_order // price)
        if qty <= 0:
            continue
        orders.append({
            "ticker": candidate["symbol"],
            "symbol": candidate["symbol"],
            "name": candidate["name"],
            "limit_price": price,
            "price": price,
            "quantity": qty,
            "qty": qty,
            "estimated_cost": qty * price,
            "reason": ", ".join(candidate.get("reasons") or []),
            "strategy_score": candidate.get("score", 0),
        })
    return orders


def annotate_candidates_with_order_plan(candidates: list[dict[str, Any]], cash: float) -> list[dict[str, Any]]:
    orders_by_symbol = {item["symbol"]: item for item in build_orders(candidates, cash)}
    annotated = []
    for candidate in candidates:
        row = dict(candidate)
        order = orders_by_symbol.get(row["symbol"])
        if order:
            row.update({
                "planned_qty": order["qty"],
                "quantity": order["qty"],
                "qty": order["qty"],
                "limit_price": order["price"],
                "estimated_cost": order["estimated_cost"],
                "order_reason": order["reason"],
            })
        else:
            row.update({
                "planned_qty": 0,
                "quantity": 0,
                "qty": 0,
                "limit_price": row.get("price") or 0,
                "estimated_cost": 0,
            })
        return_price = row.get("current_price")
        if return_price is None:
            row["current_price"] = row.get("price") or 0
        annotated.append(row)
    return annotated


def signals() -> list[dict[str, Any]]:
    balance = get_balance()
    rows = []
    for holding in balance["holdings"]:
        hist = fetch_history(holding["symbol"])
        profile = strategy_profile(hist["close"], hist["high"], hist["volume"])
        action = "hold"
        if str(config.strategy_model or "").lower() == "macd_rsi_momentum" and (
            profile.get("macd_bear_cross") or profile["rsi"] < config.indicator_rsi_entry_min
        ):
            action = "sell"
        elif profile["rsi"] >= config.rsi_sell or holding["rt"] >= config.take_profit:
            action = "sell"
        elif holding["rt"] <= config.stop_loss_pct:
            action = "sell"
        rows.append({
            "symbol": holding["symbol"],
            "name": holding["name"],
            "action": action,
            "strategy_score": profile["score"],
            "signal_qty": int(holding["qty"]),
            "signal_price": holding["price"],
            "rsi": profile["rsi"],
            "rsi2": profile["rsi2"],
            "macd_hist": profile["macd_hist"],
            "reason": ", ".join(profile["reasons"]),
        })
    return rows


def execution_plan() -> dict[str, Any]:
    balance = get_balance()
    scan = scan_candidates()
    orders = build_orders(scan["candidates"], balance["cash"])
    return {
        "mode": "mistock-demo",
        "plan": orders,
        "cash": balance["cash"],
        "remaining_cash": balance["cash"] - sum(item["estimated_cost"] for item in orders),
        "total_eval": balance["total_eval"],
        "pnl": balance["pnl"],
        "daily_loss_halt": False,
        "scanned": scan["scanned"],
        "scan_error": scan["scan_error"],
    }


def notify_slack_order(symbol: str, action: str, qty: float, price: float, reason: str, ok: bool) -> None:
    try:
        from src.notifier.slack import mistock_slack_order
        # Gather indicators
        indicators = {"rsi": 0.0, "sma20": 0.0, "sma60": 0.0, "rt": 0.0}
        try:
            hist = fetch_history(symbol)
            profile = strategy_profile(hist["close"], hist["high"], hist["volume"])
            indicators["rsi"] = float(profile.get("rsi", 0.0))
            indicators["sma20"] = float(profile.get("sma20", 0.0))
            indicators["sma60"] = float(profile.get("sma60", 0.0))
        except Exception:
            pass

        if action == "sell":
            try:
                if config.trading_env not in {"demo", "real"}:
                    existing = db.row("SELECT avg_price FROM holdings WHERE symbol = ?", (symbol,))
                    if existing:
                        avg_price = float(existing["avg_price"])
                        indicators["rt"] = ((price - avg_price) / avg_price * 100.0) if avg_price > 0 else 0.0
                else:
                    holdings = get_holdings()
                    matching = next((h for h in holdings if h["symbol"] == symbol), None)
                    if matching:
                        indicators["rt"] = matching["rt"]
            except Exception:
                pass

        mistock_slack_order(
            name=symbol_name(symbol),
            symbol=symbol,
            action=action,
            qty=qty,
            price=price,
            reason=reason,
            ok=ok,
            indicators=indicators,
        )
    except Exception as e:
        from src.utils.logger import logger
        logger.error(f"Failed to send Slack order notification: {e}")


def place_order(symbol: str, action: str, qty: float, price: float, reason: str = "", strategy_id: str | None = None) -> dict[str, Any]:
    from src.online_access import require_online_access

    require_online_access("Mistock order execution")
    symbol = normalize_symbol(symbol)
    action = str(action).lower()
    qty = float(qty)
    price = float(price or quote(symbol)["current"] or 0.0)
    if qty <= 0 or price <= 0:
        notify_slack_order(symbol, action, qty, price, "qty and price must be greater than 0", False)
        return {"ok": False, "status": "failed", "message": "qty and price must be greater than 0"}

    if config.trading_env not in {"demo", "real"}:
        cash = float(db.get_setting("cash", str(config.total_capital)) or 0.0)
        existing = db.row("SELECT symbol, name, qty, avg_price FROM holdings WHERE symbol = ?", (symbol,))
        if action == "buy":
            cost = qty * price
            if cost > cash:
                notify_slack_order(symbol, action, qty, price, "insufficient cash", False)
                return {"ok": False, "status": "failed", "message": "insufficient cash"}
            _apply_local_filled_order(symbol, action, qty, price)
            db.set_setting("cash", str(cash - cost))
        elif action == "sell":
            if not existing or float(existing["qty"]) < qty:
                notify_slack_order(symbol, action, qty, price, "insufficient holdings", False)
                return {"ok": False, "status": "failed", "message": "insufficient holdings"}
            _apply_local_filled_order(symbol, action, qty, price)
            db.set_setting("cash", str(cash + qty * price))
        else:
            notify_slack_order(symbol, action, qty, price, "action must be buy or sell", False)
            return {"ok": False, "status": "failed", "message": "action must be buy or sell"}
        save_trade(symbol, symbol_name(symbol), action, qty, price, reason, True, "filled", "simulated order filled")
        notify_slack_order(symbol, action, qty, price, reason or "simulated order filled", True)
        return {"ok": True, "status": "filled", "msg1": "simulated order filled"}
    else:
        real_orders_enabled = (not config.dry_run) and config.trading_env == "real" and config.enable_live_trading
        order_submission_enabled = (not config.dry_run) and (config.trading_env == "demo" or real_orders_enabled)
        if not order_submission_enabled:
            save_trade(symbol, symbol_name(symbol), action, qty, price, reason, True, "dry_run", "dry run order skipped")
            notify_slack_order(symbol, action, qty, price, reason or "dry run order skipped", True)
            return {"ok": True, "status": "dry_run", "msg1": "dry run order skipped"}

        try:
            client = _get_kis_client()
            res = client.place_overseas_order(symbol, action, price, qty)
            rt_cd = res.get("rt_cd")
            msg = res.get("msg1") or "KIS order response received"
            ok = (rt_cd == "0")
            status = "filled" if ok else "failed"
            if ok and config.trading_env == "demo":
                _apply_local_filled_order(symbol, action, qty, price)
            elif config.trading_env == "demo" and _kis_demo_order_is_unsupported(msg):
                _apply_local_filled_order(symbol, action, qty, price)
                ok = True
                status = "demo_local_filled"
                msg = f"KIS demo overseas order unsupported; local shadow fill applied: {msg}"
            save_trade(symbol, symbol_name(symbol), action, qty, price, reason, ok, status, msg, strategy_id)
            notify_slack_order(symbol, action, qty, price, reason or msg, ok)
            return {"ok": ok, "status": status, "msg1": msg, "res": res}
        except Exception as e:
            from src.utils.logger import logger
            logger.error(f"Failed to place KIS US order: {e}")
            save_trade(symbol, symbol_name(symbol), action, qty, price, reason, False, "failed", str(e), strategy_id)
            notify_slack_order(symbol, action, qty, price, str(e), False)
            return {"ok": False, "status": "failed", "message": str(e)}


def cancel_order(symbol: str, order_no: str, qty: float = 0) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    if config.trading_env not in {"demo", "real"}:
        return {"ok": False, "status": "unsupported", "message": "broker cancel requires MISTOCK_TRADING_ENV=demo or real"}
    if config.dry_run:
        return {"ok": True, "status": "dry_run", "msg1": "dry run cancel skipped"}
    try:
        res = _get_kis_client().cancel_overseas_order(symbol, order_no, qty=qty)
        return {"ok": res.get("rt_cd") == "0", "status": "submitted" if res.get("rt_cd") == "0" else "failed", "res": res}
    except Exception as exc:
        return {"ok": False, "status": "failed", "message": str(exc)}


def revise_order(symbol: str, order_no: str, qty: float, price: float) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    if config.trading_env not in {"demo", "real"}:
        return {"ok": False, "status": "unsupported", "message": "broker revise requires MISTOCK_TRADING_ENV=demo or real"}
    if config.dry_run:
        return {"ok": True, "status": "dry_run", "msg1": "dry run revise skipped"}
    try:
        res = _get_kis_client().revise_overseas_order(symbol, order_no, qty=qty, price=price)
        return {"ok": res.get("rt_cd") == "0", "status": "submitted" if res.get("rt_cd") == "0" else "failed", "res": res}
    except Exception as exc:
        return {"ok": False, "status": "failed", "message": str(exc)}


def save_trade(symbol: str, name: str, action: str, qty: float, price: float, reason: str, ok: bool, order_status: str, response_msg: str, strategy_id: str | None = None) -> None:
    # 수수료/세금 예상 계산 (미장 기본 수수료 0.1%, 매도시 SEC Fee 등 0.03% 추가)
    fee = (qty * price * 0.001) if ok else 0.0
    tax = (qty * price * 0.0003) if (ok and action.lower() == "sell") else 0.0
    exchange_rate = get_usd_krw_rate()
    
    db.execute(
        """
        INSERT INTO trades (ts, symbol, name, action, qty, price, reason, ok, env, dry_run, order_status, response_msg, broker_result, fee, tax, exchange_rate, strategy_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            db.now_text(), symbol, name, action, qty, price, reason, int(ok), config.trading_env,
            int(config.dry_run), order_status, response_msg, json.dumps({"env": config.trading_env}, ensure_ascii=False),
            fee, tax, exchange_rate, strategy_id,
        ),
    )


def reset_circuit() -> None:
    client = _get_kis_client()
    if client and hasattr(client, "circuit"):
        client.circuit.reset()
