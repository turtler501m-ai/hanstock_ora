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


def trade_value_surge(
    prices: list[float],
    volumes: list[float],
    *,
    window: int = 20,
    minimum_ratio: float = 1.5,
) -> dict:
    """Measure latest traded value against the preceding window average."""
    size = min(len(prices), len(volumes))
    if size < window + 1:
        return {"matched": False, "ratio": 0.0, "current": 0.0, "average": 0.0}
    values = [
        max(0.0, float(prices[index] or 0)) * max(0.0, float(volumes[index] or 0))
        for index in range(size)
    ]
    average = sum(values[-(window + 1):-1]) / window
    current = values[-1]
    ratio = current / average if average > 0 else 0.0
    return {
        "matched": ratio >= max(1.0, float(minimum_ratio or 1.0)),
        "ratio": round(ratio, 3),
        "current": round(current, 2),
        "average": round(average, 2),
    }


def first_wave_pullback(
    prices: list[float],
    volumes: list[float],
    *,
    lookback: int = 40,
    minimum_wave_pct: float = 12.0,
    minimum_pullback_pct: float = 3.0,
    maximum_pullback_pct: float = 12.0,
) -> dict:
    """Detect a first impulse, controlled pullback, volume contraction and rebound."""
    if len(prices) < lookback + 1:
        return {"matched": False, "wave_pct": 0.0, "pullback_pct": 0.0, "volume_contraction": 0.0}
    window_prices = [float(value or 0) for value in prices[-(lookback + 1):]]
    if any(value <= 0 for value in window_prices):
        return {"matched": False, "wave_pct": 0.0, "pullback_pct": 0.0, "volume_contraction": 0.0}

    # Reserve the last two bars for pullback/rebound confirmation.
    peak_index = max(range(5, len(window_prices) - 2), key=window_prices.__getitem__)
    base = min(window_prices[:peak_index])
    peak = window_prices[peak_index]
    current = window_prices[-1]
    previous = window_prices[-2]
    wave_pct = (peak / base - 1) * 100 if base > 0 else 0.0
    pullback_pct = (1 - current / peak) * 100 if peak > 0 else 0.0

    volume_contraction = 0.0
    if len(volumes) >= lookback + 1:
        window_volumes = [max(0.0, float(value or 0)) for value in volumes[-(lookback + 1):]]
        impulse_start = max(0, peak_index - 5)
        impulse = window_volumes[impulse_start:peak_index + 1]
        pullback = window_volumes[peak_index + 1:]
        impulse_average = sum(impulse) / len(impulse) if impulse else 0.0
        pullback_average = sum(pullback) / len(pullback) if pullback else 0.0
        volume_contraction = pullback_average / impulse_average if impulse_average > 0 else 0.0

    matched = (
        wave_pct >= minimum_wave_pct
        and minimum_pullback_pct <= pullback_pct <= maximum_pullback_pct
        and current > previous
        and 0 < volume_contraction <= 0.8
    )
    return {
        "matched": matched,
        "wave_pct": round(wave_pct, 2),
        "pullback_pct": round(pullback_pct, 2),
        "volume_contraction": round(volume_contraction, 3),
        "base_price": round(base, 4),
        "peak_price": round(peak, 4),
    }
