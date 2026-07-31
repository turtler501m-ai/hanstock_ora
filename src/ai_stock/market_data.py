# -*- coding: utf-8 -*-
"""시장 데이터 제공자 인터페이스 (§3 시장 어댑터, §4.6/4.8).

기본 구현은 best-effort로, 데이터가 없으면 빈 값/None을 반환한다(네트워크 없이도
동작; discovery는 데이터 부족 시 insufficient_data 후보를 만든다). 테스트는 provider를
주입해 결정적으로 검증한다(§17 외부 API mock).
"""
from __future__ import annotations

from typing import Any, Protocol

from src.ai_stock.constants import (
    INSTRUMENT_ETF,
    INSTRUMENT_STOCK,
    MARKET_KR,
    MARKET_US,
)


class MarketDataProvider(Protocol):
    def universe_items(self, market: str) -> list[dict[str, Any]]: ...
    def quote(self, market: str, symbol: str) -> dict[str, Any] | None: ...
    def daily_series(self, market: str, symbol: str) -> list[float] | None: ...
    def index_series(self, market: str) -> dict[str, list[float]]: ...


class DefaultProvider:
    """기존 소스 기반 best-effort 제공자. 가격/지표는 없으면 None."""

    def universe_items(self, market: str) -> list[dict[str, Any]]:
        if market == MARKET_KR:
            return self._kr_items()
        if market == MARKET_US:
            return self._us_items()
        return []

    def _kr_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            from src.db.repository import connect_db
            import sqlite3

            conn = connect_db()
            conn.row_factory = sqlite3.Row
            with conn:
                rows = conn.execute("SELECT symbol, name FROM watchlist").fetchall()
            for r in rows:
                items.append({
                    "symbol": str(r["symbol"]),
                    "name": str(r["name"] or r["symbol"]),
                    "instrument_type": INSTRUMENT_STOCK,
                    "market_cap": None,
                    "avg_trading_value": None,
                    "price": None,
                })
        except Exception:
            pass
        return items

    def _us_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            from src.mistock.config import MistockConfig

            cfg = MistockConfig()
            symbols = list(getattr(cfg, "universe_list", []) or [])
            for s in symbols[:50]:
                items.append({
                    "symbol": s,
                    "name": s,
                    "instrument_type": INSTRUMENT_ETF if s in _US_ETFS else INSTRUMENT_STOCK,
                    "market_cap": None,
                    "avg_trading_value": None,
                    "price": None,
                })
        except Exception:
            pass
        return items

    def quote(self, market: str, symbol: str) -> dict[str, Any] | None:
        series = self.daily_series(market, symbol)
        if not series:
            return None
        price = series[-1]
        change = None
        if len(series) >= 2 and series[-2]:
            change = round((series[-1] / series[-2] - 1.0) * 100.0, 2)
        from src.ai_stock.freshness import now

        return {"price": price, "change_pct": change, "data_as_of": now().isoformat()}

    def daily_series(self, market: str, symbol: str) -> list[float] | None:
        if market == MARKET_KR:
            return self._kr_series(symbol)
        if market == MARKET_US:
            return self._us_series(symbol)
        return None

    @staticmethod
    def _kr_series(symbol: str) -> list[float] | None:
        try:
            from src.db.market_repository import load_daily_charts

            charts = load_daily_charts(symbol)
            closes = [float(c["close"]) for c in charts if c.get("close") is not None]
            return closes or None
        except Exception:
            return None

    @staticmethod
    def _us_series(symbol: str) -> list[float] | None:
        # 미스톡 yfinance 소스 재사용(§2.4). online 차단/오류 시 None.
        try:
            from src.mistock.strategy import fetch_history

            hist = fetch_history(symbol, period="6mo")
            closes = [float(c) for c in hist.get("close", []) if c is not None]
            return closes or None
        except Exception:
            return None

    def index_series(self, market: str) -> dict[str, list[float]]:
        idx = {MARKET_KR: ("^KS11", "KOSPI"), MARKET_US: ("^GSPC", "S&P500")}.get(market)
        if idx:
            try:
                from src.mistock.strategy import fetch_history

                # The autonomy regime classifier requires at least 200 market
                # observations. Six calendar months cannot provide that many
                # trading sessions, so request a full year.
                hist = fetch_history(idx[0], period="1y")
                closes = [float(c) for c in hist.get("close", []) if c is not None]
                if len(closes) >= 21:
                    return {idx[1]: closes}
            except Exception:
                pass
        # KR fallback: daily_charts ETF proxy(069500=KODEX200)
        if market == MARKET_KR:
            for code in ("069500", "KOSPI", "0001"):
                s = self._kr_series(code)
                if s and len(s) >= 21:
                    return {"KOSPI": s}
        return {}


_US_ETFS = {"SPY", "QQQ", "DIA", "IWM", "VTI", "XLK", "XLF", "XLE", "SOXX", "SMH"}

_provider: MarketDataProvider = DefaultProvider()


def get_provider() -> MarketDataProvider:
    return _provider


def set_provider(provider: MarketDataProvider) -> None:
    """테스트/실데이터 연결용 provider 교체."""
    global _provider
    _provider = provider
