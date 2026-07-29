from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any


METADATA_PATH = Path("config/kr_stock_metadata.json")
PLACEHOLDER_STOCK_NAMES = {
    "",
    "-",
    "Unknown",
    "알 수 없는 종목",
    "우량 종목",
}
PLACEHOLDER_SECTORS = {
    "",
    "-",
    "미분류",
    "Unknown",
}


def normalize_kr_symbol(symbol: Any) -> str:
    value = str(symbol or "").strip().upper()
    if value.isdigit() and len(value) < 6:
        return value.zfill(6)
    return value


def normalize_kr_order_symbol(symbol: Any) -> str:
    """Return the six-character KIS balance/order code for domestic instruments."""
    value = normalize_kr_symbol(symbol)
    if len(value) == 7 and value.startswith("Q") and value[1:].isdigit():
        return value[1:]
    return value


def is_placeholder_stock_name(name: Any, symbol: Any = "") -> bool:
    value = str(name or "").strip()
    return (
        value in PLACEHOLDER_STOCK_NAMES
        or bool(symbol and value == normalize_kr_symbol(symbol))
    )


def is_placeholder_sector(sector: Any) -> bool:
    return str(sector or "").strip() in PLACEHOLDER_SECTORS


@functools.lru_cache(maxsize=1)
def load_kr_stock_metadata() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for symbol, item in symbols.items():
        if not isinstance(item, dict):
            continue
        normalized = normalize_kr_symbol(symbol)
        result[normalized] = {**item, "symbol": normalized}
    return result


def resolve_stock_name(symbol: Any, fallback: Any = None) -> str:
    normalized = normalize_kr_symbol(symbol)
    item = load_kr_stock_metadata().get(normalized, {})
    metadata_name = str(item.get("name") or "").strip()
    if metadata_name and not is_placeholder_stock_name(metadata_name, normalized):
        return metadata_name
    fallback_name = str(fallback or "").strip()
    if fallback_name and not is_placeholder_stock_name(fallback_name, normalized):
        return fallback_name
    return normalized


def resolve_stock_sector(symbol: Any, fallback: Any = None) -> str:
    normalized = normalize_kr_symbol(symbol)
    item = load_kr_stock_metadata().get(normalized, {})
    metadata_sector = str(item.get("sector") or "").strip()
    if metadata_sector and not is_placeholder_sector(metadata_sector):
        return metadata_sector
    fallback_sector = str(fallback or "").strip()
    if fallback_sector and not is_placeholder_sector(fallback_sector):
        return fallback_sector
    return "미분류"
