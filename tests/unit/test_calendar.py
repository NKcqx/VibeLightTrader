from __future__ import annotations

from datetime import date, datetime, timezone

from vibe_trader.scheduler.calendar import (
    early_close,
    is_market_open_at,
    is_trading_day,
)


def test_weekend_not_trading() -> None:
    """2026-05-02 is a Saturday."""
    assert is_trading_day(date(2026, 5, 2)) is False


def test_normal_weekday_is_trading() -> None:
    """2026-05-04 is a Monday."""
    assert is_trading_day(date(2026, 5, 4)) is True


def test_christmas_day_not_trading() -> None:
    assert is_trading_day(date(2026, 12, 25)) is False


def test_independence_day_2026_observed() -> None:
    """July 4, 2026 is a Saturday → NYSE observes holiday on Friday July 3."""
    assert is_trading_day(date(2026, 7, 3)) is False


def test_market_open_during_session() -> None:
    """2026-05-04 14:00 UTC = 10:00 ET → market open."""
    when = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    assert is_market_open_at(when) is True


def test_market_closed_before_open() -> None:
    """2026-05-04 12:00 UTC = 08:00 ET → before open."""
    when = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    assert is_market_open_at(when) is False


def test_market_closed_after_close() -> None:
    """2026-05-04 21:00 UTC = 17:00 ET → after close."""
    when = datetime(2026, 5, 4, 21, 0, tzinfo=timezone.utc)
    assert is_market_open_at(when) is False


def test_market_closed_on_weekend_query() -> None:
    when = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
    assert is_market_open_at(when) is False


def test_black_friday_early_close() -> None:
    """2026-11-27 is Black Friday → early close at 13:00 ET."""
    ec = early_close(date(2026, 11, 27))
    assert ec is not None


def test_normal_day_no_early_close() -> None:
    assert early_close(date(2026, 5, 4)) is None


def test_weekend_no_early_close() -> None:
    assert early_close(date(2026, 5, 2)) is None
