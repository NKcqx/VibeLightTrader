from __future__ import annotations

import pandas as pd
import structlog

from vibe_trader.futu_client import FREQ_TO_KTYPE, FutuClient


log = structlog.get_logger(__name__)


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


def fetch_klines_multi(
    client: FutuClient,
    code: str,
    freqs: list[str],
    *,
    limit: int = 200,
) -> dict[str, pd.DataFrame]:
    """Return one OHLCV DataFrame per requested frequency.

    Frequencies not present in `FREQ_TO_KTYPE` are skipped after logging a warning.
    """
    out: dict[str, pd.DataFrame] = {}
    for freq in freqs:
        ktype = FREQ_TO_KTYPE.get(freq)
        if ktype is None:
            log.warning("kline.unknown_freq_skipped", freq=freq, code=code)
            continue
        out[freq] = fetch_kline_df(client, code, ktype=ktype, limit=limit)
    return out
