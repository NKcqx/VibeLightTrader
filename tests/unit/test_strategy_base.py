"""Unit tests for the strategy abstraction layer (C1).

Covers:
  - Registry: register / build / unknown-name error path.
  - RuleStrategy is byte-equivalent to the original strategy_lite.decide_action
    on the existing 5-rule matrix (regression guard for C1's "zero behaviour
    change" promise).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from equity_monitor.signals.base import Severity, Signal
from equity_monitor.signals.strategy_base import (
    StrategyContext,
    build_strategy,
    register_strategy,
    registered_strategies,
)
from equity_monitor.signals.strategy_lite import SignalSuggest, decide_action
from equity_monitor.signals.strategy_rule import RuleStrategy  # ensures registration


def _sig(signal_type: str, severity: Severity = Severity.WARN, **payload) -> Signal:
    return Signal(
        code="US.AAPL",
        ts=datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc),
        signal_type=signal_type,
        severity=severity,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Registry mechanics
# ---------------------------------------------------------------------------


def test_rule_is_registered_on_import() -> None:
    """Importing strategy_rule must auto-register `rule` (used by jobs.py)."""
    assert "rule" in registered_strategies()


def test_build_strategy_returns_a_concrete_rule_strategy() -> None:
    s = build_strategy("rule", {"max_position_per_symbol": 150})
    assert isinstance(s, RuleStrategy)
    assert s.max_position_per_symbol == 150
    assert s.name == "rule"


def test_build_unknown_strategy_raises_with_available_names() -> None:
    with pytest.raises(KeyError, match="unknown strategy 'foobar'"):
        build_strategy("foobar", {})


def test_register_duplicate_name_raises() -> None:
    @register_strategy("dup_test_x")
    def _f(_):
        return RuleStrategy(name="dup_test_x")

    with pytest.raises(ValueError, match="already registered"):

        @register_strategy("dup_test_x")
        def _g(_):  # pragma: no cover - registration fails before call
            return RuleStrategy(name="dup_test_x")


# ---------------------------------------------------------------------------
# RuleStrategy ≡ strategy_lite.decide_action — regression guard for C1.
# Each case below is one rule path; if any drift, C1 silently broke parity.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signals, current_qty",
    [
        # Rule 1: lower threshold → BUY
        ([_sig("threshold_breach_lower", Severity.CRITICAL)], 0),
        # Rule 1 saturated → HOLD
        ([_sig("threshold_breach_lower", Severity.CRITICAL)], 200),
        # Rule 2: upper threshold + position → SELL all
        ([_sig("threshold_breach_upper", Severity.CRITICAL)], 50),
        # Rule 2 with no position → None (silent)
        ([_sig("threshold_breach_upper", Severity.CRITICAL)], 0),
        # Rule 3: oversold + golden cross → BUY warn_size
        (
            [
                _sig("rsi_oversold", Severity.WARN, rsi=25.0),
                _sig("macd_golden_cross", Severity.INFO),
            ],
            0,
        ),
        # Rule 4: overbought + death cross with position → SELL warn_size
        (
            [
                _sig("rsi_overbought", Severity.WARN, rsi=75.0),
                _sig("macd_death_cross", Severity.WARN),
            ],
            100,
        ),
        # Rule 5: lower band break + extreme RSI → HOLD
        (
            [
                _sig("boll_lower_break", Severity.INFO),
                _sig("rsi_oversold", Severity.WARN, rsi=22.0),
            ],
            0,
        ),
        # No rule fires → None
        ([_sig("macd_golden_cross", Severity.INFO)], 0),
        # Empty input → None
        ([], 0),
    ],
)
def test_rule_strategy_matches_strategy_lite(signals, current_qty) -> None:
    rule = RuleStrategy()
    ctx = StrategyContext(
        code="US.AAPL", signals=signals, position_qty=current_qty
    )

    expected = decide_action(signals, current_qty=current_qty)
    actual = rule.decide(ctx)

    assert actual == expected, (
        f"RuleStrategy diverged from strategy_lite for "
        f"signals={[s.signal_type for s in signals]} qty={current_qty}: "
        f"expected={expected}, actual={actual}"
    )


def test_rule_strategy_respects_custom_knobs() -> None:
    """Custom max_position / sizes flow through to the underlying decide_action."""
    rule = RuleStrategy(max_position_per_symbol=80, critical_size=30)
    ctx = StrategyContext(
        code="US.AAPL",
        signals=[_sig("threshold_breach_lower", Severity.CRITICAL)],
        position_qty=60,
    )
    actual = rule.decide(ctx)
    assert isinstance(actual, SignalSuggest)
    assert actual.action == "BUY"
    assert actual.qty == 20  # capacity = 80 - 60 = 20, capped under critical_size=30
