from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import structlog
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from vibe_trader.config import AppConfig, WatchlistConfig
from vibe_trader.data.indicators import compute_indicators
from vibe_trader.data.kline import fetch_kline_df
from vibe_trader.data.quotes import sync_snapshots
from vibe_trader.db import session_scope
from vibe_trader.futu_client import FutuClient
from vibe_trader.models import Indicator, Position
from vibe_trader.models import Signal as SignalRow
from vibe_trader.models import Symbol
from vibe_trader.models import Trade
from vibe_trader.reports.interpret import (
    IndicatorReading,
    PositionSummary,
    ReturnSummary,
    build_diagnostics_md,
    reading_from_row,
)
from vibe_trader.reports.lark import send_card
from vibe_trader.reports.lark_image import send_image as _send_image
from vibe_trader.reports.render import (
    render_daily_brief,
    render_signal_alert,
)
from vibe_trader.reports.snapshot import (
    SnapshotRequest,
    TradeMarker,
    render_snapshot,
)
from vibe_trader.signals.base import Signal
from vibe_trader.signals.compose import deduplicate, split_by_severity
from vibe_trader.signals.strategy_base import (
    Strategy,
    StrategyContext,
    build_strategy,
)
from vibe_trader.signals.strategy_lite import SignalSuggest
import vibe_trader.signals.strategy_rule  # noqa: F401  (registers "rule")
import vibe_trader.signals.strategy_llm   # noqa: F401  (registers "llm")
import vibe_trader.signals.strategy_hitl  # noqa: F401  (registers "hitl")
from vibe_trader.signals.tech import detect_tech_signals
from vibe_trader.journal import (
    JournalEntry,
    append_event,
    refresh_overview_only,
)
from vibe_trader.journal.errors import (
    render_probe_lines,
    scan_recent_failures,
)
from vibe_trader.journal.metrics import (
    compute_hit_rates,
    render_hit_rate_lines,
)
from vibe_trader.journal.writer import compute_overview
from vibe_trader.signals.threshold import detect_threshold_breach


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


SendImageFn = Callable[[Path, str, str], str]


def _make_default_image_sender(
    cli_path: str = "lark-cli", identity: str = "bot"
) -> SendImageFn:
    """Build a default image sender bound to lark-cli path and identity."""

    def _sender(path: Path, open_id: str, receiver_type: str) -> str:
        return _send_image(
            path,
            open_id=open_id,
            receiver_type=receiver_type,  # type: ignore[arg-type]
            cli_path=cli_path,
            identity=identity,  # type: ignore[arg-type]
        )

    return _sender


_default_image_sender: SendImageFn = _make_default_image_sender()


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
        # NOTE: SQLAlchemy on SQLite emits a fake `inserted_primary_key`
        # even for ON CONFLICT DO NOTHING rows (it returns the cursor's
        # lastrowid which is the most recent *real* insert). Gate on
        # rowcount instead so we only count rows that actually changed.
        if result.rowcount > 0 and result.inserted_primary_key:
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


def _load_open_positions_full(session) -> dict[str, tuple[int, float, float]]:
    """Return {code: (qty, avg_cost, realized_pnl)} for ALL Position rows.

    Includes zero-qty positions (closed) so `realized_pnl` history isn't
    lost; the StrategyContext consumer is expected to ignore qty=0.

    Used by `_run_strategy_per_code` to populate `StrategyContext.avg_cost`
    / `StrategyContext.realized_pnl` — the LLM strategy reads these to
    explain HOLD/SELL recommendations like "已盈利 12% 落袋为安".
    """
    rows = (
        session.query(Symbol.code, Position.qty, Position.avg_cost, Position.realized_pnl)
        .join(Position, Position.symbol_id == Symbol.id)
        .all()
    )
    return {code: (qty, avg, real or 0.0) for code, qty, avg, real in rows}


def _compute_overview_decorations(
    *, code: str, factory: sessionmaker, audit_log_path_str: str | None,
    now_utc: datetime,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Build the two optional bullet-line groups for the overview block.

    Returns (hit_rate_lines, error_probe_lines). Either may be empty —
    `render_overview` then omits the corresponding section.

    Anything that raises here is swallowed: a missing audit log or an
    unreadable Quote table just means the overview lacks the extra
    sections this tick — it must NOT prevent journal write.
    """
    hit_lines: list[str] = []
    probe_lines: list[str] = []
    if audit_log_path_str:
        path = Path(audit_log_path_str)
        try:
            stats = compute_hit_rates(
                audit_log_path=path,
                factory=factory,
                code=code,
                cutoff=now_utc,
            )
            hit_lines = render_hit_rate_lines(stats)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("journal.metrics_failed", code=code, error=repr(e))
        try:
            probe = scan_recent_failures(audit_log_path=path, code=code)
            probe_lines = render_probe_lines(probe)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("journal.error_probe_failed", code=code, error=repr(e))
    return tuple(hit_lines), tuple(probe_lines)


def _write_journal_entry(
    *,
    code: str,
    code_to_name: dict[str, str | None],
    ts_for_card: datetime,
    signals: list[Signal],
    suggestion: SignalSuggest | None,
    indicator_readings: dict[str, IndicatorReading],
    return_summaries: dict[str, ReturnSummary],
    snapshots_by_code: dict[str, Any],
    position_details: dict[str, tuple[int, float, float]],
    watchlist_by_code: dict[str, tuple[float | None, float | None]],
    png_path: Path | None,
    journal_dir: Path,
    audit_log_path_str: str | None,
    factory: sessionmaker | None = None,
    now_utc: datetime | None = None,
) -> None:
    """Build a JournalEntry + OverviewSnapshot for one code and append.

    Pure orchestration — pulls fields from the cron loop's closure-state
    dicts and hands them to the journal module. Never raises (the caller
    catches; we keep this function side-effect-only for clarity).
    """
    ind = indicator_readings.get(code)
    ret = return_summaries.get(code)
    snap = snapshots_by_code.get(code)
    qty, avg_cost, _real = position_details.get(code, (0, 0.0, 0.0))
    upper, lower = watchlist_by_code.get(code, (None, None))

    last_price = float(snap.last_price) if snap is not None else None
    intraday_pct = ret.intraday if ret is not None else None

    unrealized: float | None = None
    if qty > 0 and last_price is not None and avg_cost:
        unrealized = (last_price - avg_cost) * qty

    chart_rel: str | None = None
    if png_path is not None:
        try:
            chart_rel = str(Path(png_path).resolve().relative_to(Path.cwd().resolve()))
        except ValueError:
            chart_rel = str(png_path)

    entry = JournalEntry(
        code=code,
        ts=ts_for_card,
        last_price=last_price,
        intraday_pct=intraday_pct,
        last_30_bar_pct=(ret.last_30_bars if ret is not None else None),
        rsi_14=(ind.rsi_14 if ind is not None else None),
        macd=(ind.macd if ind is not None else None),
        macd_signal=(ind.macd_signal if ind is not None else None),
        macd_hist=(ind.macd_hist if ind is not None else None),
        boll_upper=(ind.boll_upper if ind is not None else None),
        boll_mid=(ind.boll_mid if ind is not None else None),
        boll_lower=(ind.boll_lower if ind is not None else None),
        position_qty=qty,
        avg_cost=(avg_cost if qty > 0 else None),
        unrealized_pnl=unrealized,
        signals=signals,
        suggestion=suggestion,
        audit_log_path=audit_log_path_str,
        chart_image_path=chart_rel,
    )

    hit_lines, probe_lines = (
        _compute_overview_decorations(
            code=code,
            factory=factory,
            audit_log_path_str=audit_log_path_str,
            now_utc=now_utc or ts_for_card,
        )
        if factory is not None
        else ((), ())
    )

    overview = compute_overview(
        code=code,
        display_name=code_to_name.get(code),
        last_check_ts=ts_for_card,
        last_price=last_price,
        intraday_pct=intraday_pct,
        upper_threshold=upper,
        lower_threshold=lower,
        position_qty=qty,
        avg_cost=(avg_cost if qty > 0 else None),
        unrealized_pnl=unrealized,
        journal_dir=journal_dir,
        new_entry=entry,
        hit_rate_lines=hit_lines,
        error_probe_lines=probe_lines,
    )

    append_event(journal_dir=journal_dir, overview=overview, entry=entry)


def _refresh_journal_overview_for_quiet_code(
    *,
    code: str,
    code_to_name: dict[str, str | None],
    now_utc: datetime,
    snapshots_by_code: dict[str, Any],
    return_summaries: dict[str, ReturnSummary],
    position_details: dict[str, tuple[int, float, float]],
    watchlist_by_code: dict[str, tuple[float | None, float | None]],
    journal_dir: Path,
    factory: sessionmaker | None = None,
    audit_log_path_str: str | None = None,
) -> None:
    """For a code that produced NO signals this tick: just bump the overview.

    Same Hybrid-mode commitment as `_write_journal_entry` — the file's
    overview always reflects "last check ran successfully at <ts>" so
    the user can verify the loop is alive without grepping logs.
    """
    snap = snapshots_by_code.get(code)
    ret = return_summaries.get(code)
    qty, avg_cost, _real = position_details.get(code, (0, 0.0, 0.0))
    upper, lower = watchlist_by_code.get(code, (None, None))
    last_price = float(snap.last_price) if snap is not None else None
    intraday_pct = ret.intraday if ret is not None else None
    unrealized: float | None = None
    if qty > 0 and last_price is not None and avg_cost:
        unrealized = (last_price - avg_cost) * qty

    hit_lines, probe_lines = (
        _compute_overview_decorations(
            code=code,
            factory=factory,
            audit_log_path_str=audit_log_path_str,
            now_utc=now_utc,
        )
        if factory is not None
        else ((), ())
    )

    overview = compute_overview(
        code=code,
        display_name=code_to_name.get(code),
        last_check_ts=now_utc,
        last_price=last_price,
        intraday_pct=intraday_pct,
        upper_threshold=upper,
        lower_threshold=lower,
        position_qty=qty,
        avg_cost=(avg_cost if qty > 0 else None),
        unrealized_pnl=unrealized,
        journal_dir=journal_dir,
        new_entry=None,
        hit_rate_lines=hit_lines,
        error_probe_lines=probe_lines,
    )
    refresh_overview_only(journal_dir=journal_dir, overview=overview)


def _build_strategy_from_cfg(
    cfg: AppConfig, *, send_card_fn: SendCardFn = _default_sender
) -> Strategy:
    """Resolve `cfg.trader.strategy.type` into a concrete Strategy via the
    Registry (see signals/strategy_base.py).

    The matching sub-block (`cfg.trader.strategy.<type>`) is dumped to dict
    and passed to the registered builder. Unknown strategy types raise
    KeyError listing the registered names.

    For HITL strategy specifically: post-construction we inject a
    callable that converts the strategy's markdown summary into a Lark
    card and pushes it via the configured sender. The strategy uses
    this best-effort to nudge the user when a packet drops.
    """
    sc = cfg.trader.strategy
    sub = getattr(sc, sc.type)
    sub_dict = sub.model_dump()
    # Cross-strategy investor profile is shared at trader-level. LLM uses
    # it to frame the prompt; rule/HITL ignore the unknown key today (and
    # for forward-compat we pop it before HITL so its config dict stays
    # tight).
    profile = cfg.trader.investment_profile
    if sc.type == "llm":
        sub_dict["investment_profile"] = profile
    strat = build_strategy(sc.type, sub_dict)

    if sc.type == "hitl":
        from vibe_trader.signals.strategy_hitl import HITLStrategy

        if isinstance(strat, HITLStrategy):
            recv = cfg.lark.receiver
            strat.lark_push = lambda md_body: send_card_fn(
                _hitl_packet_card(md_body), recv.open_id, recv.type
            )
    return strat


def _hitl_packet_card(md_body: str) -> dict[str, Any]:
    """One-element Lark card carrying the HITL packet summary.

    Inline to keep the templates folder uncluttered; this card has no
    branding, just the raw lark_md body the strategy already produced.
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "yellow",
            "title": {
                "tag": "plain_text",
                "content": "🎯 HITL 决策待办",
            },
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": md_body},
            }
        ],
    }


def _run_strategy_per_code(
    strategy: Strategy,
    sigs_by_code: dict[str, list[Signal]],
    *,
    positions: dict[str, int],
    snapshots_by_code: dict[str, Any] | None = None,
    kline_dfs: dict[str, Any] | None = None,
    position_details: dict[str, tuple[int, float, float]] | None = None,
    return_summaries: dict[str, ReturnSummary] | None = None,
) -> dict[str, SignalSuggest]:
    """Build a `StrategyContext` per code and collect non-None decisions.

    Strategy errors are isolated: a crash on one symbol must not abort the
    rest of the cron tick. Failed symbols simply produce no suggestion.

    Args:
        positions: {code: qty}. Required.
        snapshots_by_code: {code: Snapshot}. Optional — provides
            last_price / open_price for prompt rendering.
        kline_dfs: {code: indicator_df}. Optional — DataFrames already
            computed by `run_intraday_check` (with RSI/MACD/Bollinger
            columns). RuleStrategy ignores this; LLMStrategy reads the
            last bar to fill the indicators block of its prompt.
        position_details: {code: (qty, avg_cost, realized_pnl)}. Optional;
            avg_cost/realized_pnl default to 0.0 when absent.
        return_summaries: {code: ReturnSummary}. Optional — provides
            intraday and 30-bar return percentages.
    """
    snapshots_by_code = snapshots_by_code or {}
    kline_dfs = kline_dfs or {}
    position_details = position_details or {}
    return_summaries = return_summaries or {}
    out: dict[str, SignalSuggest] = {}
    for code, sigs in sigs_by_code.items():
        qty, avg_cost, realized_pnl = position_details.get(
            code, (positions.get(code, 0), 0.0, 0.0)
        )
        ret = return_summaries.get(code)
        ctx = StrategyContext(
            code=code,
            signals=sigs,
            position_qty=qty,
            snapshot=snapshots_by_code.get(code),
            kline_60m=kline_dfs.get(code),
            avg_cost=avg_cost,
            realized_pnl=realized_pnl,
            intraday_return=ret.intraday if ret else None,
            last_30_bar_return=ret.last_30_bars if ret else None,
        )
        try:
            decision = strategy.decide(ctx)
        except Exception as e:
            log.error(
                "strategy.decide_crash",
                strategy=strategy.name,
                code=code,
                exc_type=type(e).__name__,
                error=repr(e),
            )
            continue
        if decision is not None:
            out[code] = decision
    return out


def _execute_suggestions(
    session,
    inserted: dict[int, tuple[str, str]],
    suggestions: dict[str, SignalSuggest],
    paper_trader: Any,
) -> dict[int, int]:
    """Auto-execute BUY/SELL suggestions for newly-inserted signals.

    Idempotency: relies on `_persist_signal_rows` returning ONLY the
    primary-keys of *newly inserted* rows (ON CONFLICT DO NOTHING). A
    repeat run with no new signals yields an empty `inserted` and thus no
    trades.

    Per-symbol dedupe: a SignalSuggest can be triggered by multiple
    signal_types (e.g. RSI oversold + MACD golden cross both feed BUY 50).
    We trade once per code; the highest-priority signal_type (threshold
    breach > tech combo) lands first thanks to deduplicate() ordering.

    Errors are logged and isolated — one rejection / mismatch must not
    abort the rest of the run.

    Returns {signal_id: trade_id} for successfully placed orders.
    """
    from vibe_trader.trader.execute import (
        SignalExecutionError,
        execute_signal_trade,
    )

    executed: dict[int, int] = {}
    done_codes: set[str] = set()

    for sid, (code, signal_type) in inserted.items():
        if code in done_codes:
            log.debug("auto_exec.skip_done_code", sid=sid, code=code)
            continue
        sug = suggestions.get(code)
        if sug is None:
            log.debug("auto_exec.skip_no_suggestion", sid=sid, code=code)
            continue
        if sug.action == "HOLD" or sug.qty <= 0:
            log.debug(
                "auto_exec.skip_hold_or_zero",
                sid=sid,
                code=code,
                action=sug.action,
                qty=sug.qty,
            )
            continue
        if signal_type not in sug.triggering_signal_types:
            log.debug(
                "auto_exec.skip_wrong_type",
                sid=sid,
                code=code,
                signal_type=signal_type,
                triggers=list(sug.triggering_signal_types),
            )
            continue

        sig = session.query(SignalRow).filter(SignalRow.id == sid).one_or_none()
        if sig is None or sig.suggested_action is None:
            log.debug(
                "auto_exec.skip_sig_missing_or_no_action",
                sid=sid,
                code=code,
                sig_present=sig is not None,
            )
            continue
        # Belt-and-suspenders idempotency: SQLAlchemy's ON CONFLICT DO
        # NOTHING on SQLite leaks the existing row's PK back through
        # `inserted_primary_key`, so a re-run can re-surface a signal we
        # already executed. Skip anything not in 'pending'.
        if sig.status != "pending":
            log.debug(
                "auto_exec.skip_non_pending",
                sid=sid,
                code=code,
                status=sig.status,
            )
            continue
        sym = session.query(Symbol).filter(Symbol.id == sig.symbol_id).one()
        log.info(
            "auto_exec.attempting",
            sid=sid,
            code=code,
            side=sug.action,
            qty=sig.suggested_qty or sug.qty,
        )

        try:
            trade_id = execute_signal_trade(
                session, sig, sym, sig.suggested_qty or sug.qty, paper_trader
            )
            executed[sid] = trade_id
            done_codes.add(code)
            log.info(
                "intraday_check.auto_executed",
                signal_id=sid,
                code=code,
                side=sug.action,
                qty=sug.qty,
                trade_id=trade_id,
            )
        except SignalExecutionError as e:
            log.warning(
                "intraday_check.auto_execute_failed",
                signal_id=sid,
                code=code,
                side=sug.action,
                qty=sug.qty,
                error=str(e),
            )
        except Exception as e:
            log.error(
                "intraday_check.auto_execute_crash",
                signal_id=sid,
                code=code,
                exc_type=type(e).__name__,
                error=repr(e),
            )

    return executed


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
    send_image_fn: SendImageFn | None = None,
    snapshot_dir: Path | None = None,
    paper_trader: Any | None = None,
) -> dict[str, int]:
    """One pass of intraday_check.

    Args:
        paper_trader: optional PaperTrader. When supplied AND
            cfg.trader.auto_execute is True, BUY/SELL suggestions for
            newly-inserted signals are placed automatically and recorded
            as Trade/Position rows. When None, suggestions are still
            shown in the alert card but never executed (manual confirm
            via `vibe-trader trade confirm <signal_id>`).

    Returns {'quotes': N, 'signals': M, 'pushed': P, 'executed': E}.
    """
    if now_utc is None:
        now_utc = datetime.now(tz=timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    codes = [s.code for s in watchlist.symbols]

    # Journal lookup tables: per-code display name + threshold pair, used
    # by both the entry-writer and the no-signal overview-refresh path.
    code_to_name: dict[str, str | None] = {
        s.code: s.name for s in watchlist.symbols
    }
    watchlist_by_code: dict[str, tuple[float | None, float | None]] = {
        s.code: (s.upper_threshold, s.lower_threshold) for s in watchlist.symbols
    }
    journal_dir = Path("data/journal")
    audit_log_path_str: str | None = None
    if cfg.trader.strategy.type == "llm":
        audit_log_path_str = cfg.trader.strategy.llm.audit_log_path or None

    inserted_quotes = sync_snapshots(client, factory, codes=codes)
    snapshots_by_code = {s.code: s for s in client.snapshot(codes)}

    all_sigs: list[Signal] = []
    # Cache per-code data for card decoration AND strategy ctx expansion.
    indicator_readings: dict[str, IndicatorReading] = {}
    return_summaries: dict[str, ReturnSummary] = {}
    kline_dfs: dict[str, Any] = {}

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

        # Capture indicator + return readings for card diagnostics
        indicator_readings[sym_cfg.code] = reading_from_row(
            last.to_dict(), close=float(last["close"])
        )
        snap = snapshots_by_code.get(sym_cfg.code)
        intraday_pct: float | None = None
        if snap is not None and snap.open_price:
            intraday_pct = (snap.last_price - snap.open_price) / snap.open_price
        last_30_pct: float | None = None
        if len(ind_df) > 30:
            ref_close = float(ind_df.iloc[-31]["close"])
            if ref_close > 0:
                last_30_pct = (float(last["close"]) - ref_close) / ref_close
        return_summaries[sym_cfg.code] = ReturnSummary(
            intraday=intraday_pct, last_30_bars=last_30_pct
        )
        kline_dfs[sym_cfg.code] = ind_df

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

        # Threshold breach uses LIVE snapshot price (not stale kline close)
        snap_for_threshold = snapshots_by_code.get(sym_cfg.code)
        threshold_price = (
            float(snap_for_threshold.last_price)
            if snap_for_threshold is not None
            else float(last["close"])
        )
        sigs = detect_threshold_breach(
            code=sym_cfg.code,
            ts=last_ts,
            close=threshold_price,
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

    strategy = _build_strategy_from_cfg(cfg, send_card_fn=send_card_fn)
    executed: dict[int, int] = {}
    with session_scope(factory) as session:
        positions = _load_open_positions(session)
        position_details = _load_open_positions_full(session)
        suggestions = _run_strategy_per_code(
            strategy,
            sigs_by_code,
            positions=positions,
            snapshots_by_code=snapshots_by_code,
            kline_dfs=kline_dfs,
            position_details=position_details,
            return_summaries=return_summaries,
        )
        ids = _persist_signal_rows(session, deduped, suggestions=suggestions)
        if paper_trader is not None and cfg.trader.auto_execute:
            executed = _execute_suggestions(
                session, ids, suggestions, paper_trader
            )
    log.info(
        "intraday_check.signals",
        n=len(deduped),
        persisted=len(ids),
        suggested=len([s for s in suggestions.values() if s.action != "HOLD"]),
        executed=len(executed),
    )

    # Build {(code, signal_type) -> signal_id} for card decoration
    sigid_by_key: dict[tuple[str, str], int] = {key: sid for sid, key in ids.items()}

    # Load current paper positions once for position-summary card decoration.
    with session_scope(factory) as session:
        sym_lookup = {s.code: s.id for s in session.query(Symbol).all()}
        pos_rows = (
            session.query(Position).filter(Position.qty > 0).all()
            if sym_lookup
            else []
        )
        positions_by_code: dict[str, tuple[int, float]] = {}
        sym_id_to_code = {sid: code for code, sid in sym_lookup.items()}
        for p in pos_rows:
            code = sym_id_to_code.get(p.symbol_id)
            if code:
                positions_by_code[code] = (p.qty, p.avg_cost)

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

        # Compose diagnostics block (indicators + returns + position).
        ind_reading = indicator_readings.get(code)
        ret_summary = return_summaries.get(code)
        pos_summary: PositionSummary | None = None
        if code in positions_by_code:
            qty, avg = positions_by_code[code]
            snap = snapshots_by_code.get(code)
            if snap is not None:
                pos_summary = PositionSummary(
                    qty=qty, avg_cost=avg, mark=snap.last_price
                )
        diagnostics_md = build_diagnostics_md(
            indicator=ind_reading,
            returns=ret_summary,
            position=pos_summary,
        )
        # Use intraday change for header line if available
        change_pct_for_header = (
            ret_summary.intraday if ret_summary and ret_summary.intraday is not None
            else 0.0
        )

        card = render_signal_alert(
            code=code,
            ts=ts_for_card,
            close=float(close),
            change_pct=change_pct_for_header,
            signals=sigs,
            signal_ids=signal_ids,
            suggestion=sug_card_arg,
            diagnostics_md=diagnostics_md,
        )
        card_ok = False
        try:
            msg_id = send_card_fn(
                card, cfg.lark.receiver.open_id, cfg.lark.receiver.type
            )
            pushed += 1
            card_ok = True
            log.info(
                "intraday_check.push",
                code=code,
                count=len(sigs),
                suggestion=(sug.action if sug is not None else None),
                qty=(sug.qty if sug is not None else None),
                msg_id=msg_id,
            )
        except Exception as e:
            log.error("intraday_check.push_failed", code=code, error=str(e))

        # Chart snapshot — best-effort. Failures here MUST NOT stop the
        # journal write below; a missing PNG just means the entry has no
        # image link, the rest of the data is still useful.
        png_path: Path | None = None
        if send_image_fn is not None and card_ok:
            try:
                trade_window_start = now_utc - timedelta(days=30)
                markers: list[TradeMarker] = []
                with session_scope(factory) as session:
                    sym = session.query(Symbol).filter(Symbol.code == code).one_or_none()
                    if sym is not None:
                        rows = (
                            session.query(Trade)
                            .filter(
                                Trade.symbol_id == sym.id,
                                Trade.ts >= trade_window_start,
                            )
                            .order_by(Trade.ts.asc())
                            .all()
                        )
                        for r in rows:
                            ts_raw = _ts_to_pydatetime(r.ts)
                            ts_trade = (
                                ts_raw if ts_raw.tzinfo else ts_raw.replace(tzinfo=timezone.utc)
                            )
                            side_normalized = r.side.strip().upper()
                            if side_normalized == "BUY":
                                side_lit: Literal["buy", "sell"] = "buy"
                            elif side_normalized == "SELL":
                                side_lit = "sell"
                            else:
                                log.warning(
                                    "intraday_check.snapshot.unknown_trade_side",
                                    code=code,
                                    trade_id=r.id,
                                    side=r.side,
                                )
                                continue
                            markers.append(
                                TradeMarker(
                                    ts=ts_trade,
                                    side=side_lit,
                                    qty=r.qty,
                                    price=r.price,
                                )
                            )

                avg_cost_chart: float | None = None
                if code in positions_by_code:
                    avg_cost_chart = positions_by_code[code][1]

                snap_live = snapshots_by_code.get(code)
                current_price = (
                    float(snap_live.last_price) if snap_live is not None else None
                )

                # TODO(p4-perf): hoist df cache to avoid 2x kline fetch.
                df_for_chart = fetch_kline_df(client, code, ktype="K_60M", limit=200)
                req = SnapshotRequest(
                    code=code,
                    freq="60m",
                    df=df_for_chart,
                    markers=markers,
                    avg_cost=avg_cost_chart,
                    current_price=current_price,
                    out_dir=snapshot_dir,
                )
                png_path = render_snapshot(req)
                img_msg_id = send_image_fn(
                    png_path, cfg.lark.receiver.open_id, cfg.lark.receiver.type
                )
                log.info(
                    "intraday_check.snapshot_pushed",
                    code=code,
                    msg_id=img_msg_id,
                    markers=len(markers),
                )
            except Exception as e:
                log.error(
                    "intraday_check.snapshot_failed",
                    code=code,
                    exc_type=type(e).__name__,
                    error=str(e),
                    error_repr=repr(e),
                )

        # Journal write — independent of card / chart success.
        try:
            _write_journal_entry(
                code=code,
                code_to_name=code_to_name,
                ts_for_card=ts_for_card,
                signals=sigs,
                suggestion=sug,
                indicator_readings=indicator_readings,
                return_summaries=return_summaries,
                snapshots_by_code=snapshots_by_code,
                position_details=position_details,
                watchlist_by_code=watchlist_by_code,
                png_path=png_path,
                journal_dir=journal_dir,
                audit_log_path_str=audit_log_path_str,
                factory=factory,
                now_utc=now_utc,
            )
        except Exception as e:
            log.error(
                "intraday_check.journal_failed",
                code=code,
                exc_type=type(e).__name__,
                error=str(e),
            )

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

    # Hybrid trigger: codes that produced NO signals this tick still get
    # their overview refreshed so the journal file's "最近检查" line is
    # always current. This is the user's "the loop is alive" indicator.
    touched_by_signals: set[str] = set(crit_by_code.keys()) | {
        c for c in (sigs_by_code.keys()) if c in {s.code for s in deduped}
    }
    for sym_cfg in watchlist.symbols:
        if sym_cfg.code in touched_by_signals:
            continue
        try:
            _refresh_journal_overview_for_quiet_code(
                code=sym_cfg.code,
                code_to_name=code_to_name,
                now_utc=now_utc,
                snapshots_by_code=snapshots_by_code,
                return_summaries=return_summaries,
                position_details=position_details,
                watchlist_by_code=watchlist_by_code,
                journal_dir=journal_dir,
                factory=factory,
                audit_log_path_str=audit_log_path_str,
            )
        except Exception as e:  # never let a journal write break a tick
            log.error(
                "intraday_check.journal_overview_failed",
                code=sym_cfg.code,
                exc_type=type(e).__name__,
                error=str(e),
            )

    return {
        "quotes": inserted_quotes,
        "signals": len(deduped),
        "pushed": pushed,
        "suggestions": sum(
            1 for s in suggestions.values() if s.action != "HOLD"
        ),
        "executed": len(executed),
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


