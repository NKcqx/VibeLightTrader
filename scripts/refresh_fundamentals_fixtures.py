"""One-shot probe: refresh local yfinance fundamentals fixtures.

Thin wrapper around :func:`vibe_trader.data.fundamentals.refresh_fixtures`.
Use this for ad-hoc / on-demand refreshes from the command line. The
scheduled cron path (``run_refresh_fundamentals``) shares the same
implementation.

Examples:

    # Default: refresh NVDA + MSFT
    python scripts/refresh_fundamentals_fixtures.py

    # Specific tickers (with or without US. prefix)
    python scripts/refresh_fundamentals_fixtures.py US.NVDA MSFT TSLA
"""

from __future__ import annotations

import sys

from vibe_trader.data.fundamentals import refresh_fixtures


def _normalise(arg: str) -> str:
    return arg if arg.startswith("US.") else f"US.{arg.upper()}"


def main(args: list[str]) -> int:
    codes = [_normalise(a) for a in (args or ["NVDA", "MSFT"])]
    print(f"[refresh] requesting: {codes}", flush=True)
    summary = refresh_fixtures(codes)
    bad = 0
    for code, status in summary.items():
        marker = "✓" if status == "ok" else "✗"
        print(f"  {marker} {code}: {status}")
        if not status.startswith("ok"):
            bad += 1
    if bad:
        print(f"[refresh] DONE — {len(summary) - bad} ok, {bad} failed.")
        return 1
    print(f"[refresh] DONE — all {len(summary)} ok.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
