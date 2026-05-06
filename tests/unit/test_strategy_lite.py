from __future__ import annotations

from datetime import datetime

from vibe_trader.signals.base import Severity, Signal
from vibe_trader.signals.strategy_lite import (
    decide_action,
    decide_actions_for_codes,
)


def _sig(stype: str, severity: Severity = Severity.WARN, **payload) -> Signal:
    return Signal(
        code="US.AAPL",
        ts=datetime(2026, 5, 2, 14, 0),
        signal_type=stype,
        severity=severity,
        payload=payload,
    )


def test_no_signals_returns_none() -> None:
    assert decide_action([]) is None


def test_unrecognized_signal_returns_none() -> None:
    assert decide_action([_sig("futu_capital_anomaly")]) is None


def test_threshold_breach_lower_buys_at_critical_size() -> None:
    out = decide_action(
        [_sig("threshold_breach_lower", Severity.CRITICAL)],
        current_qty=0,
        critical_size=100,
    )
    assert out is not None
    assert out.action == "BUY"
    assert out.qty == 100
    assert "支撑位" in out.reason
    assert "threshold_breach_lower" in out.triggering_signal_types


def test_threshold_breach_lower_caps_at_max_position() -> None:
    """Already 150 of 200 → can only add 50, not full critical_size 100."""
    out = decide_action(
        [_sig("threshold_breach_lower", Severity.CRITICAL)],
        current_qty=150,
        max_position_per_symbol=200,
        critical_size=100,
    )
    assert out is not None
    assert out.action == "BUY"
    assert out.qty == 50


def test_threshold_breach_lower_at_max_returns_hold() -> None:
    out = decide_action(
        [_sig("threshold_breach_lower", Severity.CRITICAL)],
        current_qty=200,
        max_position_per_symbol=200,
    )
    assert out is not None
    assert out.action == "HOLD"
    assert out.qty == 0
    assert "已满仓" in out.reason


def test_threshold_breach_upper_sells_all() -> None:
    out = decide_action(
        [_sig("threshold_breach_upper", Severity.CRITICAL)],
        current_qty=75,
    )
    assert out is not None
    assert out.action == "SELL"
    assert out.qty == 75
    assert "阻力位" in out.reason


def test_threshold_breach_upper_with_no_position_returns_none() -> None:
    """Can't sell what you don't have — no suggestion."""
    out = decide_action(
        [_sig("threshold_breach_upper", Severity.CRITICAL)],
        current_qty=0,
    )
    assert out is None


def test_rsi_oversold_plus_golden_cross_buys() -> None:
    out = decide_action(
        [_sig("rsi_oversold", rsi=25.0), _sig("macd_golden_cross", macd_hist=0.5)],
        current_qty=0,
        warn_size=50,
    )
    assert out is not None
    assert out.action == "BUY"
    assert out.qty == 50
    assert set(out.triggering_signal_types) == {"rsi_oversold", "macd_golden_cross"}


def test_rsi_oversold_alone_does_not_trigger_buy() -> None:
    """Need BOTH rsi_oversold AND macd_golden_cross to fire rule 3."""
    out = decide_action([_sig("rsi_oversold", rsi=25.0)])
    assert out is None


def test_macd_golden_cross_alone_does_not_trigger_buy() -> None:
    out = decide_action([_sig("macd_golden_cross", macd_hist=0.5)])
    assert out is None


def test_rsi_overbought_plus_death_cross_sells() -> None:
    out = decide_action(
        [_sig("rsi_overbought", rsi=80.0), _sig("macd_death_cross", macd_hist=-0.5)],
        current_qty=100,
        warn_size=50,
    )
    assert out is not None
    assert out.action == "SELL"
    assert out.qty == 50


def test_rsi_overbought_capped_by_current_qty() -> None:
    """If we hold less than warn_size, sell only what we have."""
    out = decide_action(
        [_sig("rsi_overbought", rsi=80.0), _sig("macd_death_cross", macd_hist=-0.5)],
        current_qty=20,
        warn_size=50,
    )
    assert out is not None
    assert out.action == "SELL"
    assert out.qty == 20


def test_rsi_overbought_with_no_position_returns_none() -> None:
    out = decide_action(
        [_sig("rsi_overbought", rsi=80.0), _sig("macd_death_cross", macd_hist=-0.5)],
        current_qty=0,
    )
    assert out is None


def test_boll_lower_break_with_extreme_rsi_returns_hold() -> None:
    out = decide_action(
        [
            _sig("boll_lower_break", lower=170.0, close=168.0),
            _sig("rsi_oversold", rsi=22.0),
        ],
        rsi_extreme=30.0,
    )
    assert out is not None
    assert out.action == "HOLD"
    assert out.qty == 0
    assert "观望" in out.reason


def test_boll_lower_break_without_rsi_extreme_no_suggestion() -> None:
    """rsi=35 above the 30 threshold → no HOLD signal."""
    out = decide_action(
        [
            _sig("boll_lower_break", lower=170.0, close=168.0),
            _sig("rsi_oversold", rsi=35.0),
        ],
        rsi_extreme=30.0,
    )
    assert out is None


def test_priority_threshold_lower_beats_rsi_combo() -> None:
    """Threshold breach (CRITICAL) wins over rsi_oversold+macd combo (WARN)."""
    out = decide_action(
        [
            _sig("threshold_breach_lower", Severity.CRITICAL),
            _sig("rsi_oversold", rsi=25.0),
            _sig("macd_golden_cross", macd_hist=0.5),
        ],
        current_qty=0,
    )
    assert out is not None
    assert out.action == "BUY"
    assert out.qty == 100  # critical_size, not warn_size


def test_decide_actions_for_codes_filters_no_decisions() -> None:
    out = decide_actions_for_codes(
        {
            "US.AAPL": [_sig("threshold_breach_lower", Severity.CRITICAL)],
            "US.NVDA": [_sig("futu_capital_anomaly")],  # no rule fires
        },
        positions={"US.AAPL": 50},
    )
    assert "US.AAPL" in out
    assert "US.NVDA" not in out
    assert out["US.AAPL"].action == "BUY"


def test_decide_actions_uses_per_code_position() -> None:
    """Each code gets its own current_qty for the cap calc."""
    sigs_aapl = [_sig("threshold_breach_upper", Severity.CRITICAL)]
    sigs_nvda = [
        Signal(
            code="US.NVDA",
            ts=datetime(2026, 5, 2, 14, 0),
            signal_type="threshold_breach_upper",
            severity=Severity.CRITICAL,
            payload={},
        )
    ]
    out = decide_actions_for_codes(
        {"US.AAPL": sigs_aapl, "US.NVDA": sigs_nvda},
        positions={"US.AAPL": 100, "US.NVDA": 30},
    )
    assert out["US.AAPL"].qty == 100
    assert out["US.NVDA"].qty == 30
