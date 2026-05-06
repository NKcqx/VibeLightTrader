from __future__ import annotations

from unittest.mock import patch

import pytest

from vibe_trader.config import AppConfig, WatchlistConfig
from vibe_trader.futu_client import FakeFutuClient
from vibe_trader.scheduler.runner import _wrap_trading_day, build_scheduler


def test_build_scheduler_registers_four_jobs(
    app_cfg: AppConfig, watchlist: WatchlistConfig
) -> None:
    sched = build_scheduler(
        cfg=app_cfg,
        watchlist=watchlist,
        client_factory=lambda: FakeFutuClient(),
    )
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {
        "intraday_check",
        "morning_brief",
        "closing_brief",
        "news_pulse",
    }


def test_wrap_trading_day_skips_non_trading() -> None:
    """When NYSE is closed, wrapped fn returns None and is NOT called."""
    calls: list[int] = []

    def job() -> int:
        calls.append(1)
        return 42

    wrapped = _wrap_trading_day(job, tz_name="America/New_York")
    with patch("vibe_trader.scheduler.runner.is_trading_day", return_value=False):
        out = wrapped()
    assert out is None
    assert calls == []


def test_wrap_trading_day_runs_on_trading_day() -> None:
    """On trading days, the inner job runs and its return is forwarded."""
    calls: list[int] = []

    def job() -> int:
        calls.append(1)
        return 42

    wrapped = _wrap_trading_day(job, tz_name="America/New_York")
    with patch("vibe_trader.scheduler.runner.is_trading_day", return_value=True):
        out = wrapped()
    assert out == 42
    assert calls == [1]


def test_wrap_trading_day_swallows_inner_exceptions() -> None:
    """Inner exception is caught (logged) — APScheduler should NOT see it."""

    def bad() -> None:
        raise RuntimeError("boom")

    wrapped = _wrap_trading_day(bad, tz_name="America/New_York")
    with patch("vibe_trader.scheduler.runner.is_trading_day", return_value=True):
        wrapped()


def test_build_scheduler_uses_configured_timezone(
    app_cfg: AppConfig, watchlist: WatchlistConfig
) -> None:
    sched = build_scheduler(
        cfg=app_cfg,
        watchlist=watchlist,
        client_factory=lambda: FakeFutuClient(),
    )
    assert str(sched.timezone) == "America/New_York"
