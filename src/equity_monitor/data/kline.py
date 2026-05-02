from __future__ import annotations

import pandas as pd

from equity_monitor.futu_client import FutuClient


def fetch_kline_df(
    client: FutuClient,
    code: str,
    *,
    ktype: str = "K_60M",
    limit: int = 200,
) -> pd.DataFrame:
    """Return a tidy OHLCV DataFrame indexed by ts (ascending)."""
    candles = client.kline(code, ktype=ktype, limit=limit)
    if not candles:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume", "turnover"]
        )
    rows = [
        {
            "ts": c.ts,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "turnover": c.turnover,
        }
        for c in candles
    ]
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    return df
