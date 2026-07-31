"""Persistent entry-aware peak tracking for trailing stops."""

from __future__ import annotations

from src.runtime_state import runtime_state_store


STATE_KEY = "technical_position_peaks_v1"


def update_position_peak(
    market: str,
    symbol: str,
    *,
    current_price: float,
    entry_price: float,
    quantity: float,
) -> dict:
    normalized_symbol = str(symbol or "").upper().strip()
    if not normalized_symbol:
        return {
            "market": str(market).upper(),
            "symbol": "",
            "entry_price": round(max(0.0, float(entry_price or 0)), 6),
            "quantity": round(max(0.0, float(quantity or 0)), 8),
            "peak_price": round(max(0.0, float(current_price or 0)), 6),
        }
    key = f"{str(market).upper()}:{normalized_symbol}"
    state = runtime_state_store.get(STATE_KEY, {"positions": {}})
    positions = state.setdefault("positions", {})
    current = max(0.0, float(current_price or 0))
    entry = max(0.0, float(entry_price or 0))
    qty = max(0.0, float(quantity or 0))
    if qty <= 0 or current <= 0:
        positions.pop(key, None)
        runtime_state_store.set(STATE_KEY, state)
        return {}

    previous = positions.get(key) or {}
    previous_entry = float(previous.get("entry_price") or 0)
    previous_qty = float(previous.get("quantity") or 0)
    position_changed = (
        previous_entry <= 0
        or abs(entry - previous_entry) / previous_entry > 0.005
        or qty > previous_qty + 1e-9
    )
    peak = current if position_changed else max(current, float(previous.get("peak_price") or current))
    row = {
        "market": str(market).upper(),
        "symbol": str(symbol).upper(),
        "entry_price": round(entry, 6),
        "quantity": round(qty, 8),
        "peak_price": round(peak, 6),
    }
    positions[key] = row
    runtime_state_store.set(STATE_KEY, state)
    return row


def clear_missing_positions(market: str, active_symbols: set[str]) -> None:
    prefix = f"{str(market).upper()}:"
    active = {str(symbol).upper() for symbol in active_symbols}
    state = runtime_state_store.get(STATE_KEY, {"positions": {}})
    positions = state.setdefault("positions", {})
    for key in list(positions):
        if key.startswith(prefix) and key[len(prefix):] not in active:
            positions.pop(key, None)
    runtime_state_store.set(STATE_KEY, state)
