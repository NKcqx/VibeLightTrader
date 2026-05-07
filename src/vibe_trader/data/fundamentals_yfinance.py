"""Live yfinance fetcher for the fundamentals snapshot.

This is the *only* place the codebase touches yfinance. It is invoked by:

  - ``scripts/refresh_fundamentals_fixtures.py`` (manual one-shot)
  - ``scheduler.jobs.run_refresh_fundamentals`` (daily cron, default off)

Anything else reads via :mod:`vibe_trader.data.fundamentals`'s fixture
client. yfinance is a soft dependency: this module imports it lazily so
``vibe_trader.data.fundamentals`` (the read path) keeps working even on
machines without yfinance installed.

Design notes:
  * Per-call exceptions are caught and recorded as ``{"__error__": ...}``
    inside the returned dict so a partial fixture still lands. The cron
    runner inspects the top-level return code.
  * ``_to_jsonable`` aggressively converts pandas / numpy / datetime
    types to JSON-friendly primitives so the snapshot can be written
    with the stdlib ``json`` module — no pickle, no parquet, no surprises.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _to_jsonable(value: Any) -> Any:
    """Recursive pandas / numpy / datetime → JSON conversion."""
    import numpy as np  # local import: keep top-level fast
    import pandas as pd

    if value is None:
        return None
    if isinstance(value, float) and (pd.isna(value) or value != value):
        return None
    if isinstance(value, bool):  # bool before int (bool is an int subclass)
        return value
    if isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        v = float(value)
        return None if v != v else v
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        try:
            return value.tz_convert("UTC").isoformat() if value.tzinfo else value.isoformat()
        except Exception:
            return value.isoformat()
    if isinstance(value, pd.DataFrame):
        df = value.reset_index()
        return [
            {str(col): _to_jsonable(row[col]) for col in df.columns}
            for _, row in df.iterrows()
        ]
    if isinstance(value, pd.Series):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    try:
        return str(value)
    except Exception:
        return None


def _safe(call, default: Any = None) -> Any:
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def fetch_raw_fundamentals(ticker_short: str) -> dict[str, Any]:
    """Pull the raw yfinance bundle for ``ticker_short`` (e.g. ``"NVDA"``).

    Returns a JSON-serialisable dict matching the schema consumed by
    :func:`vibe_trader.data.fundamentals.parse_raw_fundamentals`.

    Raises only if yfinance itself can't be imported. Per-endpoint
    failures degrade gracefully into ``{"__error__": ...}`` entries so
    a partial snapshot can still be persisted.
    """
    import yfinance as yf  # lazy

    t = yf.Ticker(ticker_short)
    payload: dict[str, Any] = {
        "ticker": ticker_short,
        "code": f"US.{ticker_short}",
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    payload["info"] = _to_jsonable(_safe(lambda: t.info))
    payload["recommendations"] = _to_jsonable(_safe(lambda: t.recommendations))
    payload["recommendations_summary"] = _to_jsonable(
        _safe(lambda: t.recommendations_summary)
    )
    payload["upgrades_downgrades"] = _to_jsonable(_safe(lambda: t.upgrades_downgrades))
    payload["analyst_price_targets"] = _to_jsonable(_safe(lambda: t.analyst_price_targets))
    payload["news"] = _to_jsonable(_safe(lambda: t.news))
    payload["calendar"] = _to_jsonable(_safe(lambda: t.calendar))
    payload["earnings_dates"] = _to_jsonable(_safe(lambda: t.earnings_dates))
    payload["institutional_holders"] = _to_jsonable(_safe(lambda: t.institutional_holders))
    payload["major_holders"] = _to_jsonable(_safe(lambda: t.major_holders))
    return payload


__all__ = ["fetch_raw_fundamentals"]
