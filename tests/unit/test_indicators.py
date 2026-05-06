from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from vibe_trader.data.indicators import compute_indicators

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "known_ohlc.csv"


def _load() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE, parse_dates=["ts"]).set_index("ts").sort_index()
    return df


# ──────────────────── Plan-spec tests (sanity / direction) ───────────────────


def test_compute_indicators_columns_and_length() -> None:
    df = _load()
    out = compute_indicators(df)
    expected = {
        "rsi_14",
        "macd",
        "macd_signal",
        "macd_hist",
        "boll_upper",
        "boll_mid",
        "boll_lower",
    }
    assert expected.issubset(out.columns)
    assert len(out) == len(df)


def test_rsi_high_in_uptrend() -> None:
    df = _load()
    out = compute_indicators(df)
    assert out["rsi_14"].iloc[-1] > 70.0


def test_macd_positive_in_uptrend() -> None:
    df = _load()
    out = compute_indicators(df)
    assert out["macd"].iloc[-1] > 0
    assert out["macd_hist"].iloc[-1] > 0


def test_boll_mid_equals_sma() -> None:
    df = _load()
    out = compute_indicators(df, boll_period=20)
    sma = df["close"].rolling(20).mean()
    pd.testing.assert_series_equal(
        out["boll_mid"].dropna(),
        sma.dropna(),
        check_names=False,
    )


# ──────────────────── Numerical-precision tests (cross-check formulas) ───────


def _make_df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2026-04-01", periods=n, freq="h", name="ts")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [10_000] * n,
            "turnover": [10_000.0 * c for c in closes],
        },
        index=idx,
    )


def test_rsi_monotonic_uptrend_yields_100() -> None:
    """RSI on a strictly monotonic uptrend must converge to 100 (loss = 0)."""
    df = _make_df([100.0 + i for i in range(30)])
    out = compute_indicators(df, rsi_period=14)
    assert math.isclose(out["rsi_14"].iloc[-1], 100.0, abs_tol=1e-9)


def test_rsi_constant_series_is_nan() -> None:
    """If close never changes, both gain and loss are zero → RSI undefined (NaN)."""
    df = _make_df([100.0] * 20)
    out = compute_indicators(df, rsi_period=14)
    assert pd.isna(out["rsi_14"].iloc[-1])


def test_boll_constant_series_collapses_to_mid() -> None:
    """Constant close → std=0 → upper == mid == lower."""
    df = _make_df([100.0] * 25)
    out = compute_indicators(df, boll_period=20, boll_std=2.0)
    last = out.iloc[-1]
    assert math.isclose(last["boll_mid"], 100.0)
    assert math.isclose(last["boll_upper"], 100.0)
    assert math.isclose(last["boll_lower"], 100.0)


def test_boll_matches_population_std() -> None:
    """Verify boll uses ddof=0 (population) std, not sample (ddof=1) std."""
    closes = list(np.linspace(100.0, 130.0, 20))
    df = _make_df(closes)
    out = compute_indicators(df, boll_period=20, boll_std=2.0)
    sma = sum(closes) / 20
    pop_std = math.sqrt(sum((c - sma) ** 2 for c in closes) / 20)
    assert math.isclose(out["boll_mid"].iloc[-1], sma, abs_tol=1e-9)
    assert math.isclose(out["boll_upper"].iloc[-1], sma + 2 * pop_std, abs_tol=1e-9)
    assert math.isclose(out["boll_lower"].iloc[-1], sma - 2 * pop_std, abs_tol=1e-9)


def test_macd_zero_when_close_constant() -> None:
    """Constant close → EMA fast == EMA slow → MACD line = 0."""
    df = _make_df([100.0] * 40)
    out = compute_indicators(df, macd_fast=12, macd_slow=26, macd_signal=9)
    assert math.isclose(out["macd"].iloc[-1], 0.0, abs_tol=1e-12)
    assert math.isclose(out["macd_signal"].iloc[-1], 0.0, abs_tol=1e-12)
    assert math.isclose(out["macd_hist"].iloc[-1], 0.0, abs_tol=1e-12)
