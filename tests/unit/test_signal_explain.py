"""Unit tests for `reports.render.explain_signal`.

The user complained that "穿越上限阈值 (close=198.45, upper=150.0)" and
"RSI 超买 (rsi=71.35733884186854)" were unreadable. Two requirements:

  1. Format the numbers nicely (no 14-digit floats, prices as $X.XX).
  2. After every feature line, append a one-line meaning explaining
     what it means in plain Chinese.

Each test asserts both halves below.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vibe_trader.reports.render import explain_signal
from vibe_trader.signals.base import Severity, Signal


def _sig(signal_type: str, payload: dict | None = None) -> Signal:
    return Signal(
        code="US.AAPL",
        ts=datetime(2026, 5, 4, 14, tzinfo=timezone.utc),
        signal_type=signal_type,
        severity=Severity.WARN,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# Threshold breaches — the user's own price triggers.
# ---------------------------------------------------------------------------


def test_threshold_breach_upper_renders_prices_and_meaning() -> None:
    out = explain_signal(
        _sig("threshold_breach_upper", {"close": 198.45, "upper": 150.0})
    )
    # Feature line: nice prices, no raw payload dump
    assert "穿越上限阈值" in out
    assert "$198.45" in out
    assert "$150.00" in out
    assert "close=" not in out  # raw dict-dump must be gone
    # Meaning line: must explain "what does this mean"
    assert "止盈" in out or "减仓" in out


def test_threshold_breach_lower_renders_prices_and_meaning() -> None:
    out = explain_signal(
        _sig("threshold_breach_lower", {"close": 162.10, "lower": 165.0})
    )
    assert "穿越下限阈值" in out
    assert "$162.10" in out
    assert "$165.00" in out
    assert "lower=" not in out
    assert "加仓" in out or "抄底" in out


# ---------------------------------------------------------------------------
# RSI — the other format crime the user called out (long float tail).
# ---------------------------------------------------------------------------


def test_rsi_overbought_truncates_long_float_and_explains() -> None:
    out = explain_signal(
        _sig("rsi_overbought", {"rsi": 71.35733884186854, "close": 200.0})
    )
    assert "RSI 超买" in out
    assert "71.36" in out  # ≤ 2 decimals
    assert "71.35733" not in out  # full precision must NOT leak
    assert "高于 70" in out
    # Meaning must explain WHY RSI > 70 matters
    assert "回调" in out or "见顶" in out


def test_rsi_oversold_truncates_and_explains() -> None:
    out = explain_signal(_sig("rsi_oversold", {"rsi": 27.5, "close": 150.0}))
    assert "RSI 超卖" in out
    assert "27.50" in out
    assert "低于 30" in out
    assert "反弹" in out or "见底" in out


# ---------------------------------------------------------------------------
# MACD — payload-less signals.
# ---------------------------------------------------------------------------


def test_macd_golden_cross_explains_direction() -> None:
    out = explain_signal(_sig("macd_golden_cross", {}))
    assert "MACD 金叉" in out
    assert "上穿" in out
    assert "多头" in out


def test_macd_death_cross_explains_direction() -> None:
    out = explain_signal(_sig("macd_death_cross", {}))
    assert "MACD 死叉" in out
    assert "下穿" in out
    assert "空头" in out


# ---------------------------------------------------------------------------
# Bollinger band breaks — close + band level.
# ---------------------------------------------------------------------------


def test_boll_upper_break_renders_levels() -> None:
    out = explain_signal(
        _sig("boll_upper_break", {"close": 205.0, "boll_upper": 200.5})
    )
    assert "突破布林上轨" in out
    assert "$205.00" in out
    assert "$200.50" in out
    assert "+2σ" in out or "高位" in out


def test_boll_lower_break_renders_levels() -> None:
    out = explain_signal(
        _sig("boll_lower_break", {"close": 145.0, "boll_lower": 150.5})
    )
    assert "跌破布林下轨" in out
    assert "$145.00" in out
    assert "$150.50" in out
    assert "-2σ" in out or "低位" in out


# ---------------------------------------------------------------------------
# Robustness — payload missing fields / unknown signal_type.
# ---------------------------------------------------------------------------


def test_payload_missing_keys_render_n_a() -> None:
    """Missing payload['close'] / payload['upper'] must not crash."""
    out = explain_signal(_sig("threshold_breach_upper", {}))
    assert "穿越上限阈值" in out
    assert "n/a" in out  # graceful degrade


def test_unknown_signal_type_falls_back_to_raw_dump() -> None:
    """Unknown signal_type still surfaces both name and payload (fallback)."""
    out = explain_signal(
        _sig("brand_new_signal", {"foo": 1, "bar": 2.5})
    )
    assert "brand_new_signal" in out
    assert "foo=1" in out
    assert "bar=2.5" in out


# ---------------------------------------------------------------------------
# Format invariants — every output starts with the bold name and has a
# meaning marker. This is the contract render_signal_alert relies on.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stype",
    [
        "threshold_breach_upper",
        "threshold_breach_lower",
        "rsi_overbought",
        "rsi_oversold",
        "macd_golden_cross",
        "macd_death_cross",
        "boll_upper_break",
        "boll_lower_break",
    ],
)
def test_known_signal_types_have_two_line_format(stype: str) -> None:
    out = explain_signal(_sig(stype, {"close": 100, "upper": 90, "lower": 90,
                                       "rsi": 50, "boll_upper": 110, "boll_lower": 90}))
    assert out.startswith("**"), "feature line must be bolded"
    assert "↳" in out, "second line must use the explanation arrow"
    # Two-paragraph format: "<feature>\n  ↳ <meaning>"
    parts = out.split("\n", 1)
    assert len(parts) == 2
    assert parts[1].lstrip().startswith("↳")
