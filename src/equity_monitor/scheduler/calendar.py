from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal


_NYSE = "NYSE"
_TZ_ET = ZoneInfo("America/New_York")


@lru_cache(maxsize=1)
def _calendar():
    return mcal.get_calendar(_NYSE)


def is_trading_day(d: date) -> bool:
    """True if NYSE was/will be open at all on the calendar day `d`."""
    sched = _calendar().schedule(start_date=d, end_date=d)
    return not sched.empty


def is_market_open_at(when_utc: datetime) -> bool:
    """True if NYSE is open at the given UTC datetime (handles DST + holidays)."""
    when_et = when_utc.astimezone(_TZ_ET)
    sched = _calendar().schedule(
        start_date=when_et.date(), end_date=when_et.date()
    )
    if sched.empty:
        return False
    open_ts = sched.iloc[0]["market_open"].to_pydatetime()
    close_ts = sched.iloc[0]["market_close"].to_pydatetime()
    return open_ts <= when_utc <= close_ts


def early_close(d: date) -> datetime | None:
    """Return early-close datetime (UTC) on shortened sessions, else None.

    NYSE early close days (Black Friday, day before Independence/Christmas Day)
    close at 13:00 ET instead of the normal 16:00 ET.
    """
    sched = _calendar().schedule(start_date=d, end_date=d)
    if sched.empty:
        return None
    close_ts = sched.iloc[0]["market_close"].to_pydatetime()
    et_close = close_ts.astimezone(_TZ_ET)
    if et_close.hour < 16:
        return close_ts
    return None
