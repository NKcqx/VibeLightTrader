"""One-shot helper to render a demo K-line PNG with multiple trade points
for the article. Reads real NVDA bars from OpenD, but the markers are
fabricated — clearly labelled as demo. Output goes under
`docs/articles/assets/` (gitignored).

Usage:
    python scripts/gen_demo_chart.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vibe_trader.futu_client import FREQ_TO_KTYPE, OpenDClient
from vibe_trader.data.kline import fetch_kline_df
from vibe_trader.reports.snapshot import (
    SnapshotRequest,
    TradeMarker,
    render_snapshot,
)


def main() -> None:
    client = OpenDClient("127.0.0.1", 11111)
    try:
        df = fetch_kline_df(
            client, "US.NVDA", ktype=FREQ_TO_KTYPE["60m"], limit=200
        )
    finally:
        client.close()

    # Anchor markers to actual bar timestamps so they snap precisely.
    # Pattern: scaling-in over 3 buys, partial trim, second add, final trim
    # — the kind of mid-term swing the LLM strategy is designed to do.
    bars = df.index.to_list()
    n = len(bars)
    if n < 60:
        raise SystemExit(f"need >=60 bars for demo, got {n}")

    def at(idx: int) -> datetime:
        ts = bars[idx]
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    # Cherry-pick prices from real bars for plausibility
    closes = df["close"].astype(float).to_list()
    lows = df["low"].astype(float).to_list()
    highs = df["high"].astype(float).to_list()

    markers = [
        # Initial buy on a pullback bar (~25% into the window)
        TradeMarker(
            ts=at(int(n * 0.25)), side="buy",
            qty=100, price=lows[int(n * 0.25)],
        ),
        # Add #1 — another dip, ~5% lower
        TradeMarker(
            ts=at(int(n * 0.32)), side="buy",
            qty=80, price=lows[int(n * 0.32)],
        ),
        # Add #2 — third tranche
        TradeMarker(
            ts=at(int(n * 0.42)), side="buy",
            qty=60, price=lows[int(n * 0.42)],
        ),
        # Take-profit #1 — trim 50% on a strong bar
        TradeMarker(
            ts=at(int(n * 0.58)), side="sell",
            qty=120, price=highs[int(n * 0.58)],
        ),
        # Re-entry on next pullback
        TradeMarker(
            ts=at(int(n * 0.70)), side="buy",
            qty=80, price=lows[int(n * 0.70)],
        ),
        # Final take-profit on the latest run-up
        TradeMarker(
            ts=at(int(n * 0.88)), side="sell",
            qty=100, price=highs[int(n * 0.88)],
        ),
    ]

    # Compute the running avg cost so the orange line ends up where
    # the real strategy would put it.
    qty = 0
    basis = 0.0
    for m in markers:
        if m.side == "buy":
            qty += m.qty
            basis += m.qty * m.price
        else:
            if qty > 0:
                avg = basis / qty
                sell_qty = min(m.qty, qty)
                basis -= sell_qty * avg
                qty -= sell_qty
    avg_cost = basis / qty if qty > 0 else None

    out_dir = ROOT / "docs" / "articles" / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    req = SnapshotRequest(
        code="US.NVDA",
        freq="60m",
        df=df,
        markers=markers,
        avg_cost=avg_cost,
        current_price=closes[-1],
        out_dir=out_dir,
    )
    png = render_snapshot(req)
    print(f"wrote {png}")
    avg_str = f"{avg_cost:.2f}" if avg_cost else "n/a"
    print(f"markers: {len(markers)}, avg_cost: {avg_str}")


if __name__ == "__main__":
    main()
