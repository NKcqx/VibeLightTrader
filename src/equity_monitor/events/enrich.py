"""Watchlist card enrichment — pull live OpenD data + indicators + thresholds.

Builds a list of `WatchlistCardRow` for the listener to ship as a Lark card
in response to /list, /add, /remove, /threshold replies.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy.orm import sessionmaker

from equity_monitor.config import AppConfig
from equity_monitor.data.indicators import compute_indicators
from equity_monitor.data.kline import fetch_kline_df
from equity_monitor.db import session_scope
from equity_monitor.futu_client import FutuClient, Snapshot
from equity_monitor.models import Symbol
from equity_monitor.reports.interpret import (
    IndicatorReading,
    ReturnSummary,
    reading_from_row,
)
from equity_monitor.reports.render import WatchlistCardRow

log = structlog.get_logger(__name__)


def build_watchlist_rows(
    *,
    cfg: AppConfig,
    factory: sessionmaker,
    client: FutuClient,
) -> tuple[list[WatchlistCardRow], int]:
    """Build per-symbol diagnostic rows from current DB watchlist + live OpenD.

    Returns (rows, count). On per-symbol fetch failure, that row falls back
    to threshold-only display so the card still renders.
    """
    with session_scope(factory) as s:
        db_symbols: list[tuple[str, str | None, float | None, float | None]] = [
            (r.code, r.name, r.upper_threshold, r.lower_threshold)
            for r in s.query(Symbol).order_by(Symbol.code).all()
        ]
    if not db_symbols:
        return [], 0

    codes = [t[0] for t in db_symbols]
    snapshots: dict[str, Snapshot] = {}
    try:
        snapshots = {snap.code: snap for snap in client.snapshot(codes)}
    except Exception:
        log.exception("enrich.snapshot_failed")

    rows: list[WatchlistCardRow] = []
    for code, name, upper, lower in db_symbols:
        body = _build_row_body(
            client=client,
            code=code,
            name=name or code.split(".")[-1],
            upper=upper,
            lower=lower,
            snapshot=snapshots.get(code),
            cfg=cfg,
        )
        rows.append(WatchlistCardRow(code=code, body_md=body))
    return rows, len(db_symbols)


def _build_row_body(
    *,
    client: FutuClient,
    code: str,
    name: str,
    upper: float | None,
    lower: float | None,
    snapshot: Snapshot | None,
    cfg: AppConfig,
) -> str:
    """Compose body markdown for one symbol on the watchlist card."""

    # ---- header line ----
    header = f"**`{code}`** {name}"
    if snapshot is not None:
        intraday: float | None = None
        if snapshot.open_price:
            intraday = (snapshot.last_price - snapshot.open_price) / snapshot.open_price
        if intraday is None:
            price_line = f"💰 **${snapshot.last_price:.2f}**"
        else:
            arrow = "▲" if intraday >= 0 else "▼"
            price_line = (
                f"💰 **${snapshot.last_price:.2f}**  {arrow} {intraday:+.2%} (日内)"
            )
    else:
        price_line = "💰 *(报价获取失败)*"

    # ---- thresholds line ----
    parts: list[str] = []
    if upper is not None:
        breached_up = (
            snapshot is not None and snapshot.last_price >= upper
        )
        marker = " 🔴" if breached_up else ""
        parts.append(f"上限 **{upper:.2f}**{marker}")
    if lower is not None:
        breached_dn = (
            snapshot is not None and snapshot.last_price <= lower
        )
        marker = " 🔴" if breached_dn else ""
        parts.append(f"下限 **{lower:.2f}**{marker}")
    threshold_line = "🎯 " + " · ".join(parts) if parts else "🎯 *无阈值*"

    # ---- indicator line ----
    indicator_line = _build_indicator_line(client, code, cfg)

    return "\n".join([header, price_line, threshold_line, indicator_line])


def _build_indicator_line(
    client: FutuClient,
    code: str,
    cfg: AppConfig,
) -> str:
    """One-line indicator summary or fallback message."""
    try:
        df = fetch_kline_df(client, code, ktype="K_60M", limit=200)
    except Exception:
        log.exception("enrich.kline_failed", code=code)
        return "📊 *指标计算失败*"
    if df.empty:
        return "📊 *暂无 K 线数据*"
    try:
        ind_df = compute_indicators(
            df,
            rsi_period=14,
            macd_fast=cfg.signals.macd_fast,
            macd_slow=cfg.signals.macd_slow,
            macd_signal=cfg.signals.macd_signal,
            boll_period=cfg.signals.bollinger_period,
            boll_std=cfg.signals.bollinger_std,
        )
    except Exception:
        log.exception("enrich.indicators_failed", code=code)
        return "📊 *指标计算失败*"
    if ind_df.empty:
        return "📊 *指标计算失败*"
    last = ind_df.iloc[-1]
    reading = reading_from_row(last.to_dict(), close=float(last["close"]))
    bits = reading.lines()
    if not bits:
        return "📊 *指标暂不可用*"
    return "📊 " + " · ".join(_short(b) for b in bits)


def _short(line: str) -> str:
    """Shorten the indicator interpret lines for one-line layout."""
    # Replace verbose bracketed BOLL list with a compact summary
    if line.startswith("BOLL"):
        # `BOLL [a / m / b] · 通道内 (69% 位置)` → `BOLL 通道内 (69%)`
        if " · " in line:
            return "BOLL " + line.split(" · ", 1)[1].replace(" 位置", "")
    return line


def build_returns_summary(snapshot: Snapshot | None) -> ReturnSummary:
    """Quick helper for callers wanting just the return summary."""
    if snapshot is None or not snapshot.open_price:
        return ReturnSummary(intraday=None, last_30_bars=None)
    intraday = (snapshot.last_price - snapshot.open_price) / snapshot.open_price
    return ReturnSummary(intraday=intraday, last_30_bars=None)


def build_indicator_reading(
    client: FutuClient, code: str, cfg: AppConfig
) -> IndicatorReading | None:
    """Standalone IndicatorReading; returns None on any data fetch failure."""
    try:
        df = fetch_kline_df(client, code, ktype="K_60M", limit=200)
        if df.empty:
            return None
        ind_df = compute_indicators(
            df,
            rsi_period=14,
            macd_fast=cfg.signals.macd_fast,
            macd_slow=cfg.signals.macd_slow,
            macd_signal=cfg.signals.macd_signal,
            boll_period=cfg.signals.bollinger_period,
            boll_std=cfg.signals.bollinger_std,
        )
        if ind_df.empty:
            return None
        last = ind_df.iloc[-1]
        return reading_from_row(last.to_dict(), close=float(last["close"]))
    except Exception:
        log.exception("enrich.reading_failed", code=code)
        return None


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)
