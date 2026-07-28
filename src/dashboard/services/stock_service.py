from __future__ import annotations

import sqlite3
from typing import Any

from src import trader
from src.db.repository import load_ai_strategies
from src.utils.logger import logger


class DashboardStockService:
    def resolve_dashboard_strategy(self, strategy_id: str | None = None) -> dict | None:
        strategies = load_ai_strategies()
        if strategy_id:
            return next(
                (
                    strategy
                    for strategy in strategies
                    if strategy.get("id") == strategy_id or strategy.get("model") == strategy_id
                ),
                None,
            )
        return next((strategy for strategy in strategies if strategy.get("selected")), None)

    def build_dashboard_signals(self, api, parsed: dict, strategy: dict | None = None) -> list[dict]:
        resolved_strategy_id = strategy.get("id") if strategy else "seven_split"
        strategy_model = str(strategy.get("model") or "") if strategy else ""
        if strategy_model == "none":
            strategy_model = ""
        signals = []
        for holding in parsed["holdings"]:
            daily = api.get_daily(holding["symbol"], n=60)
            signal = trader.generate_signal(
                holding["_raw"],
                daily,
                strategy_model=strategy_model,
            )
            indicators = signal.get("indicators", {})
            signals.append({
                "strategy_id": resolved_strategy_id,
                "symbol": holding["symbol"],
                "name": holding["name"],
                "qty": holding["qty"],
                "price": holding["price"],
                "rt": holding["rt"],
                "action": signal.get("action", "hold"),
                "signal_qty": signal.get("qty", 0),
                "signal_price": signal.get("price", 0),
                "reason": signal.get("reason", ""),
                "rsi": indicators.get("rsi"),
                "rsi2": indicators.get("rsi2"),
                "sma20": indicators.get("sma20"),
                "sma60": indicators.get("sma60"),
                "bb_lo": indicators.get("bb_lo"),
                "bb_hi": indicators.get("bb_hi"),
                "strategy_score": indicators.get("strategy_score"),
                "macd_hist": indicators.get("macd_hist"),
            })
        return signals

    def build_dashboard_candidates(
        self,
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
        held_symbols = {holding["symbol"] for holding in parsed["holdings"]}
        if universe is None:
            universe = trader.build_scan_universe(api, held_symbols)
        else:
            universe = [symbol for symbol in universe if symbol not in held_symbols]

        if ranker == "gpt_5_mini" and ranker_weight == 0.4 and not strategy_model:
            scan_kwargs = {
                "universe": universe,
                "min_score": min_score,
            }
            if strategy_profile is not None:
                scan_kwargs["strategy_profile"] = strategy_profile
            if strategy_description:
                scan_kwargs["strategy_description"] = strategy_description
            scan_result = trader.find_candidates(held_symbols, **scan_kwargs)
        else:
            ranker_param = "rule_only" if ranker == "none" else ranker
            orig_weight = trader.config.ai_score_weight
            orig_model = trader.config.openai_model
            orig_enabled = trader.config.ai_strategy_enabled
            try:
                trader.config.ai_score_weight = ranker_weight
                trader.config.openai_model = ranker_param
                if ranker_param == "rule_only":
                    trader.config.ai_strategy_enabled = False
                else:
                    trader.config.ai_strategy_enabled = True

                scan_result = trader.find_candidates(
                    held_symbols,
                    universe=universe,
                    min_score=min_score,
                    ranker=ranker_param,
                    strategy_model=strategy_model,
                    strategy_profile=strategy_profile,
                    strategy_description=strategy_description,
                )
            finally:
                trader.config.ai_score_weight = orig_weight
                trader.config.openai_model = orig_model
                trader.config.ai_strategy_enabled = orig_enabled
            
        candidates = scan_result.get("candidates", [])
        
        if optimizer == "score_tilted_inverse_vol":
            orders = trader.build_orders(candidates, api.get_quote, len(parsed["holdings"]), parsed["cash"])
        else:
            orders = trader.build_orders(
                candidates, api.get_quote, len(parsed["holdings"]), parsed["cash"], optimizer=optimizer
            )
            
        order_by_ticker = {order["ticker"]: order for order in orders}

        rows = []
        for candidate in candidates:
            order = order_by_ticker.get(candidate["ticker"], {})
            row = {
                "ticker": candidate["ticker"],
                "name": candidate.get("name", candidate["ticker"]),
                "current_price": candidate["current_price"],
                "score": candidate["score"],
                "reasons": candidate["reasons"],
                "rsi": candidate.get("rsi"),
                "rsi2": candidate.get("rsi2"),
                "macd_hist": candidate.get("macd_hist"),
                "sma20": candidate.get("sma20"),
                "sma60": candidate.get("sma60"),
                "bb_lo": candidate.get("bb_lo"),
                "bb_hi": candidate.get("bb_hi"),
                "planned_qty": order.get("quantity", 0),
                "limit_price": order.get("limit_price", 0),
                "estimated_cost": order.get("estimated_cost", 0),
                "universe_size": len(universe),
            }
            for key in (
                "rule_score",
                "ml_score",
                "final_score",
                "ai_enabled",
                "ai_model_status",
                "ai_model_version",
                "feature_version",
                "ai_score_weight",
                "ai_fallback_reason",
                "top_features",
                "promoted_by_ai",
            ):
                if key in candidate:
                    row[key] = candidate[key]
            rows.append(row)

        return {
            "candidates": rows,
            "universe_size": len(universe),
            "scanned": scan_result.get("scanned", 0),
            "min_score": min_score,
            "scan_summary": scan_result.get("scan_summary", []),
            "scan_error": scan_result.get("scan_error"),
        }

    def build_dashboard_execution_plan(
        self,
        api,
        balance_data: dict,
        parsed_balance: dict,
        strategy_id: str | None = None,
        candidate_scan: dict | None = None,
    ) -> dict:
        market_data_api = trader.build_market_data_api(api)
        runtime_bundle = trader.build_runtime_plan(
            market_data_api,
            balance_data,
            read_cached_candidates=True,
            force_strategy_id=strategy_id,
            candidate_scan_override=candidate_scan,
        )
        return {
            "mode": "dashboard",
            "strategy_id": strategy_id or "seven_split",
            "plan": runtime_bundle["plan"],
            "cash": parsed_balance["cash"],
            "remaining_cash": runtime_bundle["remaining_cash"],
            "total_eval": parsed_balance["total_eval"],
            "pnl": parsed_balance["pnl"],
            "daily_loss_halt": runtime_bundle["daily_loss_halt"],
            "scanned": runtime_bundle["candidate_scan"]["scanned"],
            "scan_error": runtime_bundle["candidate_scan"]["scan_error"],
        }
