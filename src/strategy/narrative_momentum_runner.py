# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src import trader
from src.db.repository import save_scanned_candidate
from src.strategy.narrative_collector import collect_narrative_history
from src.strategy.narrative_momentum import (
    STRATEGY_ID,
    NarrativeMomentumStrategy,
    load_json_file,
    save_json_file,
)

BASE_DIR = Path(__file__).resolve().parents[2]
THEME_MAP_PATH = BASE_DIR / "config" / "theme_map.json"
NARRATIVE_HISTORY_PATH = BASE_DIR / ".runtime" / "narrative_history.json"
LATEST_RESULT_PATH = BASE_DIR / ".runtime" / "narrative_momentum_latest.json"


def load_inputs(
    history_path: Path = NARRATIVE_HISTORY_PATH,
    theme_map_path: Path = THEME_MAP_PATH,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[str]]:
    errors = []
    try:
        history = load_json_file(history_path, [])
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        history = []
        errors.append(f"failed to load narrative history: {exc}")
    try:
        theme_map = load_json_file(theme_map_path, {})
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        theme_map = {}
        errors.append(f"failed to load theme map: {exc}")
    if not isinstance(history, list):
        errors.append("narrative_history must be a list")
        history = []
    if not isinstance(theme_map, dict):
        errors.append("theme_map must be an object")
        theme_map = {}
    return history, theme_map, errors


def build_summary(result: dict[str, Any]) -> dict[str, Any]:
    signals = result.get("signals") if isinstance(result.get("signals"), list) else []
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    top = signals[:5]
    avg_score = 0.0
    if signals:
        avg_score = round(sum(float(item.get("final_score") or item.get("score") or 0) for item in signals) / len(signals), 1)
    return {
        "state": status.get("state"),
        "today": status.get("today"),
        "latest_date": status.get("latest_date"),
        "candidate_count": len(signals),
        "saved_count": int(result.get("saved_count") or 0),
        "unmatched_count": len(result.get("unmatched") or []),
        "avg_score": avg_score,
        "top_signals": [
            {
                "ticker": item.get("ticker"),
                "name": item.get("name"),
                "score": item.get("final_score") or item.get("score"),
                "themes": item.get("themes", []),
            }
            for item in top
        ],
    }


def run_narrative_momentum_cycle(
    *,
    save_candidates: bool = True,
    write_latest: bool = True,
    auto_collect: bool = False,
    history_path: Path = NARRATIVE_HISTORY_PATH,
    theme_map_path: Path = THEME_MAP_PATH,
    latest_path: Path = LATEST_RESULT_PATH,
) -> dict[str, Any]:
    history, theme_map, errors = load_inputs(history_path, theme_map_path)
    strategy = NarrativeMomentumStrategy()
    status = strategy.status(history, theme_map)
    collection = None
    if auto_collect and status.get("state") != "fresh":
        collection = collect_narrative_history(
            history_path=history_path,
            theme_map_path=theme_map_path,
        )
        if collection.get("generated"):
            history, theme_map, errors = load_inputs(history_path, theme_map_path)
            strategy = NarrativeMomentumStrategy()
            status = strategy.status(history, theme_map)
    signals = strategy.calculate_signals(history, theme_map)
    unmatched = strategy.unmatched_narratives(history, theme_map)
    result = {
        "strategy_id": STRATEGY_ID,
        "strategy": STRATEGY_ID,
        "mode": "execute" if save_candidates else "analysis_only",
        "status": status,
        "signals": signals,
        "unmatched": unmatched,
        "total_scanned": len(signals),
        "saved_count": 0,
        "errors": errors,
        "ok": not errors,
        "ran_at": trader.datetime.now(trader.KST).isoformat(),
    }
    if collection is not None:
        result["collection"] = collection
    if errors:
        result["summary"] = build_summary(result)
        if write_latest:
            save_json_file(latest_path, result)
        return result
    if save_candidates:
        result["saved_count"] = save_candidates_from_signals(signals)
    result["summary"] = build_summary(result)
    if write_latest:
        save_json_file(latest_path, result)
    return result


def save_candidates_from_signals(signals: list[dict[str, Any]]) -> int:
    saved_count = 0
    for signal in signals:
        price = int(_to_float(signal.get("current_price", signal.get("price"))))
        top_features = {
            "themes": signal.get("themes", []),
            "narratives": signal.get("narratives", []),
            "breakdown": signal.get("breakdown", []),
            "price_source": "signal" if price > 0 else "not_available",
        }
        saved_id = save_scanned_candidate(
            symbol=signal.get("ticker", ""),
            name=signal.get("name", signal.get("ticker", "")),
            score=signal.get("score", 0),
            reasons=signal.get("reasons", []),
            price=price,
            env=trader.runtime_flags().trading_env,
            indicators={},
            strategy={"id": STRATEGY_ID},
            ranker_model="rule_only",
            optimizer="narrative_momentum",
            scoring={
                "rule_score": signal.get("rule_score"),
                "ml_score": signal.get("ml_score"),
                "final_score": signal.get("final_score"),
                "ai_model_status": "not_used",
                "top_features": top_features,
            },
        )
        if saved_id:
            saved_count += 1
    return saved_count


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
