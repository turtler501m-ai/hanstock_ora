from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests


KIND_LISTED_COMPANY_URL = (
    "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
)
NAVER_ITEM_URL = "https://finance.naver.com/item/main.naver?code={symbol}"
EXTRA_ETP_SYMBOLS = {
    "0151S0",
    "0162Z0",
    "069500",
    "102110",
    "114800",
    "122630",
    "123310",
    "133690",
    "148020",
    "152100",
    "157490",
    "229200",
    "251340",
    "252670",
    "252710",
    "261240",
    "273130",
    "278530",
    "305720",
    "360750",
    "379800",
    "381170",
    "396500",
    "448290",
    "481190",
    "494840",
}
ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / "config" / "kr_stock_metadata.json"
THEME_MAP_PATH = ROOT / "config" / "theme_map.json"


def _normalize_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit():
        return text.zfill(6)
    return text


def _load_theme_map() -> dict[str, list[dict]]:
    try:
        data = json.loads(THEME_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_static_metadata() -> tuple[dict[str, str], dict[str, str]]:
    try:
        from src.strategy.seven_split import STOCK_NAMES, STOCK_SECTORS
    except Exception:
        return {}, {}
    return dict(STOCK_NAMES), dict(STOCK_SECTORS)


def _fetch_naver_item_name(session: requests.Session, symbol: str) -> str | None:
    response = session.get(NAVER_ITEM_URL.format(symbol=symbol), timeout=10)
    response.raise_for_status()
    text = response.content.decode("utf-8", errors="replace")
    match = re.search(
        r'<div class="wrap_company">.*?<h2>\s*<a[^>]*>(.*?)</a>',
        text,
        re.S,
    )
    if match is None:
        match = re.search(r"<title>\s*([^:<]+?)\s*[:<]", text)
    if match is None:
        return None
    return re.sub(r"<.*?>", "", match.group(1)).strip() or None


def build_metadata() -> dict:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 hanstock-metadata-updater"})

    response = session.get(KIND_LISTED_COMPANY_URL, timeout=30)
    response.raise_for_status()
    html = response.content.decode("cp949")
    frame = pd.read_html(StringIO(html), header=0, flavor="lxml")[0]

    theme_by_symbol: dict[str, list[str]] = {}
    theme_names: dict[str, str] = {}
    for theme, stocks in _load_theme_map().items():
        if not isinstance(stocks, list):
            continue
        for stock in stocks:
            if not isinstance(stock, dict):
                continue
            symbol = _normalize_symbol(stock.get("ticker") or stock.get("symbol"))
            if not symbol:
                continue
            theme_by_symbol.setdefault(symbol, [])
            if str(theme) not in theme_by_symbol[symbol]:
                theme_by_symbol[symbol].append(str(theme))
            name = str(stock.get("name") or "").strip()
            if name:
                theme_names.setdefault(symbol, name)

    static_names, static_sectors = _load_static_metadata()
    symbols: dict[str, dict] = {}

    for row in frame.to_dict("records"):
        symbol = _normalize_symbol(row.get("종목코드"))
        if not symbol:
            continue
        themes = theme_by_symbol.get(symbol, [])
        krx_sector = str(row.get("업종") or "").strip()
        symbols[symbol] = {
            "symbol": symbol,
            "name": str(row.get("회사명") or "").strip(),
            "market": str(row.get("시장구분") or "").strip(),
            "sector": themes[0] if themes else static_sectors.get(symbol, krx_sector),
            "themes": themes,
            "krx_industry": krx_sector,
            "source": "krx_kind",
        }

    for symbol, name in {**theme_names, **static_names}.items():
        normalized = _normalize_symbol(symbol)
        if not normalized:
            continue
        item = symbols.setdefault(
            normalized,
            {
                "symbol": normalized,
                "name": str(name or normalized).strip(),
                "market": "",
                "sector": "",
                "themes": [],
                "krx_industry": "",
                "source": "static_supplement",
            },
        )
        if not item.get("name") or item.get("name") == normalized:
            item["name"] = str(name or normalized).strip()
        themes = theme_by_symbol.get(normalized, [])
        if themes:
            item["themes"] = themes
            item["sector"] = themes[0]
        elif static_sectors.get(normalized) and not item.get("sector"):
            item["sector"] = static_sectors[normalized]

    for symbol in sorted(EXTRA_ETP_SYMBOLS):
        normalized = _normalize_symbol(symbol)
        if not normalized or normalized in symbols:
            continue
        try:
            name = _fetch_naver_item_name(session, normalized)
        except requests.RequestException:
            name = None
        if not name:
            continue
        symbols[normalized] = {
            "symbol": normalized,
            "name": name,
            "market": "ETP",
            "sector": static_sectors.get(normalized, "ETF/ETN"),
            "themes": theme_by_symbol.get(normalized, []),
            "krx_industry": "",
            "source": "naver_finance_supplement",
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": [
            {
                "name": "KRX KIND listed company list",
                "url": KIND_LISTED_COMPANY_URL,
            },
            {
                "name": "config/theme_map.json",
                "url": "config/theme_map.json",
            },
            {
                "name": "src.strategy.seven_split static supplements",
                "url": "src/strategy/seven_split.py",
            },
            {
                "name": "Naver Finance ETF/ETN supplements",
                "url": "https://finance.naver.com/item/main.naver",
            },
        ],
        "symbols": dict(sorted(symbols.items())),
    }


def main() -> int:
    payload = build_metadata()
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT_PATH} ({len(payload['symbols'])} symbols)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
