from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache

from src.utils.logger import logger


@lru_cache(maxsize=2)
def _calendar(calendar_name: str):
    import exchange_calendars

    return exchange_calendars.get_calendar(calendar_name)


def is_market_session(market: str, value: date | datetime) -> bool:
    """Return whether *value* is an exchange trading session.

    The weekday fallback keeps local tools usable before dependencies are
    installed. Production installs exchange_calendars from requirements.txt.
    """
    session_date = value.date() if isinstance(value, datetime) else value
    calendar_name = "XNYS" if str(market).upper() == "US" else "XKRX"
    try:
        return bool(_calendar(calendar_name).is_session(session_date.isoformat()))
    except (ImportError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            f"[MARKET CALENDAR] {calendar_name} unavailable; "
            f"falling back to weekday check: {exc}"
        )
        return session_date.weekday() < 5
