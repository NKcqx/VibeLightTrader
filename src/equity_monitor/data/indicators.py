from __future__ import annotations

import numpy as np
import pandas as pd


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI (standard formulation, matches TradingView/MetaTrader default).

    Edge cases (per Wilder 1978):
      - avg_loss == 0 and avg_gain  > 0  → RSI = 100  (only gains in window)
      - avg_loss == 0 and avg_gain == 0  → RSI = NaN  (no movement, undefined)
      - avg_gain == 0 and avg_loss  > 0  → RSI = 0    (only losses in window)
    Float division naturally gives `inf`/`nan` which collapses to the right RSI
    via `100 - 100/(1+rs)`; we just suppress the divide-by-zero RuntimeWarnings.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi = 100.0 - 100.0 / (1.0 + rs)
    return rsi


def _macd(
    close: pd.Series, fast: int, slow: int, signal: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Standard MACD (EMA fast - EMA slow), signal = EMA of MACD line, hist = MACD - signal."""
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(
        span=signal, adjust=False, min_periods=signal
    ).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def _bollinger(
    close: pd.Series, period: int, std_mult: float
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands: SMA ± std_mult * rolling population stddev (ddof=0)."""
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return lower, mid, upper


def compute_indicators(
    df: pd.DataFrame,
    *,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    boll_period: int = 20,
    boll_std: float = 2.0,
) -> pd.DataFrame:
    """Compute RSI / MACD / Bollinger from OHLC DataFrame.

    Returns a DataFrame indexed identically to `df` with the original columns
    plus rsi_14, macd, macd_signal, macd_hist, boll_upper, boll_mid, boll_lower.
    """
    out = df.copy()
    out["rsi_14"] = _rsi(out["close"], period=rsi_period)
    macd_line, sig_line, hist = _macd(
        out["close"], fast=macd_fast, slow=macd_slow, signal=macd_signal
    )
    out["macd"] = macd_line
    out["macd_signal"] = sig_line
    out["macd_hist"] = hist
    lower, mid, upper = _bollinger(out["close"], period=boll_period, std_mult=boll_std)
    out["boll_lower"] = lower
    out["boll_mid"] = mid
    out["boll_upper"] = upper
    return out
