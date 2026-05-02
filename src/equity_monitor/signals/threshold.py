from __future__ import annotations

from datetime import datetime

from equity_monitor.signals.base import Severity, Signal


def detect_threshold_breach(
    *,
    code: str,
    ts: datetime,
    close: float,
    upper: float | None,
    lower: float | None,
) -> list[Signal]:
    """Emit CRITICAL signals when close >= upper or close <= lower.

    Either threshold can be None (skipped). Both can fire simultaneously only
    if upper <= lower, which would be a misconfiguration upstream.
    """
    out: list[Signal] = []
    if upper is not None and close >= upper:
        out.append(
            Signal(
                code=code,
                ts=ts,
                signal_type="threshold_breach_upper",
                severity=Severity.CRITICAL,
                payload={"close": close, "upper": upper},
            )
        )
    if lower is not None and close <= lower:
        out.append(
            Signal(
                code=code,
                ts=ts,
                signal_type="threshold_breach_lower",
                severity=Severity.CRITICAL,
                payload={"close": close, "lower": lower},
            )
        )
    return out
