from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from equity_monitor.data.indicators import compute_indicators
from equity_monitor.data.kline import fetch_kline_df
from equity_monitor.db import session_scope
from equity_monitor.futu_client import FutuClient
from equity_monitor.models import Indicator, Quote, Symbol


def _ts_to_pydt(ts: Any) -> Any:
    return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts


def _safe_float(v: Any) -> float | None:
    """Convert pandas value to float or None for NaN/NA."""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(out):
        return None
    return out


def backfill_symbol(
    *,
    client: FutuClient,
    factory: sessionmaker,
    code: str,
    days: int,
) -> dict[str, int]:
    """Backfill 60-min OHLC + computed indicators for one symbol.

    Returns insert counts (excluding rows skipped by ON CONFLICT DO NOTHING).
    Idempotent: re-running with same data inserts 0 new rows.
    """
    # ~7 K_60M bars per US trading day (6.5h session); pad to 60 min minimum
    limit = max(60, days * 7)

    df = fetch_kline_df(client, code, ktype="K_60M", limit=limit)
    if df.empty:
        return {"quotes": 0, "indicators": 0}

    ind = compute_indicators(df)

    inserted_q, inserted_i = 0, 0
    with session_scope(factory) as session:
        sym = session.query(Symbol).filter(Symbol.code == code).one_or_none()
        if sym is None:
            return {"quotes": 0, "indicators": 0}

        for ts, row in df.iterrows():
            stmt = (
                sqlite_insert(Quote)
                .values(
                    symbol_id=sym.id,
                    ts=_ts_to_pydt(ts),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                    turnover=float(row["turnover"]),
                )
                .on_conflict_do_nothing(index_elements=["symbol_id", "ts"])
            )
            r = session.execute(stmt)
            if r.rowcount and r.rowcount > 0:
                inserted_q += 1

        for ts, row in ind.iterrows():
            stmt = (
                sqlite_insert(Indicator)
                .values(
                    symbol_id=sym.id,
                    ts=_ts_to_pydt(ts),
                    rsi_14=_safe_float(row.get("rsi_14")),
                    macd=_safe_float(row.get("macd")),
                    macd_signal=_safe_float(row.get("macd_signal")),
                    macd_hist=_safe_float(row.get("macd_hist")),
                    boll_upper=_safe_float(row.get("boll_upper")),
                    boll_mid=_safe_float(row.get("boll_mid")),
                    boll_lower=_safe_float(row.get("boll_lower")),
                )
                .on_conflict_do_nothing(index_elements=["symbol_id", "ts"])
            )
            r = session.execute(stmt)
            if r.rowcount and r.rowcount > 0:
                inserted_i += 1

    return {"quotes": inserted_q, "indicators": inserted_i}


def backfill_all(
    *,
    client: FutuClient,
    factory: sessionmaker,
    codes: Sequence[str],
    days: int,
) -> dict[str, dict[str, int]]:
    """Backfill multiple symbols sequentially. Returns per-symbol stats."""
    return {
        code: backfill_symbol(
            client=client, factory=factory, code=code, days=days
        )
        for code in codes
    }
