"""Deterministic technical signals shared by Hanstock and Mistock."""

from __future__ import annotations

from src.strategy.indicators import calc_sma


def moving_average_cross(
    prices: list[float],
    *,
    short_period: int = 20,
    long_period: int = 60,
) -> dict:
    """Return current alignment and a fresh short/long moving-average cross."""
    if len(prices) < long_period + 1:
        return {
            "short_sma": calc_sma(prices, short_period),
            "long_sma": calc_sma(prices, long_period),
            "golden_cross": False,
            "dead_cross": False,
        }

    short_now = calc_sma(prices, short_period)
    long_now = calc_sma(prices, long_period)
    short_prev = calc_sma(prices[:-1], short_period)
    long_prev = calc_sma(prices[:-1], long_period)
    return {
        "short_sma": short_now,
        "long_sma": long_now,
        "golden_cross": short_prev <= long_prev and short_now > long_now,
        "dead_cross": short_prev >= long_prev and short_now < long_now,
    }


def trailing_stop_signal(
    *,
    current_price: float,
    return_pct: float,
    recent_highs: list[float],
    activation_pct: float,
    trail_pct: float,
    lookback: int = 20,
) -> dict:
    """Evaluate a trailing stop after the inferred peak return reached activation.

    Entry price is inferred from the broker-provided current return. The stop only
    activates while the position is still non-negative, preventing an old chart
    high from replacing the ordinary fixed stop-loss path.
    """
    current = float(current_price or 0)
    position_return = float(return_pct or 0)
    activation = max(0.0, float(activation_pct or 0))
    distance = max(0.1, float(trail_pct or 0))
    window = max(1, int(lookback or 1))
    highs = [float(value) for value in recent_highs[-window:] if float(value or 0) > 0]
    result = {
        "triggered": False,
        "peak_price": current,
        "peak_return_pct": position_return,
        "drawdown_pct": 0.0,
        "stop_price": 0.0,
    }
    if current <= 0 or position_return <= -99.9 or not highs:
        return result

    entry_price = current / (1 + position_return / 100)
    peak_price = max(max(highs), current)
    peak_return = (peak_price / entry_price - 1) * 100 if entry_price > 0 else position_return
    drawdown = (current / peak_price - 1) * 100 if peak_price > 0 else 0.0
    stop_price = peak_price * (1 - distance / 100)
    result.update({
        "peak_price": round(peak_price, 4),
        "peak_return_pct": round(peak_return, 2),
        "drawdown_pct": round(drawdown, 2),
        "stop_price": round(stop_price, 4),
    })
    result["triggered"] = (
        position_return >= 0
        and peak_return >= activation
        and current <= stop_price
    )
    return result
