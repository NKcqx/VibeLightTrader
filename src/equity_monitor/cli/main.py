from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from equity_monitor.config import load_settings, load_watchlist
from equity_monitor.events.grammar import ALLOWED_CHART_FREQS
from equity_monitor.data.backfill import backfill_all
from equity_monitor.db import init_schema, make_engine, make_sessionmaker, session_scope
from equity_monitor.futu_client import OpenDClient
from equity_monitor.models import Position, Symbol
from equity_monitor.models import Signal as SignalRow
from equity_monitor.models import Trade
from equity_monitor.scheduler.jobs import (
    run_closing_brief,
    run_intraday_check,
    run_morning_brief,
    run_news_pulse,
)
from equity_monitor.scheduler.runner import run_forever


@click.group()
@click.option(
    "--settings",
    "settings_path",
    default="config/settings.yaml",
    show_default=True,
    type=click.Path(),
)
@click.option(
    "--watchlist",
    "watchlist_path",
    default="config/watchlist.yaml",
    show_default=True,
    type=click.Path(),
)
@click.pass_context
def cli(ctx: click.Context, settings_path: str, watchlist_path: str) -> None:
    """Equity Monitor — hourly US-equity monitor with Lark alerts."""
    ctx.ensure_object(dict)
    # Lazy: store paths only; load configs on first access in subcommand
    # so `--help` works even if config files don't exist yet.
    ctx.obj["settings_path"] = settings_path
    ctx.obj["watchlist_path"] = watchlist_path


def _get_cfg(ctx: click.Context) -> Any:
    if "cfg" not in ctx.obj:
        path = ctx.obj["settings_path"]
        if not Path(path).exists():
            raise click.UsageError(
                f"Settings file not found: {path!r}. "
                f"Either `cd` into the equity-monitor repo (so 'config/settings.yaml' "
                f"resolves), or pass an absolute path: "
                f"`equity-monitor --settings /abs/path/to/settings.yaml ...`."
            )
        ctx.obj["cfg"] = load_settings(path)
    return ctx.obj["cfg"]


def _get_watchlist(ctx: click.Context) -> Any:
    if "watchlist" not in ctx.obj:
        ctx.obj["watchlist"] = load_watchlist(ctx.obj["watchlist_path"])
    return ctx.obj["watchlist"]


def _make_factory(cfg: Any) -> Any:
    Path(cfg.database.path).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(cfg.database.path, wal_mode=cfg.database.wal_mode)
    init_schema(engine)
    return make_sessionmaker(engine)


@cli.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Start the long-running scheduler (blocking; SIGINT/SIGTERM to stop)."""
    run_forever(_get_cfg(ctx), _get_watchlist(ctx))


@cli.command()
@click.option(
    "--backend",
    type=click.Choice(["websocket", "polling"]),
    default="websocket",
    show_default=True,
    help=(
        "websocket = lark-cli event +subscribe long-connection (recommended; "
        "needs im.message.receive_v1 registered under 事件与回调). "
        "polling = adaptive p2p chat-history polling (3s after activity, 10s "
        "idle); fallback when websocket can't be configured. "
        "Note: only one websocket subscriber per bot — kill stray "
        "lark-cli event processes first or you'll lose events to round-robin."
    ),
)
@click.option(
    "--poll-interval",
    type=int,
    default=10,
    show_default=True,
    help="idle polling interval in seconds (polling backend only).",
)
@click.option(
    "--rich-cards/--text-only",
    default=True,
    show_default=True,
    help=(
        "rich-cards = enrich replies with live OpenD price + RSI/MACD/BOLL "
        "diagnostics in a Lark Interactive Card. text-only = plain markdown."
    ),
)
@click.pass_context
def listen(
    ctx: click.Context, backend: str, poll_interval: int, rich_cards: bool
) -> None:
    """Start the Lark message listener (blocking; SIGINT to stop).

    Listens for user-sent text in p2p chat with the bot and dispatches
    /add /remove /list /threshold /help (plus Chinese natural-language).
    Replies are Lark Interactive Cards with live OpenD price + indicators.
    Pair with `equity-monitor run` in another tmux pane.
    """
    from equity_monitor.events.listener import run_listener

    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    click.echo(
        f"listener starting [backend={backend}, rich_cards={rich_cards}] "
        "(Ctrl-C to stop)…"
    )
    run_listener(
        cfg=cfg,
        factory=factory,
        backend=backend,
        poll_interval=poll_interval,
        rich_cards=rich_cards,
    )


@cli.command()
@click.argument("code", metavar="TICKER")
@click.option(
    "--freq",
    default="60m",
    show_default=True,
    type=click.Choice(sorted(ALLOWED_CHART_FREQS)),
)
@click.option(
    "--out-dir",
    default="var/snapshots",
    show_default=True,
    type=click.Path(),
)
@click.option(
    "--push/--no-push",
    default=False,
    show_default=True,
    help="Also push the PNG to Lark via lark-cli.",
)
@click.option(
    "--no-reconcile",
    is_flag=True,
    default=False,
    help="Skip the broker fill-price reconcile step (faster, but PENDING "
         "MARKET orders won't show their actual fill price on the chart).",
)
@click.pass_context
def chart(
    ctx: click.Context,
    code: str,
    freq: str,
    out_dir: str,
    push: bool,
    no_reconcile: bool,
) -> None:
    """Render a K-line snapshot PNG. Optionally push it to Lark.

    \b
    TICKER is a Futu-style symbol such as US.AAPL, US.NVDA, HK.00700.
    Examples:
      equity-monitor chart US.AAPL
      equity-monitor chart US.NVDA --freq D
      equity-monitor chart US.AAPL --freq 15m --push
    """
    from pathlib import Path

    from equity_monitor.events.apply import apply_chart
    from equity_monitor.events.grammar import ChartCommand, _normalize_code
    from equity_monitor.reports.lark_image import send_image
    from equity_monitor.trader.reconcile import reconcile_pending_fills

    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    out_path_dir = Path(out_dir).resolve()

    if not no_reconcile:
        # Heal MARKET orders whose fill price hasn't been written back yet.
        # Cheap when there's nothing pending; quietly skipped if OpenD's
        # trade ctx isn't reachable so the chart still renders.
        try:
            trader = _make_trader(cfg)
            try:
                r = reconcile_pending_fills(factory, trader)
                if r.candidates:
                    click.echo(
                        f"reconcile: {r.updated}/{r.candidates} pending fills "
                        f"backfilled (matched={r.matched}, errors={r.errors})"
                    )
            finally:
                try:
                    trader.close()
                except Exception:
                    pass
        except Exception as e:
            click.echo(f"reconcile skipped: {e}", err=True)

    client = OpenDClient(cfg.opend.host, cfg.opend.port)
    try:
        text, payload = apply_chart(
            ChartCommand(code=_normalize_code(code), freq=freq),
            factory,
            client=client,
            snapshot_dir=out_path_dir,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass

    click.echo(text)
    click.echo(f"snapshot: {payload.image_path}")

    if push:
        msg_id = send_image(
            payload.image_path,
            open_id=cfg.lark.receiver.open_id,
            receiver_type=cfg.lark.receiver.type,  # type: ignore[arg-type]
            cli_path=cfg.lark.cli_path,
            identity=cfg.lark.identity,  # type: ignore[arg-type]
        )
        click.echo(f"pushed: {msg_id}")


@cli.command()
@click.option(
    "--job",
    type=click.Choice(["intraday", "morning", "closing", "news"]),
    required=True,
    help="Which single job to run.",
)
@click.option(
    "--auto-trade/--no-auto-trade",
    "auto_trade",
    default=None,
    help=(
        "Override cfg.trader.auto_execute for this run only. "
        "Only meaningful for --job intraday."
    ),
)
@click.pass_context
def once(ctx: click.Context, job: str, auto_trade: bool | None) -> None:
    """Run a single job once and print the result dict."""
    cfg = _get_cfg(ctx)
    wl = _get_watchlist(ctx)
    factory = _make_factory(cfg)

    if auto_trade is not None:
        cfg.trader.auto_execute = auto_trade

    if job == "news":
        res = run_news_pulse(factory=factory, cfg=cfg, watchlist=wl)
    else:
        client = OpenDClient(cfg.opend.host, cfg.opend.port)
        paper_trader: Any | None = None
        try:
            if job == "intraday":
                if cfg.trader.auto_execute:
                    from equity_monitor.trader.paper import OpenDSecTrader

                    try:
                        paper_trader = OpenDSecTrader(
                            host=cfg.opend.host, port=cfg.opend.port
                        )
                    except Exception as e:
                        click.echo(
                            f"warning: paper trader init failed; "
                            f"auto-trade skipped this run ({e})",
                            err=True,
                        )
                        paper_trader = None
                res = run_intraday_check(
                    client=client,
                    factory=factory,
                    cfg=cfg,
                    watchlist=wl,
                    paper_trader=paper_trader,
                )
            elif job == "morning":
                res = run_morning_brief(
                    client=client, factory=factory, cfg=cfg, watchlist=wl
                )
            else:
                res = run_closing_brief(
                    client=client, factory=factory, cfg=cfg, watchlist=wl
                )
        finally:
            client.close()
            if paper_trader is not None:
                try:
                    paper_trader.close()
                except Exception:
                    pass
    click.echo(res)


@cli.group()
def watchlist() -> None:
    """Watchlist subcommands."""


@watchlist.command("list")
@click.pass_context
def watchlist_list(ctx: click.Context) -> None:
    """List active symbols in the database."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    with session_scope(factory) as s:
        rows = (
            s.query(Symbol)
            .filter(Symbol.is_active.is_(True))
            .order_by(Symbol.code)
            .all()
        )
        if not rows:
            click.echo("(no active symbols — try `watchlist sync`)")
            return
        for sym in rows:
            click.echo(
                f"{sym.code:12s}  upper={sym.upper_threshold}  "
                f"lower={sym.lower_threshold}  name={sym.name}"
            )


@watchlist.command("sync")
@click.pass_context
def watchlist_sync(ctx: click.Context) -> None:
    """Sync watchlist.yaml → symbols table (idempotent upsert).

    Existing rows have their thresholds/notes overwritten and are reactivated.
    """
    cfg = _get_cfg(ctx)
    wl = _get_watchlist(ctx)
    factory = _make_factory(cfg)

    with session_scope(factory) as s:
        for sc in wl.symbols:
            sym = s.query(Symbol).filter(Symbol.code == sc.code).one_or_none()
            if sym is None:
                s.add(
                    Symbol(
                        code=sc.code,
                        name=sc.name,
                        upper_threshold=sc.upper_threshold,
                        lower_threshold=sc.lower_threshold,
                        notes=sc.notes,
                        is_active=True,
                    )
                )
            else:
                sym.name = sc.name
                sym.upper_threshold = sc.upper_threshold
                sym.lower_threshold = sc.lower_threshold
                sym.notes = sc.notes
                sym.is_active = True
    click.echo(f"synced {len(wl.symbols)} symbols")


@cli.group()
def db() -> None:
    """DB subcommands."""


@db.command("init")
@click.pass_context
def db_init(ctx: click.Context) -> None:
    """Initialize SQLite schema (creates DB file if missing)."""
    cfg = _get_cfg(ctx)
    _make_factory(cfg)
    click.echo(f"initialized {cfg.database.path}")


@db.command("status")
@click.pass_context
def db_status(ctx: click.Context) -> None:
    """Print row counts of the main tables."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    from equity_monitor.models import (
        Indicator,
        NewsDigest,
        Quote,
        SentimentSnapshotRow,
    )
    from equity_monitor.models import Signal as SignalRow

    with session_scope(factory) as s:
        click.echo(f"db.path:           {cfg.database.path}")
        click.echo(f"symbols:           {s.query(Symbol).count()}")
        click.echo(f"  active:          {s.query(Symbol).filter(Symbol.is_active.is_(True)).count()}")
        click.echo(f"quotes:            {s.query(Quote).count()}")
        click.echo(f"indicators:        {s.query(Indicator).count()}")
        click.echo(f"signals:           {s.query(SignalRow).count()}")
        click.echo(f"news_digest:       {s.query(NewsDigest).count()}")
        click.echo(f"sentiment_snapshots: {s.query(SentimentSnapshotRow).count()}")


# ---------------------------------------------------------------------------
# Phase 2: paper trading CLI
# ---------------------------------------------------------------------------


def _make_trader(cfg: Any) -> Any:
    """Create the real OpenD-backed paper trader. Tests patch this."""
    from equity_monitor.trader.paper import OpenDSecTrader

    return OpenDSecTrader(host=cfg.opend.host, port=cfg.opend.port)


# ---------------------------------------------------------------------------
# `analyze` — user-triggered LLM analysis (vs the cron-triggered pipeline)
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--code",
    "codes",
    multiple=True,
    help="Symbol code (e.g. US.NVDA). Repeat to analyze several. "
    "Default: full active watchlist.",
)
@click.option(
    "--horizon-min",
    type=int,
    default=None,
    help="Min holding months (override cfg.trader.investment_profile).",
)
@click.option(
    "--horizon-max",
    type=int,
    default=None,
    help="Max holding months (override cfg.trader.investment_profile).",
)
@click.option(
    "--budget",
    type=float,
    default=None,
    help="Per-symbol budget in USD (override cfg).",
)
@click.option(
    "--drawdown",
    type=float,
    default=None,
    help="Drawdown tolerance % (override cfg).",
)
@click.option(
    "--theme",
    type=str,
    default=None,
    help="Free-text thesis (override cfg.theme).",
)
@click.option(
    "--execute",
    is_flag=True,
    default=False,
    help="Place paper orders for non-HOLD decisions (writes Signal+Trade rows, "
    "submits to OpenD SIMULATE account). Skipped on parse/LLM errors.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Print JSON results instead of human-friendly output.",
)
@click.pass_context
def analyze(
    ctx: click.Context,
    codes: tuple[str, ...],
    horizon_min: int | None,
    horizon_max: int | None,
    budget: float | None,
    drawdown: float | None,
    theme: str | None,
    execute: bool,
    json_output: bool,
) -> None:
    """Run LLM analysis on demand (no signal trigger required).

    Examples:

      equity-monitor analyze
      equity-monitor analyze --code US.NVDA --code US.MSFT
      equity-monitor analyze --code US.NVDA --budget 30000 --drawdown 15
      equity-monitor analyze --execute       # main use case: act on the LLM
    """
    import json as _json
    from datetime import datetime as _dt

    from equity_monitor.analyze import analyze_symbols

    cfg = _get_cfg(ctx)
    wl = _get_watchlist(ctx)
    factory = _make_factory(cfg)

    if codes:
        target_codes = list(codes)
    else:
        target_codes = [s.code for s in wl.symbols]

    overrides: dict[str, Any] = {}
    if horizon_min is not None:
        overrides["horizon_months_min"] = horizon_min
    if horizon_max is not None:
        overrides["horizon_months_max"] = horizon_max
    if budget is not None:
        overrides["budget_per_symbol_usd"] = budget
    if drawdown is not None:
        overrides["drawdown_tolerance_pct"] = drawdown
    if theme is not None:
        overrides["theme"] = theme

    with session_scope(factory) as s:
        results = analyze_symbols(
            s,
            cfg=cfg,
            codes=target_codes,
            profile_overrides=overrides or None,
        )

        if json_output:
            payload = []
            for r in results:
                d = r.decision
                payload.append(
                    {
                        "code": r.code,
                        "name": r.name,
                        "last_close": r.last_close,
                        "position_qty": r.position_qty,
                        "avg_cost": r.avg_cost,
                        "decision": (
                            None if d is None else {
                                "action": d.action,
                                "qty": d.qty,
                                "confidence": d.confidence,
                                "reason": d.reason,
                            }
                        ),
                        "latency_ms": r.latency_ms,
                        "error": r.error,
                    }
                )
            click.echo(_json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for r in results:
                click.echo("─" * 60)
                click.echo(f"  {r.code} ({r.name}) · last close ${r.last_close:.2f}")
                click.echo(
                    f"  position: {r.position_qty} @ ${r.avg_cost:.2f}"
                    f"  realized PnL ${r.realized_pnl:.2f}"
                )
                if r.error:
                    click.echo(f"  ❌ {r.error}")
                    continue
                d = r.decision
                if d is None:
                    click.echo("  (no decision)")
                    continue
                emoji = "🟢" if d.action == "BUY" else "🔴" if d.action == "SELL" else "⚪"
                notional = d.qty * r.last_close
                click.echo(
                    f"  {emoji} {d.action} {d.qty} (conf {d.confidence:.2f})"
                    + (f"  ≈${notional:,.0f}" if d.qty else "")
                )
                click.echo(f"  💬 {d.reason}")
                click.echo(f"  ({r.latency_ms}ms)")
            click.echo("─" * 60)

        if execute:
            _execute_analysis_results(s, cfg, results)


def _execute_analysis_results(
    session: Any, cfg: Any, results: list[Any]
) -> None:
    """For each non-HOLD analyze result, materialise a manual Signal +
    paper trade. Mirrors the logic of `equity-monitor trade confirm`,
    minus the human-confirmation handshake.
    """
    import json as _json
    from datetime import datetime as _dt

    from equity_monitor.trader.execute import (
        SignalExecutionError,
        execute_signal_trade,
    )

    actionable = [
        r for r in results if r.decision and r.decision.action in ("BUY", "SELL")
    ]
    if not actionable:
        click.echo("(no actionable decisions to execute)")
        return

    trader = _make_trader(cfg)
    try:
        for r in actionable:
            sym = (
                session.query(Symbol)
                .filter(Symbol.code == r.code, Symbol.is_active.is_(True))
                .one_or_none()
            )
            if sym is None:
                click.echo(f"  skip {r.code}: not in DB")
                continue
            d = r.decision
            sig = SignalRow(
                symbol_id=sym.id,
                ts=_dt.utcnow(),
                signal_type="llm_analyze_manual",
                severity="INFO",
                payload_json=_json.dumps(
                    {
                        "source": "analyze_cli",
                        "confidence": d.confidence,
                        "reason": d.reason,
                        "last_close": r.last_close,
                    },
                    ensure_ascii=False,
                ),
                delivered=0,
                suggested_action=d.action,
                suggested_qty=d.qty,
                status="pending",
            )
            session.add(sig)
            session.flush()
            try:
                trade_id = execute_signal_trade(
                    session, sig, sym, d.qty, trader
                )
                click.echo(
                    f"  ✅ {r.code}: signal_id={sig.id} trade_id={trade_id} "
                    f"{d.action} {d.qty} @ ~${r.last_close:.2f}"
                )
            except SignalExecutionError as e:
                click.echo(f"  ❌ {r.code}: {e}")
    finally:
        try:
            trader.close()
        except Exception:
            pass


@cli.group()
def trade() -> None:
    """Phase 2 paper-trading subcommands."""


@trade.command("list")
@click.option(
    "--status",
    type=click.Choice(["pending", "confirmed", "executed", "cancelled", "all"]),
    default="pending",
    show_default=True,
)
@click.pass_context
def trade_list(ctx: click.Context, status: str) -> None:
    """Show today's signal suggestions with their signal_id."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    with session_scope(factory) as s:
        q = s.query(SignalRow).filter(SignalRow.suggested_action.isnot(None))
        if status != "all":
            q = q.filter(SignalRow.status == status)
        rows = q.order_by(SignalRow.ts.desc()).limit(50).all()
        if not rows:
            click.echo(f"(no signals with status={status})")
            return
        sym_by_id = {sym.id: sym for sym in s.query(Symbol).all()}
        click.echo(f"{'id':>5}  {'code':12s}  {'action':6s}  {'qty':>4}  status     ts")
        for sig in rows:
            code = sym_by_id[sig.symbol_id].code
            click.echo(
                f"{sig.id:>5}  {code:12s}  {sig.suggested_action or '-':6s}  "
                f"{sig.suggested_qty or 0:>4}  {sig.status:10s} {sig.ts.isoformat(timespec='minutes')}"
            )


def _execute_paper_trade(
    s: Any, sig: SignalRow, sym: Symbol, qty: int, trader: Any
) -> int:
    """CLI-side wrapper: delegate to trader.execute and translate errors.

    Lifts SignalExecutionError → click.ClickException so the standard CLI
    error rendering kicks in.
    """
    from equity_monitor.trader.execute import (
        SignalExecutionError,
        execute_signal_trade,
    )

    try:
        return execute_signal_trade(s, sig, sym, qty, trader)
    except SignalExecutionError as e:
        raise click.ClickException(str(e)) from e


@trade.command("confirm")
@click.argument("signal_id", type=int)
@click.option(
    "--qty",
    type=int,
    default=None,
    help="Override the suggested qty (default: use the suggestion).",
)
@click.pass_context
def trade_confirm(ctx: click.Context, signal_id: int, qty: int | None) -> None:
    """Place a paper order for a pending suggestion."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    trader = _make_trader(cfg)

    try:
        with session_scope(factory) as s:
            sig = s.query(SignalRow).filter(SignalRow.id == signal_id).one_or_none()
            if sig is None:
                raise click.ClickException(f"signal {signal_id} not found")
            if sig.status == "executed":
                click.echo(
                    f"signal {signal_id} already executed (trade_id={sig.executed_trade_id})"
                )
                return
            if sig.status != "pending":
                raise click.ClickException(
                    f"signal {signal_id} status={sig.status}, can only confirm pending"
                )
            if sig.suggested_action is None:
                raise click.ClickException(
                    f"signal {signal_id} has no suggested_action"
                )
            sym = s.query(Symbol).filter(Symbol.id == sig.symbol_id).one()
            chosen_qty = qty if qty is not None else (sig.suggested_qty or 0)
            if chosen_qty <= 0:
                raise click.ClickException(
                    f"qty must be positive (got {chosen_qty})"
                )
            trade_id = _execute_paper_trade(s, sig, sym, chosen_qty, trader)
            click.echo(
                f"placed paper order: signal_id={signal_id} trade_id={trade_id} "
                f"{sig.suggested_action} {chosen_qty} {sym.code}"
            )
    finally:
        try:
            trader.close()
        except Exception:
            pass


@trade.command("cancel")
@click.argument("signal_id", type=int)
@click.pass_context
def trade_cancel(ctx: click.Context, signal_id: int) -> None:
    """Mark a pending suggestion as cancelled (no order is placed)."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    with session_scope(factory) as s:
        sig = s.query(SignalRow).filter(SignalRow.id == signal_id).one_or_none()
        if sig is None:
            raise click.ClickException(f"signal {signal_id} not found")
        if sig.status != "pending":
            raise click.ClickException(
                f"signal {signal_id} status={sig.status}, only pending can be cancelled"
            )
        sig.status = "cancelled"
    click.echo(f"cancelled signal {signal_id}")


@trade.command("positions")
@click.pass_context
def trade_positions(ctx: click.Context) -> None:
    """Show open paper positions with mark-to-market P&L (DB-side)."""
    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    with session_scope(factory) as s:
        rows = s.query(Position).filter(Position.qty > 0).all()
        if not rows:
            click.echo("(no open positions)")
            return
        sym_by_id = {sym.id: sym for sym in s.query(Symbol).all()}
        click.echo(f"{'code':12s}  {'qty':>5}  {'avg_cost':>10}  {'realized_pnl':>14}")
        for p in rows:
            code = sym_by_id[p.symbol_id].code
            click.echo(
                f"{code:12s}  {p.qty:>5}  {p.avg_cost:>10.2f}  "
                f"{p.realized_pnl:>14.2f}"
            )


@trade.command("pnl")
@click.option("--days", type=int, default=7, show_default=True)
@click.pass_context
def trade_pnl(ctx: click.Context, days: int) -> None:
    """Print cumulative realized P&L by symbol for the last N days."""
    from datetime import datetime, timedelta, timezone

    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    with session_scope(factory) as s:
        trades = s.query(Trade).filter(Trade.ts >= cutoff).all()
        if not trades:
            click.echo(f"(no trades in the last {days} days)")
            return
        sym_by_id = {sym.id: sym for sym in s.query(Symbol).all()}
        # Recompute realized P&L by replaying fills (FIFO via avg-cost)
        ledger: dict[str, dict[str, float]] = {}
        for t in sorted(trades, key=lambda x: x.ts):
            code = sym_by_id[t.symbol_id].code
            book = ledger.setdefault(code, {"qty": 0.0, "avg_cost": 0.0, "realized": 0.0})
            if t.side == "BUY":
                new_qty = book["qty"] + t.qty
                book["avg_cost"] = (
                    book["qty"] * book["avg_cost"] + t.qty * t.price
                ) / new_qty if new_qty > 0 else 0.0
                book["qty"] = new_qty
            else:
                realized = (t.price - book["avg_cost"]) * t.qty
                book["realized"] += realized
                book["qty"] -= t.qty
                if book["qty"] <= 0:
                    book["avg_cost"] = 0.0

        total = 0.0
        click.echo(
            f"{'code':12s}  {'fills':>5}  realized_pnl"
        )
        for code, book in ledger.items():
            n = sum(1 for t in trades if sym_by_id[t.symbol_id].code == code)
            click.echo(f"{code:12s}  {n:>5}  {book['realized']:>+12.2f}")
            total += book["realized"]
        click.echo(f"{'TOTAL':12s}  {'':>5}  {total:>+12.2f}")


@cli.command()
@click.option(
    "--days",
    default=30,
    show_default=True,
    type=int,
    help="How many calendar days of 60-min K-line to pull (~7 bars per US trading day).",
)
@click.pass_context
def backfill(ctx: click.Context, days: int) -> None:
    """Backfill historical 60-min OHLC + indicators for the entire watchlist."""
    cfg = _get_cfg(ctx)
    wl = _get_watchlist(ctx)
    factory = _make_factory(cfg)

    client = OpenDClient(cfg.opend.host, cfg.opend.port)
    try:
        out = backfill_all(
            client=client,
            factory=factory,
            codes=[s.code for s in wl.symbols],
            days=days,
        )
    finally:
        client.close()
    for code, stats in out.items():
        click.echo(
            f"{code}: quotes={stats['quotes']} indicators={stats['indicators']}"
        )


# ---------------------------------------------------------------------------
# HITL decide CLI — receive decisions submitted by Claude via Cursor.
# ---------------------------------------------------------------------------


def _make_packet_store(cfg: Any) -> Any:
    """Resolve the configured PacketStore. Centralised so tests can patch."""
    from equity_monitor.decisions.store import PacketStore

    var_dir = Path(cfg.trader.strategy.hitl.var_dir)
    return PacketStore(var_dir)


@cli.group("decide")
def decide_group() -> None:
    """Human-in-the-Loop decision pipeline.

    HITL strategy writes a packet to var/decisions/pending/ on each event;
    you paste it into Cursor/Claude (which has access to MEMORY + tools);
    Claude outputs a decision JSON; you submit it back via `decide submit`
    and the system places the paper trade.
    """


@decide_group.command("list")
@click.option(
    "--state",
    type=click.Choice(["pending", "submitted", "executed", "cancelled", "all"]),
    default="pending",
    show_default=True,
)
@click.pass_context
def decide_list(ctx: click.Context, state: str) -> None:
    """Show packets in the requested state, oldest first."""
    from equity_monitor.decisions.store import PacketState

    cfg = _get_cfg(ctx)
    store = _make_packet_store(cfg)

    states = list(PacketState) if state == "all" else [PacketState(state)]
    found = False
    for s in states:
        for sp in store.list(state=s):
            found = True
            p = sp.packet
            price = "n/a"
            if p.snapshot and p.snapshot.get("last_price") is not None:
                try:
                    price = f"${float(p.snapshot['last_price']):.2f}"
                except (TypeError, ValueError):
                    pass
            triggers = ",".join(p.triggering_signal_types) or "(none)"
            click.echo(
                f"[{s.value:9s}] {p.id}  {p.code:8s}  price={price:8s}  "
                f"qty={p.position_qty:>4}  triggers={triggers}"
            )
    if not found:
        click.echo(f"(no packets in state={state})")


@decide_group.command("show")
@click.argument("packet_id")
@click.pass_context
def decide_show(ctx: click.Context, packet_id: str) -> None:
    """Print a packet's markdown prompt to stdout (paste into Cursor)."""
    cfg = _get_cfg(ctx)
    store = _make_packet_store(cfg)
    sp = store.get(packet_id)
    if sp is None:
        raise click.ClickException(f"packet {packet_id!r} not found")
    click.echo(sp.markdown())


@decide_group.command("submit")
@click.argument("packet_id")
@click.option(
    "--json",
    "json_str",
    type=str,
    default=None,
    help="Decision JSON inline (use shell single-quotes around the whole arg).",
)
@click.option(
    "--file",
    "json_file",
    type=click.Path(exists=True),
    default=None,
    help="Read decision JSON from a file instead of --json.",
)
@click.option(
    "--no-execute",
    is_flag=True,
    default=False,
    help="Just record the decision, don't actually place a paper trade.",
)
@click.pass_context
def decide_submit(
    ctx: click.Context,
    packet_id: str,
    json_str: str | None,
    json_file: str | None,
    no_execute: bool,
) -> None:
    """Submit Claude's decision JSON for a pending packet, then trade."""
    import json as _json

    from equity_monitor.decisions.store import PacketState
    from equity_monitor.signals.strategy_llm import (
        ConstraintViolation,
        enforce_constraints,
    )
    from equity_monitor.llm.prompt import ParsedDecision

    if (json_str is None) == (json_file is None):
        raise click.UsageError("provide exactly one of --json or --file")

    raw = json_str if json_str is not None else Path(json_file).read_text()
    try:
        decision = _json.loads(raw)
    except _json.JSONDecodeError as e:
        raise click.ClickException(f"invalid JSON: {e}") from e

    cfg = _get_cfg(ctx)
    factory = _make_factory(cfg)
    store = _make_packet_store(cfg)

    sp = store.get(packet_id)
    if sp is None:
        raise click.ClickException(f"packet {packet_id!r} not found")
    if sp.state != PacketState.PENDING:
        raise click.ClickException(
            f"packet {packet_id!r} is in state={sp.state.value}, "
            f"can only submit while pending"
        )

    # Step 1: persist the raw decision (transitions PENDING → SUBMITTED).
    try:
        sp = store.submit(packet_id, decision)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    click.echo("✓ recorded decision; packet now in state=submitted")

    if no_execute:
        click.echo("--no-execute set; stopping before paper trade")
        return

    # Step 2: validate against the same constraints LLMStrategy uses.
    p = sp.packet
    try:
        parsed = ParsedDecision(
            action=decision["action"],
            qty=int(decision["qty"]),
            confidence=float(decision["confidence"]),
            reason=str(decision["reason"]),
        )
    except (KeyError, ValueError, TypeError) as e:
        store.mark_executed(
            packet_id,
            execution={"status": "REJECTED", "error": f"bad decision shape: {e}"},
        )
        raise click.ClickException(f"decision invalid: {e}") from e

    constraints = p.constraints or {}
    try:
        suggest = enforce_constraints(
            parsed,
            position_qty=p.position_qty,
            max_position=int(constraints.get("max_position", 200)),
            min_trade_size=int(constraints.get("min_trade_size", 10)),
            min_confidence=float(constraints.get("min_confidence", 0.6)),
        )
    except ConstraintViolation as e:
        store.mark_executed(
            packet_id,
            execution={
                "status": "REJECTED",
                "error": f"constraint violation: {e}",
            },
        )
        raise click.ClickException(f"constraint violation: {e}") from e

    if suggest.action == "HOLD":
        store.mark_executed(
            packet_id,
            execution={
                "status": "HOLD",
                "reason": suggest.reason,
            },
        )
        click.echo(f"✓ HOLD recorded (no trade placed): {suggest.reason}")
        return

    # Step 3: place the paper trade. We need a fresh SignalRow to attach
    # the trade to; HITL doesn't carry one (it returns None from decide).
    # So we synthesise one tagged with the packet id, mark it 'pending',
    # and reuse execute_signal_trade.
    from datetime import datetime, timezone

    from equity_monitor.trader.execute import (
        SignalExecutionError,
        execute_signal_trade,
    )

    trader = _make_trader(cfg)
    try:
        with session_scope(factory) as s:
            sym = s.query(Symbol).filter(Symbol.code == p.code).one_or_none()
            if sym is None:
                raise click.ClickException(
                    f"symbol {p.code!r} not in DB; run `equity-monitor watchlist sync` first"
                )
            sig = SignalRow(
                symbol_id=sym.id,
                ts=datetime.now(tz=timezone.utc),
                signal_type=f"hitl:{packet_id}",
                severity="WARN",
                payload_json="{}",
                delivered=False,
                suggested_action=suggest.action,
                suggested_qty=suggest.qty,
                status="pending",
            )
            s.add(sig)
            s.flush()
            try:
                trade_id = execute_signal_trade(s, sig, sym, suggest.qty, trader)
            except SignalExecutionError as e:
                store.mark_executed(
                    packet_id,
                    execution={"status": "REJECTED", "error": str(e)},
                )
                raise click.ClickException(str(e)) from e
            store.mark_executed(
                packet_id,
                execution={
                    "status": "FILLED_OR_PENDING",
                    "trade_id": trade_id,
                    "side": suggest.action,
                    "qty": suggest.qty,
                    "reason": suggest.reason,
                },
            )
            click.echo(
                f"✓ paper trade placed: trade_id={trade_id} "
                f"{suggest.action} {suggest.qty} {p.code}"
            )
    finally:
        try:
            trader.close()
        except Exception:
            pass


@decide_group.command("cancel")
@click.argument("packet_id")
@click.option("--reason", default="user-cancelled", show_default=True)
@click.pass_context
def decide_cancel(ctx: click.Context, packet_id: str, reason: str) -> None:
    """Cancel a pending or submitted packet without trading."""
    cfg = _get_cfg(ctx)
    store = _make_packet_store(cfg)
    try:
        store.cancel(packet_id, reason=reason)
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"✓ cancelled packet {packet_id} ({reason})")


if __name__ == "__main__":
    cli()
