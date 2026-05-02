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
from equity_monitor.models import Indicator, NewsDigest, Position, SentimentSnapshotRow
from equity_monitor.models import Signal as SignalRow
from equity_monitor.models import Symbol
from equity_monitor.models import Trade
from equity_monitor.data.news import NewsItem, fetch_news_digest
from equity_monitor.data.sentiment import SentimentSnapshot, fetch_sentiment
from equity_monitor.reports.lark import send_card
from equity_monitor.reports.render import (
    render_daily_brief,
    render_news_pulse,
    render_signal_alert,
)
from equity_monitor.signals.base import Signal
from equity_monitor.signals.compose import deduplicate, split_by_severity
from equity_monitor.signals.strategy_lite import (
    SignalSuggest,
    decide_actions_for_codes,
)
from equity_monitor.signals.tech import detect_tech_signals
from equity_monitor.signals.threshold import detect_threshold_breach


log = structlog.get_logger(__name__)


SendCardFn = Callable[[dict[str, Any], str, str], str]


def _make_default_sender(
    cli_path: str = "lark-cli", identity: str = "bot"
) -> SendCardFn:
    """Build a default sender bound to a specific lark-cli path and identity."""

    def _sender(card: dict[str, Any], open_id: str, receiver_type: str) -> str:
        return send_card(
            card,
            open_id=open_id,
            receiver_type=receiver_type,  # type: ignore[arg-type]
            cli_path=cli_path,
            identity=identity,  # type: ignore[arg-type]
        )

    return _sender


_default_sender: SendCardFn = _make_default_sender()


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


def _persist_signal_rows(
    session,
    signals: list[Signal],
    suggestions: dict[str, SignalSuggest] | None = None,
) -> dict[int, tuple[str, str]]:
    """Insert dedup'd signals; return {row_id: (code, signal_type)}.

    If `suggestions[code]` exists AND signal_type is in the suggestion's
    triggering_signal_types, attach suggested_action/qty to the row.
    Status defaults to 'pending'.
    """
    suggestions = suggestions or {}
    inserted: dict[int, tuple[str, str]] = {}
    for s in signals:
        sym = session.query(Symbol).filter(Symbol.code == s.code).one_or_none()
        if sym is None:
            continue
        sug = suggestions.get(s.code)
        suggested_action: str | None = None
        suggested_qty: int | None = None
        if (
            sug is not None
            and s.signal_type in sug.triggering_signal_types
            and sug.action != "HOLD"
        ):
            suggested_action = sug.action
            suggested_qty = sug.qty
        stmt = (
            sqlite_insert(SignalRow)
            .values(
                symbol_id=sym.id,
                ts=s.ts,
                signal_type=s.signal_type,
                severity=s.severity.value,
                payload_json=json.dumps(s.payload),
                delivered=False,
                suggested_action=suggested_action,
                suggested_qty=suggested_qty,
                status="pending",
            )
            .on_conflict_do_nothing(
                index_elements=["symbol_id", "ts", "signal_type"]
            )
        )
        result = session.execute(stmt)
        if result.inserted_primary_key:
            inserted[result.inserted_primary_key[0]] = (s.code, s.signal_type)
    return inserted


def _load_open_positions(session) -> dict[str, int]:
    """Return {code: qty} for non-zero open positions (for strategy decisions)."""
    rows = (
        session.query(Symbol.code, Position.qty)
        .join(Position, Position.symbol_id == Symbol.id)
        .filter(Position.qty > 0)
        .all()
    )
    return {code: qty for code, qty in rows}


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

    # P2: derive trade suggestions BEFORE persisting so they're written atomically
    sigs_by_code: dict[str, list[Signal]] = {}
    for sig in deduped:
        sigs_by_code.setdefault(sig.code, []).append(sig)

    with session_scope(factory) as session:
        positions = _load_open_positions(session)
        suggestions = decide_actions_for_codes(sigs_by_code, positions=positions)
        ids = _persist_signal_rows(session, deduped, suggestions=suggestions)
    log.info(
        "intraday_check.signals",
        n=len(deduped),
        persisted=len(ids),
        suggested=len([s for s in suggestions.values() if s.action != "HOLD"]),
    )

    # Build {(code, signal_type) -> signal_id} for card decoration
    sigid_by_key: dict[tuple[str, str], int] = {key: sid for sid, key in ids.items()}

    crit, warn, _info = split_by_severity(deduped)
    pushed = 0

    def _push_for_code(code: str, sigs: list[Signal]) -> None:
        nonlocal pushed
        close = next(
            iter(s.payload.get("close", 0.0) for s in sigs if "close" in s.payload),
            0.0,
        )
        ts_for_card = (
            sigs[0].ts
            if sigs[0].ts.tzinfo
            else sigs[0].ts.replace(tzinfo=timezone.utc)
        )
        sug = suggestions.get(code)
        # Find the signal_id for the most-actionable triggering signal_type.
        sug_card_arg = None
        signal_ids = [
            sigid_by_key[(s.code, s.signal_type)]
            for s in sigs
            if (s.code, s.signal_type) in sigid_by_key
        ]
        if sug is not None:
            trigger_id = next(
                (
                    sigid_by_key[(code, st)]
                    for st in sug.triggering_signal_types
                    if (code, st) in sigid_by_key
                ),
                None,
            )
            sug_card_arg = {
                "action": sug.action,
                "qty": sug.qty,
                "reason": sug.reason,
                "signal_id": trigger_id,
            }

        card = render_signal_alert(
            code=code,
            ts=ts_for_card,
            close=float(close),
            change_pct=0.0,
            signals=sigs,
            signal_ids=signal_ids,
            suggestion=sug_card_arg,
        )
        try:
            msg_id = send_card_fn(
                card, cfg.lark.receiver.open_id, cfg.lark.receiver.type
            )
            pushed += 1
            log.info(
                "intraday_check.push",
                code=code,
                count=len(sigs),
                has_suggestion=sug is not None,
                msg_id=msg_id,
            )
        except Exception as e:
            log.error("intraday_check.push_failed", code=code, error=str(e))

    crit_by_code: dict[str, list[Signal]] = {}
    for s in crit:
        crit_by_code.setdefault(s.code, []).append(s)
    for code, sigs in crit_by_code.items():
        _push_for_code(code, sigs)

    if warn:
        warn_by_code: dict[str, list[Signal]] = {}
        for s in warn:
            warn_by_code.setdefault(s.code, []).append(s)
        for code, sigs in warn_by_code.items():
            if code in crit_by_code:
                continue  # already pushed in crit batch
            _push_for_code(code, sigs)

    return {
        "quotes": inserted_quotes,
        "signals": len(deduped),
        "pushed": pushed,
        "suggestions": sum(
            1 for s in suggestions.values() if s.action != "HOLD"
        ),
    }


def _build_pnl_lines(
    factory: sessionmaker,
    snaps: dict[str, Any],
    today_start: datetime,
) -> list[str]:
    """Compose Paper P&L lines for brief cards.

    One line per open position with mark-to-market unrealized P&L plus a
    final aggregate (today's fills + cumulative realized).
    """
    lines: list[str] = []
    with session_scope(factory) as session:
        sym_by_id = {s.id: s for s in session.query(Symbol).all()}
        positions = (
            session.query(Position).filter(Position.qty > 0).all()
        )
        if not positions and not (
            session.query(Trade).filter(Trade.ts >= today_start).count()
        ):
            return lines  # nothing trade-related to show

        total_unreal = 0.0
        for p in positions:
            sym = sym_by_id.get(p.symbol_id)
            if sym is None:
                continue
            snap = snaps.get(sym.code)
            mark = snap.last_price if snap else None
            if mark is not None:
                unreal = (mark - p.avg_cost) * p.qty
                total_unreal += unreal
                lines.append(
                    f"{sym.code} +{p.qty}@${p.avg_cost:.2f}  浮盈 {unreal:+.0f}  "
                    f"(mark ${mark:.2f})"
                )
            else:
                lines.append(f"{sym.code} +{p.qty}@${p.avg_cost:.2f}  (mark n/a)")

        today_fills = (
            session.query(Trade).filter(Trade.ts >= today_start).count()
        )
        total_realized = sum(p.realized_pnl or 0.0 for p in positions)
        lines.append(
            f"今日成交: {today_fills} 笔 · 已实现累计 {total_realized:+.0f}  "
            f"· 浮盈合计 {total_unreal:+.0f}"
        )
    return lines


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

    pnl_lines = _build_pnl_lines(factory, snaps, today_start)

    card = render_daily_brief(
        kind=kind,
        date_str=now_utc.strftime("%Y-%m-%d"),
        rows=rows,
        summary_lines=summary_lines,
        pnl_lines=pnl_lines,
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


def _persist_news(
    session: Any, sym_id: int, items: list[NewsItem]
) -> int:
    n = 0
    for it in items:
        stmt = (
            sqlite_insert(NewsDigest)
            .values(
                symbol_id=sym_id,
                ts=it.ts,
                source=it.source,
                title=it.title,
                url=it.url,
                summary=it.summary,
                sentiment_score=None,
            )
            .on_conflict_do_nothing(index_elements=["symbol_id", "url"])
        )
        result = session.execute(stmt)
        if result.rowcount and result.rowcount > 0:
            n += 1
    return n


FetchNewsFn = Callable[[list[str]], list[NewsItem]]
FetchSentFn = Callable[[list[str]], list[SentimentSnapshot]]


def _load_latest_sentiment_per_symbol(
    session: Any, sym_ids: list[int]
) -> dict[int, float]:
    """Return a {symbol_id: latest_temperature} map.

    Uses ORDER BY ts DESC + first match per symbol_id.
    """
    if not sym_ids:
        return {}
    rows = (
        session.query(SentimentSnapshotRow)
        .filter(SentimentSnapshotRow.symbol_id.in_(sym_ids))
        .order_by(SentimentSnapshotRow.symbol_id, SentimentSnapshotRow.ts.desc())
        .all()
    )
    out: dict[int, float] = {}
    for r in rows:
        out.setdefault(r.symbol_id, r.temperature)
    return out


def _persist_sentiment(
    session: Any, sym_id: int, snap: SentimentSnapshot
) -> None:
    stmt = (
        sqlite_insert(SentimentSnapshotRow)
        .values(
            symbol_id=sym_id,
            ts=snap.ts,
            temperature=snap.temperature,
            bullish_pct=snap.bullish_pct,
            bearish_pct=snap.bearish_pct,
            sample_size=snap.sample_size,
        )
        .on_conflict_do_nothing(index_elements=["symbol_id", "ts"])
    )
    session.execute(stmt)


def run_news_pulse(
    *,
    factory: sessionmaker,
    cfg: AppConfig,
    watchlist: WatchlistConfig,
    fetch_news: FetchNewsFn = fetch_news_digest,
    fetch_sent: FetchSentFn = fetch_sentiment,
    sentiment_history: dict[str, float] | None = None,
    send_card_fn: SendCardFn = _default_sender,
) -> dict[str, int]:
    """Pull news + sentiment; persist news; push pulse card on burst events.

    Baseline source:
      - If `sentiment_history` is given (test/manual override), use that dict
        for the previous-temp lookup. The caller is responsible for state.
      - If `sentiment_history` is None (default), load the latest temperature
        per symbol from the `sentiment_snapshots` table, then persist each
        new observation back to that table — so a runner restart preserves
        baseline.

    On first observation (no prior row in DB / dict), seed and skip push.
    """
    use_db = sentiment_history is None
    codes = [s.code for s in watchlist.symbols]

    news_items = fetch_news(codes)
    sent_now = {s.code: s for s in fetch_sent(codes)}

    inserted_news = 0
    pushed = 0
    with session_scope(factory) as session:
        sym_by_code = {
            s.code: s
            for s in session.query(Symbol).filter(Symbol.code.in_(codes))
        }
        by_code: dict[str, list[NewsItem]] = {}
        for it in news_items:
            by_code.setdefault(it.code, []).append(it)
        for code, items in by_code.items():
            sym = sym_by_code.get(code)
            if sym is None:
                continue
            inserted_news += _persist_news(session, sym.id, items)

        if use_db:
            sym_ids = [s.id for s in sym_by_code.values()]
            prev_by_id = _load_latest_sentiment_per_symbol(session, sym_ids)
            prev_by_code = {
                code: prev_by_id.get(sym.id) for code, sym in sym_by_code.items()
            }
        else:
            prev_by_code = dict(sentiment_history)  # type: ignore[arg-type]

        for code, snap in sent_now.items():
            sym = sym_by_code.get(code)
            if sym is None:
                continue
            prev = prev_by_code.get(code)

            if prev is None:
                if use_db:
                    _persist_sentiment(session, sym.id, snap)
                else:
                    sentiment_history[code] = snap.temperature  # type: ignore[index]
                continue

            delta = snap.temperature - prev
            direction: str | None = None
            if delta <= -cfg.signals.news_burst_drop:
                direction = "negative"
            elif delta >= cfg.signals.news_burst_rise:
                direction = "positive"
            if direction:
                titles = [it.title for it in news_items if it.code == code][:3]
                card = render_news_pulse(
                    code=code,
                    direction=direction,
                    temp_now=snap.temperature,
                    temp_prev=prev,
                    news_titles=titles,
                )
                try:
                    msg_id = send_card_fn(
                        card, cfg.lark.receiver.open_id, cfg.lark.receiver.type
                    )
                    pushed += 1
                    log.info(
                        "news_pulse.push",
                        code=code,
                        dir=direction,
                        msg_id=msg_id,
                    )
                except Exception as e:
                    log.error("news_pulse.push_failed", code=code, error=str(e))

            if use_db:
                _persist_sentiment(session, sym.id, snap)
            else:
                sentiment_history[code] = snap.temperature  # type: ignore[index]

    return {"news_inserted": inserted_news, "pushed": pushed}
