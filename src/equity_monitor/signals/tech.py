from __future__ import annotations

from datetime import datetime

import pandas as pd

from equity_monitor.signals.base import Severity, Signal


def detect_tech_signals(
    code: str,
    indicators_df: pd.DataFrame,
    *,
    rsi_overbought: float = 70.0,
    rsi_oversold: float = 30.0,
) -> list[Signal]:
    """Inspect the LATEST row of indicators_df and emit signals.

    Required columns: rsi_14, macd_hist, close, boll_upper, boll_lower.

    Detects (using only the last 2 rows):
      - rsi_overbought / rsi_oversold       (WARN)  — strict > / <
      - macd_golden_cross / macd_death_cross (WARN) — sign flip vs prev row
      - boll_upper_break / boll_lower_break  (INFO) — last close vs last band
    """
    if len(indicators_df) < 2:
        return []
    last = indicators_df.iloc[-1]
    prev = indicators_df.iloc[-2]
    raw_ts = indicators_df.index[-1]
    ts: datetime = (
        raw_ts.to_pydatetime() if hasattr(raw_ts, "to_pydatetime") else raw_ts
    )

    out: list[Signal] = []

    if pd.notna(last["rsi_14"]):
        if last["rsi_14"] > rsi_overbought:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="rsi_overbought",
                    severity=Severity.WARN,
                    payload={"rsi": float(last["rsi_14"])},
                )
            )
        if last["rsi_14"] < rsi_oversold:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="rsi_oversold",
                    severity=Severity.WARN,
                    payload={"rsi": float(last["rsi_14"])},
                )
            )

    if pd.notna(last["macd_hist"]) and pd.notna(prev["macd_hist"]):
        if prev["macd_hist"] <= 0 < last["macd_hist"]:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="macd_golden_cross",
                    severity=Severity.WARN,
                    payload={"macd_hist": float(last["macd_hist"])},
                )
            )
        if prev["macd_hist"] >= 0 > last["macd_hist"]:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="macd_death_cross",
                    severity=Severity.WARN,
                    payload={"macd_hist": float(last["macd_hist"])},
                )
            )

    if pd.notna(last["close"]) and pd.notna(last["boll_upper"]):
        if last["close"] > last["boll_upper"]:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="boll_upper_break",
                    severity=Severity.INFO,
                    payload={
                        "close": float(last["close"]),
                        "upper": float(last["boll_upper"]),
                    },
                )
            )
    if pd.notna(last["close"]) and pd.notna(last["boll_lower"]):
        if last["close"] < last["boll_lower"]:
            out.append(
                Signal(
                    code=code,
                    ts=ts,
                    signal_type="boll_lower_break",
                    severity=Severity.INFO,
                    payload={
                        "close": float(last["close"]),
                        "lower": float(last["boll_lower"]),
                    },
                )
            )

    return out
