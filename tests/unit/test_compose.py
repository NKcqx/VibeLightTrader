from __future__ import annotations

from datetime import datetime
from typing import Any

from vibe_trader.signals.base import Severity, Signal
from vibe_trader.signals.compose import (
    deduplicate,
    split_by_severity,
)


def _s(
    code: str,
    ts: datetime,
    signal_type: str,
    sev: Severity = Severity.WARN,
    payload: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        code=code, ts=ts, signal_type=signal_type, severity=sev, payload=payload or {}
    )


# ─────────────────────────────────── deduplicate ──────────────────────────────


def test_dedupe_same_bucket() -> None:
    a = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "rsi_overbought")
    b = _s("US.AAPL", datetime(2026, 5, 2, 14, 30), "rsi_overbought")
    c = _s("US.AAPL", datetime(2026, 5, 2, 15, 5), "rsi_overbought")
    out = deduplicate([a, b, c], window_minutes=60)
    assert len(out) == 2
    assert out[0] is a and out[1] is c


def test_dedupe_different_types_kept() -> None:
    a = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "rsi_overbought")
    b = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "macd_death_cross")
    out = deduplicate([a, b], window_minutes=60)
    assert len(out) == 2


def test_dedupe_different_codes_kept() -> None:
    a = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "rsi_overbought")
    b = _s("US.NVDA", datetime(2026, 5, 2, 14, 5), "rsi_overbought")
    out = deduplicate([a, b], window_minutes=60)
    assert len(out) == 2


def test_dedupe_existing_keys_carried_over() -> None:
    a = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "rsi_overbought")
    existing = {("US.AAPL", "rsi_overbought", datetime(2026, 5, 2, 14, 0))}
    out = deduplicate([a], existing_keys=existing, window_minutes=60)
    assert out == []


def test_dedupe_window_30_min_buckets() -> None:
    """With 30-min buckets, 14:05 and 14:25 share a bucket; 14:35 is a new one."""
    a = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "rsi_overbought")
    b = _s("US.AAPL", datetime(2026, 5, 2, 14, 25), "rsi_overbought")
    c = _s("US.AAPL", datetime(2026, 5, 2, 14, 35), "rsi_overbought")
    out = deduplicate([a, b, c], window_minutes=30)
    assert len(out) == 2
    assert out[0] is a and out[1] is c


def test_dedupe_preserves_input_order() -> None:
    """Stable order: first occurrence of each bucket is kept."""
    a = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "rsi_overbought")
    b = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "macd_death_cross")
    c = _s("US.AAPL", datetime(2026, 5, 2, 14, 5), "boll_upper_break")
    out = deduplicate([a, b, c], window_minutes=60)
    assert [s.signal_type for s in out] == [
        "rsi_overbought",
        "macd_death_cross",
        "boll_upper_break",
    ]


# ─────────────────────────────────── split_by_severity ────────────────────────


def test_split_by_severity() -> None:
    crit = _s("X", datetime(2026, 5, 2, 14), "x", sev=Severity.CRITICAL)
    warn = _s("X", datetime(2026, 5, 2, 14), "y", sev=Severity.WARN)
    info = _s("X", datetime(2026, 5, 2, 14), "z", sev=Severity.INFO)
    c, w, i = split_by_severity([crit, warn, info])
    assert c == [crit] and w == [warn] and i == [info]


def test_split_by_severity_empty() -> None:
    c, w, i = split_by_severity([])
    assert c == [] and w == [] and i == []
