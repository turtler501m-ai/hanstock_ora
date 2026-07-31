"""Cost-aware walk-forward validation for the deterministic technical strategy."""

from __future__ import annotations

from typing import Callable


def run_technical_walk_forward(
    prices: list[float],
    highs: list[float],
    volumes: list[float],
    *,
    profile_builder: Callable[[list[float], list[float], list[float]], dict],
    min_score: float = 4.0,
    folds: int = 3,
    warmup: int = 60,
    stop_loss_pct: float = 10.0,
    trailing_activation_pct: float = 10.0,
    trailing_stop_pct: float = 7.0,
    cost_bps: float = 10.0,
) -> dict:
    size = min(len(prices), len(highs), len(volumes))
    if size < warmup + max(20, folds * 5):
        return {"ok": False, "passed": False, "reason": "insufficient_data", "folds": []}

    start = warmup
    fold_size = max(5, (size - start) // max(1, folds))
    results = []
    all_pnls = []
    equity = 1.0
    equity_curve = [equity]
    cost_rate = max(0.0, cost_bps) / 10_000

    for fold_index in range(max(1, folds)):
        fold_start = start + fold_index * fold_size
        fold_end = size if fold_index == folds - 1 else min(size, fold_start + fold_size)
        position = None
        fold_start_equity = equity
        fold_trades = []
        for index in range(fold_start, fold_end):
            current = float(prices[index])
            history_prices = prices[:index + 1]
            profile = profile_builder(history_prices, highs[:index + 1], volumes[:index + 1])
            if position is None:
                if float(profile.get("score") or 0) >= min_score:
                    entry = current * (1 + cost_rate)
                    position = {"entry": entry, "peak": current}
                equity_curve.append(equity)
                continue

            position["peak"] = max(position["peak"], current)
            return_pct = (current / position["entry"] - 1) * 100
            drawdown_pct = (current / position["peak"] - 1) * 100
            peak_return_pct = (position["peak"] / position["entry"] - 1) * 100
            exit_reason = ""
            if return_pct <= -abs(stop_loss_pct):
                exit_reason = "fixed_stop"
            elif peak_return_pct >= trailing_activation_pct and drawdown_pct <= -abs(trailing_stop_pct):
                exit_reason = "trailing_stop"
            elif profile.get("sma_dead_cross"):
                exit_reason = "dead_cross"
            if exit_reason:
                exit_price = current * (1 - cost_rate)
                pnl = exit_price / position["entry"] - 1
                equity *= 1 + pnl
                equity_curve.append(equity)
                fold_trades.append({"pnl": pnl, "reason": exit_reason})
                all_pnls.append(pnl)
                position = None
            else:
                equity_curve.append(equity)

        if position is not None:
            exit_price = float(prices[fold_end - 1]) * (1 - cost_rate)
            pnl = exit_price / position["entry"] - 1
            equity *= 1 + pnl
            fold_trades.append({"pnl": pnl, "reason": "fold_end"})
            all_pnls.append(pnl)
        results.append({
            "fold": fold_index + 1,
            "trade_count": len(fold_trades),
            "return_pct": round((equity / fold_start_equity - 1) * 100, 2),
        })

    wins = [pnl for pnl in all_pnls if pnl > 0]
    losses = [pnl for pnl in all_pnls if pnl < 0]
    peak = equity_curve[0]
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    profit_factor = sum(wins) / abs(sum(losses)) if losses else (999.0 if wins else 0.0)
    positive_folds = sum(1 for row in results if row["return_pct"] > 0)
    metrics = {
        "trade_count": len(all_pnls),
        "win_rate": round(len(wins) / len(all_pnls), 3) if all_pnls else 0.0,
        "profit_factor": round(profit_factor, 2),
        "total_return_pct": round((equity - 1) * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "positive_fold_ratio": round(positive_folds / len(results), 3) if results else 0.0,
    }
    passed = (
        metrics["trade_count"] >= 3
        and metrics["profit_factor"] >= 1.05
        and metrics["max_drawdown_pct"] <= 20
        and metrics["positive_fold_ratio"] >= 0.5
    )
    return {
        "ok": True,
        "passed": passed,
        "metrics": metrics,
        "folds": results,
        "costs": {"round_trip_bps": cost_bps * 2, "modeled": True},
    }
