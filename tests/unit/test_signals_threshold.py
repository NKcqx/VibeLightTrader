from __future__ import annotations

from datetime import datetime

from equity_monitor.signals.base import Severity
from equity_monitor.signals.threshold import detect_threshold_breach


def test_upper_breach() -> None:
    out = detect_threshold_breach(
        code="US.AAPL",
        ts=datetime(2026, 5, 2, 14),
        close=205.0,
        upper=200.0,
        lower=165.0,
    )
    assert len(out) == 1
    assert out[0].signal_type == "threshold_breach_upper"
    assert out[0].severity is Severity.CRITICAL
    assert out[0].payload == {"close": 205.0, "upper": 200.0}


def test_lower_breach() -> None:
    out = detect_threshold_breach(
        code="US.AAPL",
        ts=datetime(2026, 5, 2, 14),
        close=160.0,
        upper=200.0,
        lower=165.0,
    )
    assert len(out) == 1
    assert out[0].signal_type == "threshold_breach_lower"


def test_no_breach() -> None:
    out = detect_threshold_breach(
        code="US.AAPL",
        ts=datetime(2026, 5, 2, 14),
        close=180.0,
        upper=200.0,
        lower=165.0,
    )
    assert out == []


def test_thresholds_optional() -> None:
    out = detect_threshold_breach(
        code="US.TSLA",
        ts=datetime(2026, 5, 2, 14),
        close=180.0,
        upper=None,
        lower=None,
    )
    assert out == []


def test_breach_at_exact_upper_boundary() -> None:
    """close == upper should trigger (>= semantics)."""
    out = detect_threshold_breach(
        code="US.AAPL",
        ts=datetime(2026, 5, 2, 14),
        close=200.0,
        upper=200.0,
        lower=None,
    )
    assert len(out) == 1
    assert out[0].signal_type == "threshold_breach_upper"


def test_breach_at_exact_lower_boundary() -> None:
    out = detect_threshold_breach(
        code="US.AAPL",
        ts=datetime(2026, 5, 2, 14),
        close=165.0,
        upper=None,
        lower=165.0,
    )
    assert len(out) == 1
    assert out[0].signal_type == "threshold_breach_lower"
