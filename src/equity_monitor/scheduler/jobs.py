from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from equity_monitor.config import AppConfig, WatchlistConfig
from equity_monitor.data.indicators import compute_indicators
from equity_monitor.data.kline import fetch_kline_df
from equity_monitor.data.quotes import sync_snapshots
from equity_monitor.db import session_scope
from equity_monitor.futu_client import FutuClient
from equity_monitor.models import Indicator
from equity_monitor.models import Signal as SignalRow
from equity_monitor.models import Symbol
from equity_monitor.reports.lark import send_card
from equity_monitor.reports.render import render_daily_brief, render_signal_alert
from equity_monitor.signals.base import Signal
from equity_monitor.signals.compose import deduplicate, split_by_severity
from equity_monitor.signals.tech import detect_tech_signals
from equity_monitor.signals.threshold import detect_threshold_breach


log = structlog.get_logger(__name__)


SendCardFn = Callable[[dict[str, Any], str, str], str]


def _default_sender(card: dict[str, Any], open_id: str, receiver_type: str) -> str:
    return send_card(card, open_id=open_id, receiver_type=receiver_type)


def _persist_indicator_row(
    session, sym_id: int, ts: datetime, row: dict[str, float | None]
) -> None:
    stmt = (
        sqlite_insert(Indicator)
        .values(symbol_id=sym_id, ts=ts, **row)
        .on_conflict_do_update(
            index_elements=["symbol_id", "ts"],
            set_=row,
        )
    )
    session.execute(stmt)


def _persist_signal_rows(session, signals: list[Signal]) -> dict[int, str]:
    """Insert dedup'd signals; return {row_id: signal_type} for delivered tracking."""
    inserted: dict[int, str] = {}
    for s in signals:
        sym = session.query(Symbol).filter(Symbol.code == s.code).one_or_none()
        if sym is None:
            continue
        stmt = (
            sqlite_insert(SignalRow)
            .values(
                symbol_id=sym.id,
                ts=s.ts,
                signal_type=s.signal_type,
                severity=s.severity.value,
                payload_json=json.dumps(s.payload),
                delivered=False,
            )
            .on_conflict_do_nothing(
                index_elements=["symbol_id", "ts", "signal_type"]
            )
        )
        result = session.execute(stmt)
        if result.inserted_primary_key:
            inserted[result.inserted_primary_key[0]] = s.signal_type
    return inserted


def _ts_to_pydatetime(raw: Any) -> datetime:
    return raw.to_pydatetime() if hasattr(raw, "to_pydatetime") else raw


def run_intraday_check(
    *,
    client: FutuClient,
    factory: sessionmaker,
    cfg: AppConfig,
    watchlist: WatchlistConfig,
    now_utc: datetime | None = None,
    send_card_fn: SendCardFn = _default_sender,
) -> dict[str, int]:
    """One pass of intraday_check.

    Returns {'quotes': N, 'signals': M, 'pushed': P}.
    """
    now_utc = now_utc or datetime.now(tz=timezone.utc)
    codes = [s.code for s in watchlist.symbols]

    inserted_quotes = sync_snapshots(client, factory, codes=codes)

    all_sigs: list[Signal] = []

    for sym_cfg in watchlist.symbols:
        df = fetch_kline_df(client, sym_cfg.code, ktype="K_60M", limit=200)
        if df.empty:
            continue
        ind_df = compute_indicators(
            df,
            rsi_period=14,
            macd_fast=cfg.signals.macd_fast,
            macd_slow=cfg.signals.macd_slow,
            macd_signal=cfg.signals.macd_signal,
            boll_period=cfg.signals.bollinger_period,
            boll_std=cfg.signals.bollinger_std,
        )
        last = ind_df.iloc[-1]
        last_ts = _ts_to_pydatetime(ind_df.index[-1])

        with session_scope(factory) as session:
            sym = (
                session.query(Symbol)
                .filter(Symbol.code == sym_cfg.code)
                .one_or_none()
            )
            if sym is None:
                continue
            _persist_indicator_row(
                session,
                sym.id,
                last_ts,
                {
                    "rsi_14": float(last["rsi_14"])
                    if last.notna()["rsi_14"]
                    else None,
                    "macd": float(last["macd"]) if last.notna()["macd"] else None,
                    "macd_signal": float(last["macd_signal"])
                    if last.notna()["macd_signal"]
                    else None,
                    "macd_hist": float(last["macd_hist"])
                    if last.notna()["macd_hist"]
                    else None,
                    "boll_upper": float(last["boll_upper"])
                    if last.notna()["boll_upper"]
                    else None,
                    "boll_mid": float(last["boll_mid"])
                    if last.notna()["boll_mid"]
                    else None,
                    "boll_lower": float(last["boll_lower"])
                    if last.notna()["boll_lower"]
                    else None,
                },
            )

        sigs = detect_threshold_breach(
            code=sym_cfg.code,
            ts=last_ts,
            close=float(last["close"]),
            upper=sym_cfg.upper_threshold,
            lower=sym_cfg.lower_threshold,
        )
        sigs += detect_tech_signals(
            sym_cfg.code,
            ind_df,
            rsi_overbought=cfg.signals.rsi_overbought,
            rsi_oversold=cfg.signals.rsi_oversold,
        )
        all_sigs.extend(sigs)

    deduped = deduplicate(
        all_sigs, window_minutes=cfg.signals.dedupe_window_minutes
    )

    with session_scope(factory) as session:
        ids = _persist_signal_rows(session, deduped)
    log.info("intraday_check.signals", n=len(deduped), persisted=len(ids))

    crit, warn, _info = split_by_severity(deduped)
    pushed = 0

    for sig in crit:
        card = render_signal_alert(
            code=sig.code,
            ts=sig.ts if sig.ts.tzinfo else sig.ts.replace(tzinfo=timezone.utc),
            close=float(sig.payload.get("close", 0.0)),
            change_pct=0.0,
            signals=[sig],
        )
        try:
            msg_id = send_card_fn(
                card, cfg.lark.receiver.open_id, cfg.lark.receiver.type
            )
            pushed += 1
            log.info("intraday_check.push", code=sig.code, msg_id=msg_id)
        except Exception as e:
            log.error("intraday_check.push_failed", code=sig.code, error=str(e))

    if warn:
        by_code: dict[str, list[Signal]] = {}
        for s in warn:
            by_code.setdefault(s.code, []).append(s)
        for code, sigs in by_code.items():
            close = next(
                iter(s.payload.get("close", 0.0) for s in sigs if "close" in s.payload),
                0.0,
            )
            ts_for_card = (
                sigs[0].ts
                if sigs[0].ts.tzinfo
                else sigs[0].ts.replace(tzinfo=timezone.utc)
            )
            card = render_signal_alert(
                code=code,
                ts=ts_for_card,
                close=float(close),
                change_pct=0.0,
                signals=sigs,
            )
            try:
                msg_id = send_card_fn(
                    card, cfg.lark.receiver.open_id, cfg.lark.receiver.type
                )
                pushed += 1
                log.info(
                    "intraday_check.push", code=code, count=len(sigs), msg_id=msg_id
                )
            except Exception as e:
                log.error("intraday_check.push_failed", code=code, error=str(e))

    return {"quotes": inserted_quotes, "signals": len(deduped), "pushed": pushed}


def _aggregate_signal_count(session, sym_id: int, since: datetime) -> int:
    return (
        session.query(SignalRow)
        .filter(SignalRow.symbol_id == sym_id, SignalRow.ts >= since)
        .count()
    )


def run_brief(
    *,
    kind: str,
    client: FutuClient,
    factory: sessionmaker,
    cfg: AppConfig,
    watchlist: WatchlistConfig,
    now_utc: datetime | None = None,
    send_card_fn: SendCardFn = _default_sender,
) -> dict[str, int]:
    """Render and push a daily brief Lark card.

    `kind` is the human-readable label ("开盘后1h盘点" / "收盘盘点").
    Aggregates today's signal count per symbol from DB.
    """
    now_utc = now_utc or datetime.now(tz=timezone.utc)
    codes = [s.code for s in watchlist.symbols]

    snaps = {s.code: s for s in client.snapshot(codes)}

    rows: list[dict[str, Any]] = []
    summary_lines: list[str] = []
    today_start = datetime.combine(now_utc.date(), datetime.min.time())

    with session_scope(factory) as session:
        for sc in watchlist.symbols:
            snap = snaps.get(sc.code)
            if snap is None:
                continue
            change_pct = (
                (snap.last_price - snap.open_price) / snap.open_price
                if snap.open_price
                else 0.0
            )
            sym = (
                session.query(Symbol)
                .filter(Symbol.code == sc.code)
                .one_or_none()
            )
            sig_count = (
                _aggregate_signal_count(session, sym.id, today_start) if sym else 0
            )
            rows.append(
                {
                    "code": sc.code,
                    "close": snap.last_price,
                    "change_pct": change_pct,
                    "signal_count": sig_count,
                }
            )

    if rows:
        gainers = sorted(rows, key=lambda r: r["change_pct"], reverse=True)[:3]
        losers = sorted(rows, key=lambda r: r["change_pct"])[:3]
        summary_lines.append(
            "Top 涨: "
            + ", ".join(f"{r['code']} {r['change_pct']:+.2%}" for r in gainers)
        )
        summary_lines.append(
            "Top 跌: "
            + ", ".join(f"{r['code']} {r['change_pct']:+.2%}" for r in losers)
        )

    card = render_daily_brief(
        kind=kind,
        date_str=now_utc.strftime("%Y-%m-%d"),
        rows=rows,
        summary_lines=summary_lines,
    )
    pushed = 0
    try:
        msg_id = send_card_fn(
            card, cfg.lark.receiver.open_id, cfg.lark.receiver.type
        )
        pushed = 1
        log.info("brief.push", kind=kind, msg_id=msg_id)
    except Exception as e:
        log.error("brief.push_failed", kind=kind, error=str(e))

    return {"rows": len(rows), "pushed": pushed}


def run_morning_brief(**kw: Any) -> dict[str, int]:
    return run_brief(kind="开盘后1h盘点", **kw)


def run_closing_brief(**kw: Any) -> dict[str, int]:
    return run_brief(kind="收盘盘点", **kw)
