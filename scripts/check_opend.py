"""Smoke check: confirm OpenD is reachable and quote API works.

Run AFTER OpenD is installed and logged in (see scripts/install_opend.sh).
Requires the project's conda env (e.g. `conda activate vibe-trader`) so
`futu-api` from `pip install -e .` is importable.
"""
from __future__ import annotations

import sys

from futu import RET_OK, OpenQuoteContext


def main() -> int:
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, data = ctx.get_market_snapshot(["US.AAPL"])
        if ret != RET_OK:
            print(f"FAIL: snapshot returned {ret}: {data}", file=sys.stderr)
            return 1
        print("OK: OpenD reachable")
        cols = [c for c in ["code", "last_price", "update_time"] if c in data.columns]
        print(data[cols].to_string(index=False))
        return 0
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
