"""관심종목 등록 및 AI 유니버스 공통 정책."""

from __future__ import annotations


MIN_WATCHLIST_PRICE = 5_000.0
MIN_WATCHLIST_MARKET_CAP = 300_000_000_000.0
DEFAULT_WATCHLIST_POLICY = {
    "enabled": True,
    "min_price": MIN_WATCHLIST_PRICE,
    "min_market_cap": MIN_WATCHLIST_MARKET_CAP,
    "require_mid_large_when_market_cap_unknown": True,
}


def _as_non_negative_float(value: object, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def normalize_watchlist_policy(policy: dict | None = None) -> dict:
    """Return a complete, type-safe policy while preserving stable defaults."""
    raw = policy if isinstance(policy, dict) else {}
    return {
        "enabled": _as_bool(raw.get("enabled"), True),
        "min_price": _as_non_negative_float(
            raw.get("min_price"),
            MIN_WATCHLIST_PRICE,
        ),
        "min_market_cap": _as_non_negative_float(
            raw.get("min_market_cap"),
            MIN_WATCHLIST_MARKET_CAP,
        ),
        "require_mid_large_when_market_cap_unknown": _as_bool(
            raw.get("require_mid_large_when_market_cap_unknown"),
            True,
        ),
    }


def eligibility_reason(
    *,
    price: float | int | None,
    market_cap: float | int | None = None,
    known_mid_large: bool = False,
    policy: dict | None = None,
) -> str | None:
    active_policy = normalize_watchlist_policy(policy)
    if not active_policy["enabled"]:
        return None

    min_price = active_policy["min_price"]
    current_price = float(price or 0)
    if min_price > 0 and current_price < min_price:
        return f"현재가 {min_price:,.0f}원 미만 종목은 관심종목에 등록할 수 없습니다."

    min_market_cap = active_policy["min_market_cap"]
    has_market_cap = market_cap is not None and float(market_cap or 0) > 0
    if min_market_cap > 0 and has_market_cap:
        if float(market_cap) < min_market_cap:
            market_cap_eok = min_market_cap / 100_000_000
            return f"시가총액 기준({market_cap_eok:,.0f}억원 이상)에 미달합니다."
    elif (
        min_market_cap > 0
        and active_policy["require_mid_large_when_market_cap_unknown"]
        and not known_mid_large
    ):
        return "시가총액 미수집 종목은 중대형주 정책 유니버스에 포함되어야 합니다."
    return None


def filter_registered_items(
    items: list[dict],
    registered_symbols: list[str] | set[str],
) -> list[dict]:
    registered = {str(symbol).strip() for symbol in registered_symbols if str(symbol).strip()}
    return [item for item in items if str(item.get("symbol") or "").strip() in registered]
