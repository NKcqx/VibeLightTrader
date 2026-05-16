"""Unit tests for `_format_brief_kind` — auto-derived brief card title.

Anchored to US market hours (9:30 ET open / 16:00 ET close). The cron jobs
should keep their familiar brand labels because they fire at the matching
ET clock times; ad-hoc / off-hour triggers should get an honest label that
reflects when they actually fired.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vibe_trader.scheduler.jobs import _format_brief_kind


# All UTC times below are picked so that the corresponding ET wall-clock is
# unambiguous regardless of DST (May 2026 is in EDT, UTC-4).
_BASE_DATE = (2026, 5, 4)  # arbitrary weekday in EDT


def _utc(h: int, m: int = 0) -> datetime:
    """Build a UTC datetime on 2026-05-04 at H:M (UTC)."""
    return datetime(*_BASE_DATE, h, m, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "now_utc, expected",
    [
        # ── Pre-market ──────────────────────────────────────────────────
        # ET 09:00 = UTC 13:00 → 30 min before open → "盘前 30min 快照"
        (_utc(13, 0), "盘前 30min 快照"),
        # ET 08:00 = UTC 12:00 → 90 min before open → "盘前快照"
        (_utc(12, 0), "盘前快照"),
        # ── Intraday (within first hour) ────────────────────────────────
        # ET 09:45 = UTC 13:45 → 15 min after open → "开盘后 15min 盘点"
        (_utc(13, 45), "开盘后 15min 盘点"),
        # ── Intraday (≥ 1 hour after open) ──────────────────────────────
        # ET 10:30 = UTC 14:30 → exactly 1.0h → cron morning_brief slot
        (_utc(14, 30), "开盘后 1.0h 盘点"),
        # ET 12:30 = UTC 16:30 → 3.0h after open → "开盘后 3.0h 盘点"
        (_utc(16, 30), "开盘后 3.0h 盘点"),
        # ── Within 1h after close: keep "收盘盘点" brand ─────────────────
        # ET 16:00 = UTC 20:00 → exactly at close → "收盘盘点"
        (_utc(20, 0), "收盘盘点"),
        # ET 16:30 = UTC 20:30 → 30 min after close → cron closing_brief slot
        (_utc(20, 30), "收盘盘点"),
        # ── Late post-market ────────────────────────────────────────────
        # ET 18:30 = UTC 22:30 → 2.5h after close → "收盘后 2.5h 盘点"
        (_utc(22, 30), "收盘后 2.5h 盘点"),
    ],
)
def test_format_brief_kind_all_branches(now_utc: datetime, expected: str) -> None:
    assert _format_brief_kind(now_utc) == expected


def test_cron_morning_slot_yields_canonical_brand() -> None:
    """The cron-scheduled morning_brief at ET 10:30 must keep its brand."""
    assert _format_brief_kind(_utc(14, 30)) == "开盘后 1.0h 盘点"


def test_cron_closing_slot_yields_canonical_brand() -> None:
    """The cron-scheduled closing_brief at ET 16:30 must keep its brand."""
    assert _format_brief_kind(_utc(20, 30)) == "收盘盘点"


def test_manual_off_hour_trigger_no_longer_misleads() -> None:
    """The bug the user reported: ET 12:39 should NOT say "开盘后1h盘点"."""
    # ET 12:39 = UTC 16:39 → ~3.15h after open
    label = _format_brief_kind(_utc(16, 39))
    assert "开盘后1h盘点" not in label
    assert "1.0h" not in label  # critical: must not pretend it's been only 1h
    assert label.startswith("开盘后")
    assert "3.1h" in label or "3.2h" in label  # rounding tolerance
