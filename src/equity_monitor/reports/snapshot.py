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
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import matplotlib

matplotlib.use("Agg")  # headless PNG; must run before pyplot/mplfinance bind a backend

import matplotlib.pyplot as plt  # noqa: E402 — after backend selection
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402

_OHLCV_COLS = frozenset({"open", "high", "low", "close", "volume"})


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
        # TODO(p4): stack same-bar markers; currently last-wins.
        s.iloc[idx[0]] = m.price
    return s


def _safe_filename(code: str, freq: str) -> str:
    safe = code.replace(".", "_").replace("/", "_")
    return f"{safe}_{freq}_{datetime.now(tz=timezone.utc):%Y%m%d_%H%M%S}.png"


def _save_placeholder_png(out_path: Path, message: str) -> None:
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=14)
    ax.axis("off")
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def _validate_ohlcv_df(df: pd.DataFrame) -> bool:
    if not _OHLCV_COLS.issubset(df.columns):
        return False
    if not isinstance(df.index, pd.DatetimeIndex):
        return False
    return True


def render_snapshot(req: SnapshotRequest) -> Path:
    """Render `req` to a PNG under `out_dir` and return the path."""
    out_dir = req.out_dir or Path("var/snapshots")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _safe_filename(req.code, req.freq)

    if req.df.empty:
        _save_placeholder_png(
            out_path,
            f"{req.code} ({req.freq}) — 暂无 K 线数据",
        )
        return out_path

    if not _validate_ohlcv_df(req.df):
        cols_repr = "[" + ",".join(req.df.columns.astype(str).tolist()) + "]"
        _save_placeholder_png(
            out_path,
            f"{req.code} ({req.freq}) — 数据格式异常 (cols={cols_repr})",
        )
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
    # Close only mplfinance's current figure so we don't call `plt.close("all")`
    # (which destroys figures owned elsewhere). mplfinance exposes one implicit
    # current figure via savefig/gcf(); if newer mplfinance attaches extra dormant
    # figures, those could leak—we accept that trade-off in CLI/server snapshots.
    plt.close(plt.gcf())
    return out_path
