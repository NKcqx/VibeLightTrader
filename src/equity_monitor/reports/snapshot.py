"""Render OHLCV + paper-trade markers as a static PNG (Phase 3, scoped).

Uses mplfinance with the 'charles' style:
  - Green ▲ markers for BUY fills
  - Red ▼ markers for SELL fills
  - Orange dashed horizontal line at average cost
  - Steel-blue dashed horizontal line at current price

The result is a single self-contained PNG that's fine to ship through
the Lark image API. No interactive features; users view it in the Lark
app on phone or desktop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd


@dataclass(frozen=True)
class TradeMarker:
    ts: datetime
    side: Literal["buy", "sell"]
    qty: int
    price: float


@dataclass(frozen=True)
class SnapshotRequest:
    code: str
    freq: str
    df: pd.DataFrame                                # OHLCV indexed by ts (UTC, ascending)
    markers: list[TradeMarker] = field(default_factory=list)
    avg_cost: float | None = None
    current_price: float | None = None
    out_dir: Path | None = None                     # default var/snapshots/


def _markers_series(
    df: pd.DataFrame, markers: list[TradeMarker], side: str
) -> pd.Series:
    """Build a DataFrame-aligned series with NaN where no marker, else price."""
    s = pd.Series(index=df.index, dtype=float)
    for m in markers:
        if m.side != side:
            continue
        # Snap to the bar at-or-before m.ts (markers don't always land exactly).
        idx = df.index.get_indexer([pd.Timestamp(m.ts)], method="ffill")
        if idx[0] == -1:
            continue
        s.iloc[idx[0]] = m.price
    return s


def _safe_filename(code: str, freq: str) -> str:
    safe = code.replace(".", "_").replace("/", "_")
    return f"{safe}_{freq}_{datetime.utcnow():%Y%m%d_%H%M%S}.png"


def render_snapshot(req: SnapshotRequest) -> Path:
    """Render `req` to a PNG under `out_dir` and return the path."""
    out_dir = req.out_dir or Path("var/snapshots")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _safe_filename(req.code, req.freq)

    if req.df.empty:
        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
        ax.text(
            0.5, 0.5,
            f"{req.code} ({req.freq}) — 暂无 K 线数据",
            ha="center", va="center", fontsize=14,
        )
        ax.axis("off")
        fig.savefig(out_path, bbox_inches="tight")
        plt.close(fig)
        return out_path

    addplots: list = []
    buy_s = _markers_series(req.df, req.markers, "buy")
    sell_s = _markers_series(req.df, req.markers, "sell")
    if buy_s.notna().any():
        addplots.append(
            mpf.make_addplot(
                buy_s, type="scatter", marker="^",
                markersize=140, color="#2ecc71", panel=0,
            )
        )
    if sell_s.notna().any():
        addplots.append(
            mpf.make_addplot(
                sell_s, type="scatter", marker="v",
                markersize=140, color="#e74c3c", panel=0,
            )
        )

    hlines: dict[str, list] = {
        "hlines": [], "colors": [], "linestyle": "--", "linewidths": 1,
    }
    if req.avg_cost is not None:
        hlines["hlines"].append(req.avg_cost)
        hlines["colors"].append("orange")
    if req.current_price is not None:
        hlines["hlines"].append(req.current_price)
        hlines["colors"].append("steelblue")

    title = f"{req.code} · {req.freq}"
    if req.current_price is not None:
        title += f"  ${req.current_price:.2f}"
    if req.avg_cost is not None:
        title += f" (avg ${req.avg_cost:.2f})"

    plot_kw: dict = {
        "type": "candle",
        "style": "charles",
        "addplot": addplots,
        "volume": True,
        "figsize": (9, 6),
        "figratio": (16, 9),
        "title": title,
        "savefig": dict(fname=str(out_path), dpi=120, bbox_inches="tight"),
    }
    if hlines["hlines"]:
        plot_kw["hlines"] = hlines

    mpf.plot(req.df, **plot_kw)
    plt.close("all")
    return out_path
