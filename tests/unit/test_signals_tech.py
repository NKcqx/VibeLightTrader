from __future__ import annotations

from datetime import datetime

import pandas as pd

from vibe_trader.signals.base import Severity
from vibe_trader.signals.tech import detect_tech_signals


def _row(
    rsi: float = 50.0,
    macd_hist: float = 0.0,
    close: float = 100.0,
    lower: float = 80.0,
    upper: float = 120.0,
) -> dict:
    return {
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1,
        "turnover": 1.0,
        "rsi_14": rsi,
        "macd": 0.0,
        "macd_signal": 0.0,
        "macd_hist": macd_hist,
        "boll_lower": lower,
        "boll_mid": (lower + upper) / 2,
        "boll_upper": upper,
    }


def _df(rows: list[dict]) -> pd.DataFrame:
    idx = [datetime(2026, 5, 2, 9 + i) for i in range(len(rows))]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="ts"))


def test_rsi_overbought_emits_signal() -> None:
    df = _df([_row(rsi=50), _row(rsi=72)])
    sigs = detect_tech_signals("US.AAPL", df)
    types = {s.signal_type for s in sigs}
    assert "rsi_overbought" in types
    rsi_sig = next(s for s in sigs if s.signal_type == "rsi_overbought")
    assert rsi_sig.severity is Severity.WARN
    assert rsi_sig.payload == {"rsi": 72.0}


def test_rsi_oversold_emits_signal() -> None:
    df = _df([_row(rsi=50), _row(rsi=25)])
    sigs = detect_tech_signals("US.AAPL", df)
    assert any(s.signal_type == "rsi_oversold" for s in sigs)


def test_rsi_at_exactly_threshold_does_not_trigger() -> None:
    """RSI uses strict > and <; exactly 70.0 must NOT trigger overbought."""
    df = _df([_row(rsi=70), _row(rsi=70.0)])
    sigs = detect_tech_signals("US.AAPL", df)
    assert not any(
        s.signal_type in ("rsi_overbought", "rsi_oversold") for s in sigs
    )


def test_macd_golden_cross() -> None:
    df = _df([_row(macd_hist=-0.2), _row(macd_hist=0.3)])
    sigs = detect_tech_signals("US.AAPL", df)
    assert any(s.signal_type == "macd_golden_cross" for s in sigs)


def test_macd_death_cross() -> None:
    df = _df([_row(macd_hist=0.2), _row(macd_hist=-0.3)])
    sigs = detect_tech_signals("US.AAPL", df)
    assert any(s.signal_type == "macd_death_cross" for s in sigs)


def test_macd_crossing_zero_to_zero_does_not_trigger() -> None:
    """0 → 0 does not satisfy `prev <= 0 < last`; no cross."""
    df = _df([_row(macd_hist=0.0), _row(macd_hist=0.0)])
    sigs = detect_tech_signals("US.AAPL", df)
    assert not any(
        s.signal_type in ("macd_golden_cross", "macd_death_cross") for s in sigs
    )


def test_boll_break_upper_info() -> None:
    df = _df([_row(close=100), _row(close=125, upper=120, lower=80)])
    sigs = detect_tech_signals("US.AAPL", df)
    boll_sig = next(s for s in sigs if s.signal_type == "boll_upper_break")
    assert boll_sig.severity is Severity.INFO
    assert boll_sig.payload == {"close": 125.0, "upper": 120.0}


def test_boll_break_lower_info() -> None:
    df = _df([_row(close=100), _row(close=70, upper=120, lower=80)])
    sigs = detect_tech_signals("US.AAPL", df)
    assert any(s.signal_type == "boll_lower_break" for s in sigs)


def test_no_signal_when_normal() -> None:
    df = _df([_row(), _row()])
    sigs = detect_tech_signals("US.AAPL", df)
    assert sigs == []


def test_single_row_yields_no_signals() -> None:
    """Need at least 2 rows for cross detection."""
    df = _df([_row(rsi=80)])
    sigs = detect_tech_signals("US.AAPL", df)
    assert sigs == []


def test_nan_indicators_skipped_safely() -> None:
    """NaN warmup rows must not crash."""
    rows = [_row(), _row(rsi=80)]
    rows[0]["macd_hist"] = float("nan")
    rows[1]["macd_hist"] = float("nan")
    df = _df(rows)
    sigs = detect_tech_signals("US.AAPL", df)
    types = {s.signal_type for s in sigs}
    assert "rsi_overbought" in types
    assert not any(s.startswith("macd_") for s in types)


def test_combo_rsi_and_boll_break_together() -> None:
    """Both RSI overbought + close breaks upper band can fire on the same bar."""
    df = _df(
        [
            _row(rsi=50, close=100, upper=120, lower=80),
            _row(rsi=78, close=125, upper=120, lower=80),
        ]
    )
    sigs = detect_tech_signals("US.AAPL", df)
    types = {s.signal_type for s in sigs}
    assert {"rsi_overbought", "boll_upper_break"}.issubset(types)
