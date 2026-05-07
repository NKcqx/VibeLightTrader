"""One-shot probe: fetch yfinance fundamentals for the watch-list and persist
raw responses to ``src/vibe_trader/data/fixtures/fundamentals/raw/``.

This script is the *only* place we hit yfinance over the network. The rest of
the codebase reads the persisted JSON via :mod:`vibe_trader.data.fundamentals`,
which keeps us insulated from any rate-limiting / anti-scrape behaviour.

Run it sparingly, e.g. once a week:

    python scripts/refresh_fundamentals_fixtures.py NVDA MSFT
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "src/vibe_trader/data/fixtures/fundamentals/raw"


def _to_jsonable(value: Any) -> Any:
    """Best-effort conversion of pandas / numpy / datetime values into JSON."""
    if value is None:
        return None
    if isinstance(value, float) and (pd.isna(value) or value != value):  # NaN
        return None
    if isinstance(value, (str, int, bool)):
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
        # Normalise to ISO-8601 UTC where possible.
        try:
            return value.tz_convert("UTC").isoformat() if value.tzinfo else value.isoformat()
        except Exception:
            return value.isoformat()
    if isinstance(value, pd.DataFrame):
        df = value.reset_index()
        # Convert all index/column values recursively.
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
    # Fallback: try str().
    try:
        return str(value)
    except Exception:
        return None


def _safe(call, default: Any = None) -> Any:
    """Run a yfinance accessor and swallow exceptions."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def fetch_one(ticker_short: str) -> dict[str, Any]:
    print(f"[refresh] {ticker_short}: fetching...", flush=True)
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


def main(tickers: list[str]) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for short in tickers:
        try:
            data = fetch_one(short)
        except Exception as exc:  # noqa: BLE001
            print(f"[refresh] {short}: FAILED — {exc}")
            continue
        out = RAW_DIR / f"US.{short}.json"
        out.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        print(f"[refresh] {short}: wrote {out} ({out.stat().st_size:,} bytes)")
    print(f"[refresh] done. fixtures at {RAW_DIR}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:] or ["NVDA", "MSFT"]
    sys.exit(main(args))
