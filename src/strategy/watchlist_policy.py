"""관심종목 등록 및 AI 유니버스 공통 정책."""

from __future__ import annotations


MIN_WATCHLIST_PRICE = 5_000.0
MIN_WATCHLIST_MARKET_CAP = 300_000_000_000.0


def eligibility_reason(
    *,
    price: float | int | None,
    market_cap: float | int | None = None,
    known_mid_large: bool = False,
) -> str | None:
    current_price = float(price or 0)
    if current_price < MIN_WATCHLIST_PRICE:
        return f"현재가 {MIN_WATCHLIST_PRICE:,.0f}원 미만 종목은 관심종목에 등록할 수 없습니다."
    if market_cap is not None and float(market_cap or 0) > 0:
        if float(market_cap) < MIN_WATCHLIST_MARKET_CAP:
            return "중·대형주 기준(시가총액 3,000억원 이상)에 미달합니다."
    elif not known_mid_large:
        return "중·대형주 유니버스에 포함되지 않은 종목입니다."
    return None


def filter_registered_items(
    items: list[dict],
    registered_symbols: list[str] | set[str],
) -> list[dict]:
    registered = {str(symbol).strip() for symbol in registered_symbols if str(symbol).strip()}
    return [item for item in items if str(item.get("symbol") or "").strip() in registered]

