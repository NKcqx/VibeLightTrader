from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from vibe_trader.signals.base import Severity, Signal


def deduplicate(
    signals: Iterable[Signal],
    *,
    existing_keys: set[tuple[str, str, datetime]] | None = None,
    window_minutes: int = 60,
) -> list[Signal]:
    """Remove duplicates of (code, signal_type) within `window_minutes` window.

    Bucket key = ts truncated to a `window_minutes` slot starting at the hour.
    `existing_keys` lets caller pass keys already in DB to also dedupe across
    runs. Order of unique signals is preserved.
    """
    seen: set[tuple[str, str, datetime]] = set(existing_keys or set())
    out: list[Signal] = []
    for sig in signals:
        bucket = sig.ts.replace(
            minute=(sig.ts.minute // window_minutes) * window_minutes,
            second=0,
            microsecond=0,
        )
        key = (sig.code, sig.signal_type, bucket)
        if key in seen:
            continue
        seen.add(key)
        out.append(sig)
    return out


def split_by_severity(
    signals: Iterable[Signal],
) -> tuple[list[Signal], list[Signal], list[Signal]]:
    """Return (critical, warn, info)."""
    crit: list[Signal] = []
    warn: list[Signal] = []
    info: list[Signal] = []
    for s in signals:
        if s.severity is Severity.CRITICAL:
            crit.append(s)
        elif s.severity is Severity.WARN:
            warn.append(s)
        else:
            info.append(s)
    return crit, warn, info


__all__ = [
    "deduplicate",
    "split_by_severity",
]
